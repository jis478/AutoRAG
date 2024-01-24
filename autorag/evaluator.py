import json
import logging
import os
import shutil
from datetime import datetime
from typing import List, Dict

import click
import pandas as pd
import yaml

from autorag.node_line import run_node_line
from autorag.nodes.retrieval.bm25 import bm25_ingest
from autorag.schema import Node
from autorag.schema.node import module_type_exists
from autorag.utils import cast_qa_dataset, cast_corpus_dataset

logger = logging.getLogger("AutoRAG")


class Evaluator:
    def __init__(self, qa_data_path: str, corpus_data_path: str):
        # validate data paths
        if not os.path.exists(qa_data_path):
            raise ValueError(f"QA data path {qa_data_path} does not exist.")
        if not os.path.exists(corpus_data_path):
            raise ValueError(f"Corpus data path {corpus_data_path} does not exist.")
        if not qa_data_path.endswith('.parquet'):
            raise ValueError(f"QA data path {qa_data_path} is not a parquet file.")
        if not corpus_data_path.endswith('.parquet'):
            raise ValueError(f"Corpus data path {corpus_data_path} is not a parquet file.")
        self.qa_data = pd.read_parquet(qa_data_path)
        self.corpus_data = pd.read_parquet(corpus_data_path)
        self.qa_data = cast_qa_dataset(self.qa_data)
        self.corpus_data = cast_corpus_dataset(self.corpus_data)

        # copy dataset to project directory
        if not os.path.exists(os.path.join(os.getcwd(), 'data')):
            os.makedirs(os.path.join(os.getcwd(), 'data'))
        qa_path_in_project = os.path.join(os.getcwd(), 'data', 'qa.parquet')
        if not os.path.exists(qa_path_in_project):
            shutil.copy(qa_data_path, qa_path_in_project)
        corpus_path_in_project = os.path.join(os.getcwd(), 'data', 'corpus.parquet')
        if not os.path.exists(corpus_path_in_project):
            shutil.copy(corpus_data_path, corpus_path_in_project)

        self.project_dir = os.getcwd()

    def start_trial(self, yaml_path: str):
        trial_name = self.__get_new_trial_name()
        self.__make_trial_dir(trial_name)

        node_lines = self._load_node_lines(yaml_path)
        self.__embed(node_lines)

        trial_summary_df = pd.DataFrame(columns=['node_line_name', 'node_type', 'best_module_filename',
                                                 'best_module_name', 'best_module_params', 'best_execution_time'])
        for i, (node_line_name, node_line) in enumerate(node_lines.items()):
            node_line_dir = os.path.join(self.project_dir, trial_name, node_line_name)
            os.makedirs(node_line_dir, exist_ok=False)
            if i == 0:
                previous_result = self.qa_data
            logger.info(f'Running node line {node_line_name}...')
            previous_result = run_node_line(node_line, node_line_dir, previous_result)

            summary_df = pd.read_parquet(os.path.join(node_line_dir, 'summary.parquet'))
            summary_df = summary_df.assign(node_line_name=node_line_name)
            summary_df = summary_df[list(trial_summary_df.columns)]
            if len(trial_summary_df) <= 0:
                trial_summary_df = summary_df
            else:
                trial_summary_df = pd.concat([trial_summary_df, summary_df], ignore_index=True)

        trial_summary_df.to_parquet(os.path.join(self.project_dir, trial_name, 'summary.parquet'), index=False)

    def __embed(self, node_lines: Dict[str, List[Node]]):
        if any(list(map(lambda nodes: module_type_exists(nodes, 'bm25'), node_lines.values()))):
            # ingest BM25 corpus
            logger.info('Embedding BM25 corpus...')
            bm25_dir = os.path.join(self.project_dir, 'resources', 'bm25.pkl')
            if not os.path.exists(os.path.dirname(bm25_dir)):
                os.makedirs(os.path.dirname(bm25_dir))
            if os.path.exists(bm25_dir):
                logger.debug('BM25 corpus already exists.')
            else:
                bm25_ingest(bm25_dir, self.corpus_data)
            logger.info('BM25 corpus embedding complete.')
            pass
        elif any(list(map(lambda nodes: module_type_exists(nodes, 'vectordb'), node_lines.values()))):
            # TODO: ingest vector DB
            pass
        else:
            logger.info('No ingestion needed.')

    def __get_new_trial_name(self) -> str:
        trial_json_path = os.path.join(self.project_dir, 'trial.json')
        if not os.path.exists(trial_json_path):
            return '0'
        with open(trial_json_path, 'r') as f:
            trial_json = json.load(f)
        return str(int(trial_json[-1]['trial_name']) + 1)

    def __make_trial_dir(self, trial_name: str):
        trial_json_path = os.path.join(self.project_dir, 'trial.json')
        if os.path.exists(trial_json_path):
            with open(trial_json_path, 'r') as f:
                trial_json = json.load(f)
        else:
            trial_json = []

        trial_json.append({
            'trial_name': trial_name,
            'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
        os.makedirs(os.path.join(self.project_dir, trial_name))
        with open(trial_json_path, 'w') as f:
            json.dump(trial_json, f, indent=4)

    @staticmethod
    def _load_node_lines(yaml_path: str) -> Dict[str, List[Node]]:
        if not os.path.exists(yaml_path):
            raise ValueError(f"YAML file {yaml_path} does not exist.")
        with open(yaml_path, 'r') as stream:
            try:
                yaml_dict = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                raise ValueError(f"YAML file {yaml_path} could not be loaded.") from exc

        node_lines = yaml_dict['node_lines']
        node_line_dict = {}
        for node_line in node_lines:
            node_line_dict[node_line['node_line_name']] = list(
                map(lambda node: Node.from_dict(node), node_line['nodes']))
        return node_line_dict


@click.group()
def cli():
    pass


@click.command()
@click.option('--config', '-c', help='Path to config yaml file. Must be yaml or yml file.', type=str)
@click.option('--qa_data_path', help='Path to QA dataset. Must be parquet file.', type=str)
@click.option('--corpus_data_path', help='Path to corpus dataset. Must be parquet file.', type=str)
def evaluate(config, qa_data_path, corpus_data_path):
    if not config.endswith('.yaml') and not config.endswith('.yml'):
        raise ValueError(f"Config file {config} is not a parquet file.")
    if not os.path.exists(config):
        raise ValueError(f"Config file {config} does not exist.")
    evaluator = Evaluator(qa_data_path, corpus_data_path)
    evaluator.start_trial(config)
    logger.info('Evaluation complete.')


cli.add_command(evaluate)

if __name__ == '__main__':
    cli()
