import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from local_coder.backend import ColabBackend, InfrastructureError


def remote_fixture(port, messages, max_tokens, context):
    return {'reply': '{"tool":"list","args":{}}', 'usage': {}}


class ColabAdapterTests(unittest.TestCase):
    def make(self, temp, execute):
        root = Path(temp)
        (root / 'environment.json').write_text(json.dumps({'port': 18080, 'context': 8192}))
        fake_agent = types.SimpleNamespace(exec_recoverable=execute, safe_error=lambda text: text)
        fake_chat = types.SimpleNamespace(generate_reply=remote_fixture)
        with patch.dict(sys.modules, {'agent': fake_agent, 'chat': fake_chat}):
            old_path = list(sys.path)
            try: return ColabBackend(root)
            finally: sys.path[:] = old_path

    def test_receipt_transport_and_usage(self):
        calls = []
        def execute(config, code, timeout):
            calls.append((code, timeout))
            return subprocess.CompletedProcess([], 0, 'LOCAL_CODER_REPLY=' + json.dumps({
                'reply': '{"tool":"list","args":{}}', 'usage': {'prompt_tokens': 50},
                'finish_reason': 'stop'}) + '\n', '')
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make(temp, execute)
            result = backend.generate([{'role': 'user', 'content': 'Test'}])
            self.assertEqual(result['usage']['prompt_tokens'], 50)
            self.assertIn('1024,8192', calls[0][0])
            self.assertEqual(calls[0][1], 680)

    def test_no_receipt_is_an_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make(temp, lambda *a, **kw: subprocess.CompletedProcess([], 0, '', ''))
            with self.assertRaises(InfrastructureError):
                backend.generate([{'role': 'user', 'content': 'Test'}])

    def test_existing_colab_lock_prevents_concurrent_inference(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make(temp, lambda *a, **kw: self.fail('Transport must not execute'))
            backend.lock_path.parent.mkdir()
            with backend.lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(InfrastructureError):
                    backend.generate([{'role': 'user', 'content': 'Test'}])

    def test_lock_storage_failure_is_classified(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make(temp, lambda *a, **kw: self.fail('Transport must not execute'))
            backend.lock_path.parent.write_text('Not a directory')
            with self.assertRaises(InfrastructureError): backend.generate([])

    def test_invalid_receipt_is_classified(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = self.make(temp, lambda *a, **kw: subprocess.CompletedProcess(
                [], 0, 'LOCAL_CODER_REPLY={"usage": []}\n', ''))
            with self.assertRaises(InfrastructureError): backend.generate([])
