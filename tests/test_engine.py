import json
import tempfile
import unittest
from pathlib import Path

from local_coder.backend import ScriptedBackend
from local_coder.engine import Engine
from local_coder.store import Store
from local_coder.tools import Tools


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        (self.repo / 'test_calc.py').write_text(
            'import unittest\nfrom calc import add\n'
            'class T(unittest.TestCase):\n'
            '    def test_add(self): self.assertEqual(add(2, 3), 5)\n')
        self.store = Store(self.root / 'state.db')
        self.addCleanup(self.store.close)

    def engine(self, actions, max_steps=8):
        return Engine(self.store, ScriptedBackend(actions), max_steps=max_steps)

    def create(self, engine):
        return engine.create(self.repo, 'Fix addition without changing tests.',
                             ['python3', '-m', 'unittest', 'discover'], self.root / 'runs')

    def test_false_finish_does_not_complete(self):
        engine = self.engine([{'tool': 'finish', 'args': {}}], 1)
        run = self.create(engine)
        engine.run(run)
        self.assertEqual(self.store.get(run)['state'], 'BUDGET_EXHAUSTED')
        self.assertIn('VERIFY', [e['kind'] for e in self.store.events(run)])

    def test_repair_then_verified_completion(self):
        engine = self.engine([
            {'tool': 'finish', 'args': {}},
            {'tool': 'replace', 'args': {'path': 'calc.py', 'old': 'a - b', 'new': 'a + b'}},
            {'tool': 'finish', 'args': {}},
        ])
        run = self.create(engine)
        engine.run(run)
        self.assertEqual(self.store.get(run)['state'], 'COMPLETE')
        self.assertIn('a - b', (self.repo / 'calc.py').read_text())
        self.assertIn('a + b', (Path(self.store.get(run)['workspace']) / 'calc.py').read_text())

    def test_state_survives_reopen(self):
        engine = self.engine([], 1)
        run = self.create(engine)
        other = Store(self.root / 'state.db')
        self.addCleanup(other.close)
        self.assertEqual(other.get(run)['state'], 'DISCOVERY')

    def test_path_escape_and_symlink_rejected(self):
        tools = Tools(self.repo, ['python3', '-m', 'unittest'], {})
        for path in ('../state.db', '/etc/passwd'):
            with self.assertRaises(ValueError): tools.execute('read', {'path': path})
        (self.repo / 'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError): tools.execute('read', {'path': 'escape'})

    def test_test_tampering_prevents_completion(self):
        engine = self.engine([])
        run = self.create(engine)
        row = self.store.get(run)
        tools = Tools(Path(row['workspace']), json.loads(row['verify_command']),
                      json.loads(row['protected']))
        with self.assertRaises(ValueError):
            tools.execute('replace', {'path': 'test_calc.py', 'old': '5', 'new': '-1'})
        (Path(row['workspace']) / 'test_calc.py').write_text('')
        self.assertFalse(tools.verify()['passed'])

    def test_malformed_output_is_bounded(self):
        engine = self.engine(['not json', {'tool': 'shell', 'args': {'cmd': 'id'}}], 2)
        run = self.create(engine)
        engine.run(run)
        self.assertEqual(self.store.get(run)['state'], 'BUDGET_EXHAUSTED')
        self.assertEqual(len([e for e in self.store.events(run) if e['kind'] == 'ACTION_ERROR']), 2)

    def test_interrupted_action_is_not_replayed(self):
        engine = self.engine([])
        run = self.create(engine)
        self.store.update(run, state='ACTING')
        engine.run(run)
        self.assertEqual(self.store.get(run)['state'], 'INTERRUPTED')

    def test_context_is_bounded(self):
        engine = self.engine([])
        run = self.create(engine)
        self.store.event(run, 'TOOL', {'output': 'x' * 100000})
        self.assertLess(sum(len(m['content']) for m in engine.messages(run)), 20000)


if __name__ == '__main__': unittest.main()
