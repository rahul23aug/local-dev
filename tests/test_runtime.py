import tempfile
import unittest
from pathlib import Path

from local_coder.backend import InfrastructureError, ScriptedBackend
from local_coder.engine import Engine
from local_coder.store import Store
from local_coder.tools import Tools


class BrokenBackend:
    def generate(self, messages): raise InfrastructureError('Runtime expired')


class RuntimeTests(unittest.TestCase):
    def test_infrastructure_failure_is_not_coding_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            (repo / 'source.py').write_text('x = 1\n')
            store = Store(root / 'state.db')
            self.addCleanup(store.close)
            engine = Engine(store, BrokenBackend())
            run = engine.create(repo, 'Repair', ['python3', '-c', 'pass'], root / 'runs')
            result = engine.run(run)
            self.assertEqual(result['state'], 'INFRA_BLOCKED')
            self.assertEqual(store.events(run)[-1]['kind'], 'INFRA_ERROR')

    def test_verifier_times_out(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'workspace'
            root.mkdir()
            result = Tools(root, ['python3', '-c', 'import time; time.sleep(5)'], {},
                           timeout=0.05).verify()
            self.assertFalse(result['passed'])
            self.assertTrue(result['timeout'])

    def test_full_verifier_evidence_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'workspace'
            root.mkdir()
            result = Tools(root, ['python3', '-c', 'print("first"); print("x"*10000)'], {}).verify()
            self.assertTrue(result['passed'])
            self.assertNotIn('first', result['output'])
            self.assertTrue(Path(result['evidence_path']).read_text().startswith('first'))

    def test_output_flood_is_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'workspace'
            root.mkdir()
            result = Tools(root, ['python3', '-c', 'print("x"*2000000)'], {}).verify()
            self.assertFalse(result['passed'])
            self.assertTrue(result['output_limit'])

    def test_unchanged_repository_is_not_marked_repaired(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            (repo / 'source.py').write_text('x = 1\n')
            store = Store(root / 'state.db')
            self.addCleanup(store.close)
            engine = Engine(store, ScriptedBackend([{'tool': 'finish', 'args': {}}]), 1)
            run = engine.create(repo, 'Repair', ['python3', '-c', 'pass'], root / 'runs')
            self.assertEqual(engine.run(run)['state'], 'BUDGET_EXHAUSTED')

    def test_budget_resumes_at_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            (repo / 'source.py').write_text('x = 1\n')
            store = Store(root / 'state.db')
            self.addCleanup(store.close)
            engine = Engine(store, ScriptedBackend([{'tool': 'list', 'args': {}}]), 1)
            run = engine.create(repo, 'Repair', ['python3', '-c', 'pass'], root / 'runs')
            engine.run(run)
            resumed = Engine(store, ScriptedBackend([
                {'tool': 'replace', 'args': {'path': 'source.py', 'old': 'x = 1', 'new': 'x = 2'}},
                {'tool': 'finish', 'args': {}},
            ]), 3)
            self.assertEqual(resumed.run(run)['state'], 'COMPLETE')
            self.assertEqual(store.get(run)['steps'], 3)

    def test_diagnostic_report_has_honest_metrics(self):
        from local_coder.report import report
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            store = Store(root / 'state.db')
            self.addCleanup(store.close)
            engine = Engine(store, ScriptedBackend([{'tool': 'finish', 'args': {}}]), 1)
            run = engine.create(repo, 'Repair', ['python3', '-c', 'exit(1)'], root / 'runs')
            engine.run(run)
            result = report(store, run)
            self.assertEqual(result['false_completion_requests'], 1)
            self.assertEqual(result['model_calls'], 1)
            self.assertFalse(result['verified_complete'])

    def test_explicit_verify_is_counted(self):
        from local_coder.report import report
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            store = Store(root / 'state.db')
            self.addCleanup(store.close)
            engine = Engine(store, ScriptedBackend([{'tool': 'verify', 'args': {}}]), 1)
            run = engine.create(repo, 'Repair', ['python3', '-c', 'pass'], root / 'runs')
            engine.run(run)
            self.assertEqual(report(store, run)['verification_runs'], 1)

    def test_missing_verifier_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            store = Store(root / 'state.db')
            self.addCleanup(store.close)
            engine = Engine(store, ScriptedBackend([{'tool': 'finish', 'args': {}}]), 5)
            run = engine.create(repo, 'Repair', ['/nonexistent/local-coder-verifier'], root / 'runs')
            self.assertEqual(engine.run(run)['state'], 'INFRA_BLOCKED')
            self.assertEqual(store.get(run)['steps'], 1)
