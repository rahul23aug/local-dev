import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from local_coder.worktree_tools import WorktreeTools


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'
        self.root.mkdir()
        (self.root / 'app.txt').write_text('old')
        (self.root / 'accept.txt').write_text('protected')
        self.protected = {'accept.txt': hashlib.sha256(b'protected').hexdigest()}
        self.tools = WorktreeTools(self.root, self.protected)

    def tearDown(self):
        self.tmp.cleanup()

    def enter(self, name='repair'):
        return self.tools.execute('EnterWorktree', {'name': name})

    def test_real_worktree_change_is_adopted_with_fast_forward(self):
        result = self.enter()
        active = self.tools.active_root
        self.assertNotEqual(active, self.root)
        self.assertTrue((active / '.git').is_file())
        (active / 'app.txt').write_text('new')
        (active / 'extra.txt').write_text('extra')
        result = self.tools.execute('ExitWorktree', {})
        self.assertEqual(self.tools.active_root, self.root)
        self.assertEqual((self.root / 'app.txt').read_text(), 'new')
        self.assertEqual((self.root / 'extra.txt').read_text(), 'extra')
        self.assertIn('app.txt', result['changed'])
        self.assertEqual((self.root / 'accept.txt').read_text(), 'protected')

    def test_metadata_survives_reopening(self):
        self.enter()
        active = self.tools.active_root
        reopened = WorktreeTools(self.root, self.protected)
        self.assertEqual(reopened.active_root, active)
        (active / 'app.txt').write_text('resumed')
        reopened.execute('ExitWorktree', {})
        self.assertEqual((self.root / 'app.txt').read_text(), 'resumed')

    def test_protected_change_is_rejected_without_losing_work(self):
        self.enter()
        active = self.tools.active_root
        (active / 'accept.txt').write_text('tampered')
        with self.assertRaises(ValueError):
            self.tools.execute('ExitWorktree', {})
        self.assertEqual(self.tools.active_root, active)
        self.assertEqual((active / 'accept.txt').read_text(), 'tampered')
        self.assertEqual((self.root / 'accept.txt').read_text(), 'protected')

    def test_dirty_primary_refuses_adoption_and_keeps_worktree(self):
        self.enter()
        active = self.tools.active_root
        (active / 'app.txt').write_text('worktree')
        (self.root / 'app.txt').write_text('primary')
        with self.assertRaises(ValueError):
            self.tools.execute('ExitWorktree', {})
        self.assertEqual((self.root / 'app.txt').read_text(), 'primary')
        self.assertEqual((active / 'app.txt').read_text(), 'worktree')
        self.assertEqual(self.tools.active_root, active)

    def test_names_and_symlink_escapes_rejected(self):
        for name in ('../../escape', '--help', 'x;touch bad', 'x\nother'):
            with self.assertRaises(ValueError):
                self.enter(name)
        (self.root / 'escape').symlink_to(Path(self.tmp.name))
        with self.assertRaises(ValueError):
            self.enter()

    def test_existing_user_git_is_never_modified(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        old = (self.root / '.git' / 'config').read_bytes()
        with self.assertRaises(ValueError):
            self.enter()
        self.assertEqual((self.root / '.git' / 'config').read_bytes(), old)

    def test_availability_and_empty_exit(self):
        self.assertTrue(self.tools.availability()['EnterWorktree']['available'])
        with self.assertRaises(ValueError):
            self.tools.execute('ExitWorktree', {})

    def test_ignored_inputs_are_in_private_baseline(self):
        (self.root / '.gitignore').write_text('input.dat\n')
        (self.root / 'input.dat').write_text('required input')
        self.enter()
        self.assertEqual((self.tools.active_root / 'input.dat').read_text(), 'required input')

    def test_size_guard_refuses_before_git_initialization(self):
        self.tools.MAX_BYTES = 1
        with self.assertRaises(ValueError):
            self.enter()
        self.assertFalse((self.root / '.git').exists())

    def test_diverged_primary_preserves_work(self):
        self.enter()
        active = self.tools.active_root
        (active / 'app.txt').write_text('worktree')
        (self.root / 'app.txt').write_text('primary')
        self.tools._git(self.root, 'add', '--all', '--', '.')
        self.tools._git(self.root, 'commit', '-m', 'primary advanced')
        with self.assertRaises(ValueError):
            self.tools.execute('ExitWorktree', {})
        self.assertEqual(self.tools.active_root, active)
        self.assertEqual((active / 'app.txt').read_text(), 'worktree')

    def test_active_git_pointer_cannot_escape_private_repository(self):
        self.enter()
        active = self.tools.active_root
        other = Path(self.tmp.name) / 'user-source'
        other.mkdir()
        subprocess.run(['git', 'init', '-q', str(other)], check=True)
        (active / '.git').write_text('gitdir: ' + str(other / '.git') + '\n')
        with self.assertRaisesRegex(ValueError, 'private|pointer|ownership'):
            self.tools.execute('ExitWorktree', {})


if __name__ == '__main__':
    unittest.main()
