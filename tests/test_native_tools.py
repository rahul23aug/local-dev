import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from local_coder.errors import InfrastructureError


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        from local_coder.native_tools import NativeTools
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'
        self.root.mkdir()
        (self.root / 'accept.py').write_text('original')
        self.tools = NativeTools(self.root, {'accept.py': hashlib.sha256(b'original').hexdigest()}, timeout=4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_write_edit(self):
        self.assertEqual(self.tools.execute('Write', {'file_path': 'sub/a.txt', 'content': 'one\ntwo\none'})['changed'], 'sub/a.txt')
        self.assertEqual(self.tools.execute('Read', {'file_path': str(self.root / 'sub/a.txt'), 'offset': 2, 'limit': 1})['content'], 'two')
        with self.assertRaises(ValueError):
            self.tools.execute('Edit', {'file_path': 'sub/a.txt', 'old_string': 'one', 'new_string': 'x'})
        self.tools.execute('Edit', {'file_path': 'sub/a.txt', 'old_string': 'one', 'new_string': 'x', 'replace_all': True})
        self.assertEqual((self.root / 'sub/a.txt').read_text(), 'x\ntwo\nx')

    def test_paths_protection_and_bounds(self):
        (self.root / 'link').symlink_to('/etc/passwd')
        for path in ['../outside', '/etc/passwd', '.env', '.git/config', 'node_modules/x', 'link', 'accept.py']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.tools.execute('Write', {'file_path': path, 'content': 'bad'})
        (self.root / 'big').write_bytes(b'a' * (256 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.tools.execute('Read', {'file_path': 'big'})

    def test_search_no_shell_injection_and_no_matches(self):
        (self.root / 'a.txt').write_text('needle\n')
        (self.root / '.env').write_text('needle')
        self.assertEqual(self.tools.execute('Glob', {'pattern': '*.txt'})['files'], ['a.txt'])
        self.assertTrue(self.tools.execute('Grep', {'pattern': 'needle'})['matches'])
        self.assertEqual(self.tools.execute('Grep', {'pattern': 'missing'})['matches'], [])
        self.tools.execute('Grep', {'pattern': '$(touch injected)'})
        self.assertFalse((self.root / 'injected').exists())

    def test_notebook_cells(self):
        p = self.root / 'test.ipynb'
        p.write_text(json.dumps({'nbformat': 4, 'nbformat_minor': 5, 'metadata': {}, 'cells': [{'id': 'a', 'cell_type': 'code', 'source': ['old'], 'metadata': {}, 'outputs': [], 'execution_count': 1}]}))
        self.tools.execute('NotebookEdit', {'file_path': 'test.ipynb', 'cell_id': 'a', 'new_source': 'print(1)\n'})
        cell = json.loads(p.read_text())['cells'][0]
        self.assertEqual(''.join(cell['source']), 'print(1)\n')
        self.assertEqual(cell['outputs'], [])
        self.assertIsNone(cell['execution_count'])
        self.tools.execute('NotebookEdit', {'file_path': 'test.ipynb', 'cell_number': 1, 'new_source': 'hello', 'cell_type': 'markdown', 'edit_mode': 'insert'})
        self.assertEqual(json.loads(p.read_text())['cells'][1]['cell_type'], 'markdown')
        self.tools.execute('NotebookEdit', {'file_path': 'test.ipynb', 'cell_number': 0, 'new_source': '', 'edit_mode': 'delete'})
        self.assertEqual(len(json.loads(p.read_text())['cells']), 1)

    def test_sandbox_and_real_execution(self):
        result = self.tools.execute('Bash', {'command': 'printf hello; test ! -e /home/rahul; test ! -r /etc/shadow'})
        self.assertTrue(result['passed'], result)
        self.assertIn('hello', result['output'])
        result = self.tools.execute('Bash', {'command': 'printf hacked > accept.py'})
        self.assertFalse(result['passed'])
        self.assertEqual((self.root / 'accept.py').read_text(), 'original')
        result = self.tools.execute('REPL', {'code': 'console.log(2 + 3)'})
        self.assertTrue(result['passed'], result)
        self.assertIn('5', result['output'])

    def test_timeout_flood_and_missing_executable(self):
        started = time.monotonic()
        result = self.tools.execute('Bash', {'command': 'sleep 20 & wait', 'timeout_ms': 150})
        self.assertTrue(result['timeout'])
        self.assertLess(time.monotonic() - started, 3)
        result = self.tools.execute('Bash', {'command': 'yes x'})
        self.assertTrue(result['output_limit'])
        self.assertLessEqual(Path(result['evidence_path']).stat().st_size, 1024 * 1024)
        self.assertLessEqual(len(result['output'].encode()), 6144)
        with self.assertRaises(InfrastructureError):
            self.tools.run_argv(['nonexistent-executable-123'])

    def test_owner_command_stdin_and_exit_code(self):
        result = self.tools.run_argv(['/usr/bin/python3', '-c', 'import sys; print(sys.stdin.read()); sys.exit(7)'], input_data='owner payload')
        self.assertFalse(result['passed'])
        self.assertEqual(result['exit_code'], 7)
        self.assertIn('owner payload', result['output'])

    def test_hidden_entries_not_visible_to_shell(self):
        (self.root / '.state').mkdir()
        (self.root / '.state' / 'secret').write_text('secret')
        (self.root / '.env.production').write_text('secret')
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / '.tools').mkdir()
        (self.root / 'sub' / '.tools' / 'secret').write_text('secret')
        result = self.tools.execute('Bash', {'command': 'test ! -s .env.production && test ! -e .state/secret && test ! -e sub/.tools/secret'})
        self.assertTrue(result['passed'], result)

    def test_protected_parent_cannot_be_renamed(self):
        from local_coder.native_tools import NativeTools
        (self.root / 'acceptance').mkdir()
        (self.root / 'acceptance' / 'check.py').write_text('original')
        tools = NativeTools(self.root, {'acceptance/check.py': hashlib.sha256(b'original').hexdigest()})
        result = tools.execute('Bash', {'command': 'mv acceptance moved; mkdir -p acceptance; printf hacked > acceptance/check.py'})
        self.assertFalse(result['passed'])
        self.assertEqual((self.root / 'acceptance' / 'check.py').read_text(), 'original')

    def test_protected_hardlink_rejected(self):
        import os
        os.link(self.root / 'accept.py', self.root / 'alias.py')
        with self.assertRaises(InfrastructureError):
            self.tools.execute('Bash', {'command': 'printf hacked > alias.py'})

    def test_hidden_files_in_protected_parent_stay_hidden(self):
        from local_coder.native_tools import NativeTools
        (self.root / 'checks').mkdir()
        (self.root / 'checks' / 'accept.py').write_text('original')
        (self.root / 'checks' / '.env').write_text('private')
        tools = NativeTools(self.root, {'checks/accept.py': hashlib.sha256(b'original').hexdigest()})
        result = tools.execute('Bash', {'command': 'test ! -s checks/.env'})
        self.assertTrue(result['passed'], result)

    def test_timeout_kills_descendant(self):
        result = self.tools.execute('Bash', {'command': '(sleep .5; touch survived) & wait', 'timeout_ms': 100})
        self.assertTrue(result['timeout'])
        time.sleep(.6)
        self.assertFalse((self.root / 'survived').exists())

    def test_unicode_and_large_write_bounded(self):
        self.tools.execute('Write', {'file_path': 'unicode', 'content': '🙂' * 40000})
        result = self.tools.execute('Read', {'file_path': 'unicode'})
        self.assertLessEqual(len(result['content'].encode()), 6000)
        self.assertTrue(result['truncated'])

    def test_missing_powershell_and_sandbox_fail_closed(self):
        from unittest.mock import patch
        with patch.object(self.tools, '_pwsh', return_value=None):
            self.assertFalse(self.tools.availability()['PowerShell']['available'])
            with self.assertRaises(InfrastructureError):
                self.tools.execute('PowerShell', {'command': 'hello'})
        self.tools._sandbox_ok = None
        with patch('local_coder.native_tools.shutil.which', return_value=None):
            with self.assertRaises(InfrastructureError):
                self.tools.execute('Bash', {'command': 'touch escaped'})
        self.assertFalse((self.root / 'escaped').exists())

    def test_private_network_and_pid_namespaces(self):
        import os
        result = self.tools.run_argv(['/usr/bin/python3', '-c', 'import os; print(os.stat("/proc/self/ns/net").st_ino); print(os.stat("/proc/self/ns/pid").st_ino)'])
        self.assertTrue(result['passed'], result)
        net, pid = map(int, result['output'].splitlines())
        self.assertNotEqual(net, os.stat('/proc/self/ns/net').st_ino)
        self.assertNotEqual(pid, os.stat('/proc/self/ns/pid').st_ino)

    def test_python_available_by_name(self):
        result = self.tools.run_argv(['python', '-c', 'print("real python")'])
        self.assertTrue(result['passed'], result)
        self.assertIn('real python', result['output'])

    def test_runtime_inside_workspace_cannot_be_modified(self):
        import shutil
        shutil.copytree(self.tools.runtime, self.root / 'runtime')
        self.tools.runtime = self.root / 'runtime'
        original = (self.tools.runtime / 'core.cjs').read_text()
        with self.assertRaises(ValueError):
            self.tools.execute('Write', {'file_path': 'runtime/core.cjs', 'content': 'corrupt'})
        result = self.tools.execute('Bash', {'command': 'printf corrupt > runtime/core.cjs'})
        self.assertFalse(result['passed'])
        self.assertEqual((self.tools.runtime / 'core.cjs').read_text(), original)

    def test_trusted_project_powershell_preferred(self):
        from unittest.mock import patch
        trusted = Path(self.tmp.name) / 'trusted'
        binary = trusted / '.tools' / 'pwsh' / 'pwsh'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        workspace_binary = self.root / '.tools' / 'pwsh' / 'pwsh'
        workspace_binary.parent.mkdir(parents=True)
        workspace_binary.write_text('#!/bin/sh\nexit 1\n')
        workspace_binary.chmod(0o755)
        with patch('local_coder.native_tools.__file__', str(trusted / 'local_coder' / 'native_tools.py')):
            self.assertEqual(self.tools._pwsh(), str(binary))

    def test_grep_modes_case_line_flags_and_head_limit(self):
        (self.root / 'first.txt').write_text('Needle\nneedle\n')
        (self.root / 'second.txt').write_text('needle\n')
        result = self.tools.execute('Grep', {'pattern': 'NEEDLE', '-i': True, '-n': False, 'head_limit': 1, 'output_mode': 'content'})
        self.assertEqual(result['matches'], ['first.txt:Needle'])
        self.assertTrue(result['truncated'])
        files = self.tools.execute('Grep', {'pattern': 'needle', '-i': True, 'output_mode': 'files_with_matches'})
        self.assertEqual(sorted(files['files']), ['first.txt', 'second.txt'])
        counts = self.tools.execute('Grep', {'pattern': 'needle', '-i': True, 'output_mode': 'count'})
        self.assertEqual(sorted(counts['counts'], key=lambda x: x['file_path']), [{'file_path': 'first.txt', 'count': 2}, {'file_path': 'second.txt', 'count': 1}])
        result = self.tools.execute('Grep', {'pattern': 'Needle', '-n': True})
        self.assertEqual(result['matches'], ['first.txt:1:Needle'])

    def test_grep_multiline_and_invalid_options(self):
        (self.root / 'a.txt').write_text('start\nfinish\n')
        result = self.tools.execute('Grep', {'pattern': 'start.*finish', 'multiline': True})
        self.assertIn('start\nfinish', result['content'])
        self.assertEqual(self.tools.execute('Grep', {'pattern': 'start.*finish'})['matches'], [])
        for kwargs in [{'output_mode': 'unknown'}, {'head_limit': -1}, {'-i': 'yes'}, {'-n': 'yes'}, {'multiline': 'yes'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.tools.execute('Grep', {'pattern': 'start', **kwargs})

    def test_canonical_bash_timeout_milliseconds(self):
        started = time.monotonic()
        result = self.tools.execute('Bash', {'command': 'sleep 20', 'timeout': 100})
        self.assertTrue(result['timeout'])
        self.assertLess(time.monotonic() - started, 3)

    def test_real_powershell_with_private_identity(self):
        if not self.tools._pwsh(): self.skipTest('PowerShell is not installed')
        # Cold .NET/module initialization can exceed the short flood-test limit.
        self.tools.timeout = 15
        result = self.tools.execute('PowerShell', {'command': 'Write-Output "native-powershell"; if (Test-Path /home/rahul) { exit 9 }; if (Test-Path /etc/shadow) { exit 10 }'})
        self.assertTrue(result['passed'], result)
        self.assertIn('native-powershell', result['output'])
        result = self.tools.run_argv(['/usr/bin/python3', '-c', 'import pwd, os; p=pwd.getpwuid(os.getuid()); print(p.pw_name); print(p.pw_dir); print(len(pwd.getpwall()))'])
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['output'].splitlines(), ['worker', '/home/worker', '1'])


if __name__ == '__main__':
    unittest.main()
