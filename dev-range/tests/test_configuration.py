"""Offline template/identity checks before any live network configuration."""
import ast
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
definition = yaml.safe_load((ROOT/'ansible/group_vars/all.yml').read_text())


def load_network():
    spec = importlib.util.spec_from_file_location('range_network', ROOT/'ansible/files/range_network.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigurationTests(unittest.TestCase):
    def test_all_python_files_parse_without_running_their_entry_points(self):
        for folder in ['scripts', 'ansible/files']:
            for source in (ROOT/folder).glob('*.py'):
                with self.subTest(source=source.name): ast.parse(source.read_text(), filename=str(source))

    def test_all_playbooks_are_valid_yaml(self):
        for filename in ['site.yml', 'verify.yml']:
            plays = yaml.safe_load((ROOT/'ansible'/filename).read_text())
            self.assertTrue(plays and all(isinstance(play['tasks'], list) for play in plays))

    def test_rendered_networks_bind_every_role_to_its_own_tunnel(self):
        env = Environment(loader=FileSystemLoader(ROOT/'ansible/templates'), undefined=StrictUndefined)
        env.filters['to_json'] = json.dumps
        peers = {role: {'underlay_ip': '10.160.0.'+str(index)} for index, role in enumerate(definition['exercise_nodes'], 10)}
        for role in ['router', *peers]:
            rendered = env.get_template('network.json.j2').render(
                deployment_id='012345abcdef', range_role=role, underlay_ip='10.160.0.2',
                router_underlay_ip='10.160.0.2', exercise_nodes=definition['exercise_nodes'], hostvars=peers)
            value = json.loads(rendered)
            self.assertEqual(value['role'], role)
            self.assertEqual(len({node['vni'] for node in value['nodes'].values()}), 4)
            load_network().validate(value, {'deployment_id': '012345abcdef', 'role': role})

    def test_remote_marker_mismatch_refuses_network_changes(self):
        with self.assertRaises(RuntimeError):
            load_network().validate({'deployment_id': 'new', 'role': 'router'}, {'deployment_id': 'old', 'role': 'router'})

    def test_mission_and_adversary_are_distinct_lans(self):
        nodes = definition['exercise_nodes']
        self.assertEqual(nodes['analyst']['network'], nodes['victim']['network'])
        self.assertEqual(nodes['c2']['network'], nodes['goal']['network'])
        self.assertNotEqual(nodes['analyst']['network'], nodes['c2']['network'])


if __name__ == '__main__': unittest.main()

