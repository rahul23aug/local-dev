import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

try:
    from local_coder.protocol_tools import ProtocolTools
except ImportError:
    ProtocolTools = None

FIXTURE = Path(__file__).parent / 'fixtures' / 'protocol_server.py'

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'a.py').write_text('def hello():\n    return 42\n')

    def client(self, mode='normal'):
        self.assertIsNotNone(ProtocolTools, 'real ProtocolTools implementation is required')
        return ProtocolTools(self.root, {
            'timeout': 5,
            'lsp': {'python': {'command': [sys.executable, str(FIXTURE), 'lsp', mode], 'extensions': ['.py']}},
            'mcp': {'fixture': {'command': [sys.executable, str(FIXTURE), 'mcp', mode]}}})

    def test_lsp_hover_initializes_and_opens_document(self):
        result = self.client().execute('LSP', {'operation': 'hover', 'file_path': 'a.py', 'line': 2, 'character': 4})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['contents']['value'], 'actual hover')

    def test_definitions_and_references_filter_external_locations(self):
        for operation in ('definition', 'references'):
            result = self.client().execute('LSP', {'action': operation, 'file_path': 'a.py', 'line': 2, 'character': 4})
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(result['result']), 1)
            self.assertEqual(result['result'][0]['range']['start'], {'line': 1, 'character': 4})

    def test_symbols_and_push_diagnostics(self):
        for operation in ('document_symbols', 'workspace_symbols', 'diagnostics'):
            result = self.client().execute('LSP', {'operation': operation, 'file_path': 'a.py', 'query': 'hello'})
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['result'])

    def test_mcp_real_list_read_list_tools_call(self):
        client = self.client()
        for name, args, key in (
            ('ListMcpResourcesTool', {}, 'resources'),
            ('read_mcp_resource', {'server': 'fixture', 'uri': 'fixture://hello'}, 'contents'),
            ('list_mcp_tools', {'server': 'fixture'}, 'tools'),
            ('call_mcp_tool', {'server': 'fixture', 'name': 'echo', 'arguments': {'hello': 'world'}}, 'content')):
            result = client.execute(name, args)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['result'][key])

    def test_model_cannot_override_backend(self):
        for key in ('command', 'env', 'timeout'):
            result = self.client().execute('LSP', {'operation': 'hover', 'file_path': 'a.py', key: 'malicious'})
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'], 'invalid_arguments')

    def test_lsp_rejects_outside_and_symlink_paths(self):
        (self.root / 'link.py').symlink_to(self.root / 'a.py')
        for path in ('../outside.py', 'link.py'):
            result = self.client().execute('LSP', {'operation': 'hover', 'file_path': path})
            self.assertFalse(result['ok'])

    def test_rpc_errors_are_not_fake_success(self):
        result = self.client('error').execute('read_mcp_resource', {'server': 'fixture', 'uri': 'fixture://hello'})
        self.assertFalse(result['ok'])
        self.assertIn('fixture rejected request', result['error'])

    def test_dead_and_output_flood_servers_are_bounded(self):
        for mode in ('dead', 'flood'):
            before = time.monotonic()
            result = self.client(mode).execute('ListMcpResourcesTool', {'server': 'fixture'})
            self.assertFalse(result['ok'])
            self.assertLess(time.monotonic() - before, 7)

    def test_timeout_is_bounded(self):
        before = time.monotonic()
        result = self.client('timeout').execute('ListMcpResourcesTool', {'server': 'fixture'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'timeout')
        self.assertLess(time.monotonic() - before, 7)

    def test_paginated_list_keeps_cursor_and_supports_next_page(self):
        client = self.client('pagination')
        result = client.execute('ListMcpResourcesTool', {'server': 'fixture'})
        self.assertEqual(result['result']['nextCursor'], 'page2')
        result = client.execute('ListMcpResourcesTool', {'server': 'fixture', 'cursor': 'page2'})
        self.assertEqual(result['result']['resources'][0]['uri'], 'fixture://page2')
        result = client.execute('ListMcpResourcesTool', {})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['next_cursors'], {'fixture': 'page2'})

    def test_missing_receipt_and_wrong_response_id_are_not_success(self):
        for mode in ('no_receipt', 'wrong_id'):
            result = self.client(mode).execute('ListMcpResourcesTool', {'server': 'fixture'})
            self.assertFalse(result['ok'], result)

    def test_entire_process_group_is_cleaned_up_after_success(self):
        client = self.client('cleanup')
        pid_file = self.root / 'pids'
        client.config['mcp']['fixture']['env'] = {'FIXTURE_PID_PATH': str(pid_file)}
        result = client.execute('ListMcpResourcesTool', {'server': 'fixture'})
        self.assertTrue(result['ok'], result)
        for pid in pid_file.read_text().split():
            stat = Path('/proc') / pid / 'stat'
            for _ in range(20):
                try:
                    state = stat.read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state == 'Z':
                    break
                time.sleep(0.01)
            else:
                self.fail(f'protocol subprocess {pid} remains alive')

    def test_unconfigured_and_missing_dependency_are_explicit(self):
        self.assertIsNotNone(ProtocolTools)
        client = ProtocolTools(self.root, {})
        self.assertFalse(client.availability()['LSP']['available'])
        self.assertEqual(client.execute('ListMcpResourcesTool', {})['status'], 'unconfigured')
        client = ProtocolTools(self.root, {'mcp': {'missing': {'command': ['/nonexistent/server', 'SECRET']}}})
        result = client.execute('ListMcpResourcesTool', {'server': 'missing'})
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('SECRET', json.dumps(result))

if __name__ == '__main__':
    unittest.main()
