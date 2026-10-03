import json
import sys
import tempfile
import unittest
from pathlib import Path

from local_coder.backend import ScriptedBackend
from local_coder.engine import Engine
from local_coder.store import Store
from local_coder.supervisor import CommandSupervisorBackend, ScriptedSupervisorBackend, Supervisor


class RecordingBackend(ScriptedBackend):
    def __init__(self, actions):
        super().__init__(actions)
        self.messages = []

    def generate(self, messages):
        self.messages.append(messages)
        return super().generate(messages)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        (self.repo / 'test_calc.py').write_text(
            'import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n'
            '    def test_add(self): self.assertEqual(add(2, 3), 5)\n')
        self.store = Store(self.root / 'agent.db')
        self.addCleanup(self.store.close)

    def supervised(self, worker_actions, supervisor_responses, max_steps=12):
        worker = RecordingBackend(worker_actions)
        supervisor = Supervisor(self.store, ScriptedSupervisorBackend(supervisor_responses))
        engine = Engine(self.store, worker, max_steps=max_steps, supervisor=supervisor)
        run = engine.create(self.repo, 'Repair addition without changing tests.',
                            ['python3', '-m', 'unittest', 'discover'], self.root / 'runs')
        return engine, worker, run

    def test_two_node_plan_drives_worker_and_final_audit(self):
        engine, worker, run = self.supervised([
            {'tool': 'Read', 'args': {'file_path': 'calc.py'}},
            {'tool': 'Finish', 'args': {}},
            {'tool': 'Edit', 'args': {'file_path': 'calc.py',
                                      'old_string': 'a - b', 'new_string': 'a + b'}},
            {'tool': 'Finish', 'args': {}},
        ], [
            {'nodes': [
                {'id': 'inspect', 'title': 'Inspect defect',
                 'objective': 'Inspect the arithmetic implementation.',
                 'success_criteria': ['Identify the faulty operation']},
                {'id': 'fix', 'title': 'Repair defect',
                 'objective': 'Repair addition while preserving tests.',
                 'blocked_by': ['inspect'],
                 'success_criteria': ['Addition passes acceptance tests']},
            ]},
            {'decision': 'accept', 'guidance': 'Cause identified.'},
            {'decision': 'accept', 'guidance': 'Patch is minimal.'},
            {'decision': 'accept', 'guidance': 'Requirements are covered.'},
        ])
        result = engine.run(run)
        self.assertEqual(result['state'], 'COMPLETE')
        self.assertEqual([n['status'] for n in self.store.supervisor_nodes(run)],
                         ['completed', 'completed'])
        self.assertIn('a + b', (Path(result['workspace']) / 'calc.py').read_text())
        contexts = [json.loads(call[1]['content']).get('supervisor') for call in worker.messages]
        self.assertTrue(any(c and c.get('current', {}).get('id') == 'inspect' for c in contexts))
        self.assertTrue(any(c and c.get('current', {}).get('id') == 'fix' for c in contexts))

    def test_revision_guidance_returns_to_same_worker_node(self):
        engine, worker, run = self.supervised([
            {'tool': 'Finish', 'args': {}},
            {'tool': 'Edit', 'args': {'file_path': 'calc.py',
                                      'old_string': 'a - b', 'new_string': 'a + b'}},
            {'tool': 'Finish', 'args': {}},
        ], [
            {'nodes': [{'id': 'fix', 'title': 'Fix', 'objective': 'Correct addition.',
                        'success_criteria': ['Tests pass']}]},
            {'decision': 'revise', 'guidance': 'No code change yet; inspect and repair calc.py.'},
            {'decision': 'accept', 'guidance': 'Corrected.'},
            {'decision': 'accept', 'guidance': 'Ready.'},
        ])
        self.assertEqual(engine.run(run)['state'], 'COMPLETE')
        serialized = '\n'.join(message['content'] for call in worker.messages for message in call)
        self.assertIn('No code change yet', serialized)
        self.assertEqual(self.store.supervisor_nodes(run)[0]['attempts'], 2)

    def test_invalid_supervisor_plan_is_infrastructure_blocked(self):
        engine, _, run = self.supervised([], [
            {'nodes': [
                {'id': 'a', 'objective': 'A', 'blocked_by': ['b']},
                {'id': 'b', 'objective': 'B', 'blocked_by': ['a']},
            ]}
        ])
        self.assertEqual(engine.run(run)['state'], 'INFRA_BLOCKED')
        self.assertTrue(any(e['kind'] == 'SUPERVISOR_ERROR'
                            for e in self.store.events(run)))

    def test_command_backend_uses_json_stdio_protocol(self):
        code = ('import json,sys; req=json.load(sys.stdin); '
                'print(json.dumps({"operation":req["operation"],"ok":True}))')
        backend = CommandSupervisorBackend([sys.executable, '-c', code], timeout=5)
        self.assertEqual(backend.call('plan', {'x': 1})['operation'], 'plan')
        self.assertEqual(backend.configuration()['command'][0], sys.executable)

    def test_supervisor_state_survives_store_reopen(self):
        engine, _, run = self.supervised([
            {'tool': 'Finish', 'args': {}},
        ], [
            {'nodes': [{'id': 'inspect', 'objective': 'Inspect only.'},
                       {'id': 'fix', 'objective': 'Fix later.',
                        'blocked_by': ['inspect']}]},
            {'decision': 'accept', 'guidance': 'Inspection complete.'},
        ], max_steps=1)
        self.assertEqual(engine.run(run)['state'], 'BUDGET_EXHAUSTED')
        other = Store(self.root / 'agent.db')
        self.addCleanup(other.close)
        nodes = other.supervisor_nodes(run)
        self.assertEqual(nodes[0]['status'], 'completed')
        self.assertEqual(nodes[1]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
