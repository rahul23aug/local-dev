"""Private local Git worktrees for a harness-owned copied workspace."""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid


class WorktreeTools:
    MAX_FILES = 10000
    MAX_BYTES = 32 * 1024 * 1024
    MAX_OUTPUT = 512 * 1024

    def __init__(self, root, protected):
        raw = Path(root).absolute()
        if any(path.is_symlink() for path in (raw, *raw.parents)):
            raise ValueError('Workspace symlinks are forbidden')
        self.root = raw.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Workspace must be a directory')
        self.protected = dict(protected)
        self.git = shutil.which('git', path='/usr/bin:/bin')
        token = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        self.metadata = self.root.parent / ('.worktree-' + token + '.json')
        self.directory = self.root.parent / 'worktrees'
        self._record = self._load()

    def _load(self):
        if self.metadata.is_symlink():
            raise ValueError('Worktree metadata symlink forbidden')
        if not self.metadata.exists():
            return None
        if self.metadata.stat().st_size > 16384:
            raise ValueError('Invalid worktree metadata')
        record = json.loads(self.metadata.read_text())
        if not isinstance(record, dict) or record.get('root') != str(self.root) or record.get('version') != 1:
            raise ValueError('Invalid worktree ownership metadata')
        return record

    def _save(self, record):
        if self.metadata.is_symlink():
            raise ValueError('Worktree metadata symlink forbidden')
        fd, name = tempfile.mkstemp(prefix='.worktree-state-', dir=self.root.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.metadata)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self._record = record

    @property
    def active_root(self):
        record = self._load()
        if record and record.get('active'):
            active = Path(record['active'])
            if active.parent != self.directory or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}-[a-f0-9]{12}', active.name):
                raise ValueError('Invalid persisted worktree path')
            if self.directory.is_symlink() or active.is_symlink() or not active.is_dir():
                raise ValueError('Persisted worktree missing or unsafe')
            return active
        return self.root

    def availability(self):
        reason = '' if self.git else 'git executable is unavailable'
        return {name: {'available': bool(self.git), 'reason': reason}
                for name in ('EnterWorktree', 'ExitWorktree')}

    def _git(self, root, *arguments):
        if not self.git:
            raise ValueError('git executable is unavailable')
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.root.parent), 'LANG': 'C.UTF-8',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'Local Coder',
               'GIT_AUTHOR_EMAIL': 'local-coder@localhost', 'GIT_COMMITTER_NAME': 'Local Coder',
               'GIT_COMMITTER_EMAIL': 'local-coder@localhost'}
        argv = [self.git, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                '-C', str(root), *arguments]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        output = bytearray()
        deadline = time.monotonic() + 30
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise ValueError('Git operation timed out')
                    for key, _ in selector.select(min(0.1, deadline - time.monotonic())):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > self.MAX_OUTPUT:
                                raise ValueError('Git output limit exceeded')
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode:
                raise ValueError('Git operation failed: ' + output.decode(errors='replace')[-3000:])
            return output.decode(errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def _check(self, root):
        count, total = 0, 0
        for base, dirs, names in os.walk(root, followlinks=False):
            if Path(base) == root:
                dirs[:] = [name for name in dirs if name != '.git']
                names = [name for name in names if name != '.git']
            for name in dirs + names:
                path = Path(base) / name
                if path.is_symlink():
                    raise ValueError('Workspace symlinks are forbidden')
            for name in names:
                path = Path(base) / name
                if not path.is_file() or path.stat().st_nlink != 1:
                    raise ValueError('Workspace requires regular non-hardlinked files')
                count += 1
                total += path.stat().st_size
                if count > self.MAX_FILES or total > self.MAX_BYTES:
                    raise ValueError('Worktree workspace size limit exceeded')
        for name, digest in self.protected.items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
                raise ValueError('Invalid protected path')
            path = root / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Protected file integrity failed: ' + name)

    def _owned(self):
        gitdir = self.root / '.git'
        if gitdir.is_symlink() or not gitdir.is_dir():
            raise ValueError('Private repository is missing or unsafe')
        if self._record is None:
            raise ValueError('Existing user repository is not a private harness repository')
        if (gitdir / 'config').is_symlink() or (gitdir / 'HEAD').is_symlink():
            raise ValueError('Unsafe private repository metadata')

    def _active_owned(self, active):
        pointer = active / '.git'
        expected = self.root / '.git' / 'worktrees' / active.name
        if pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 4096:
            raise ValueError('Unsafe private worktree Git pointer')
        if pointer.read_text().strip() != 'gitdir: ' + str(expected):
            raise ValueError('Worktree Git pointer escapes private repository')
        if any(path.is_symlink() for path in (expected, *expected.parents)) or not expected.is_dir():
            raise ValueError('Unsafe private worktree ownership')
        common = expected / 'commondir'
        if common.is_symlink() or common.read_text().strip() != '../..':
            raise ValueError('Worktree common repository escapes private ownership')

    def _stage(self, root):
        # The private baseline must contain the complete bounded copied workspace,
        # including inputs a source .gitignore would otherwise omit.
        self._git(root, 'add', '-f', '--all', '--', '.')

    def execute(self, name, args):
        if not isinstance(args, dict):
            raise ValueError('Arguments must be an object')
        self._record = self._load()
        if name == 'EnterWorktree':
            return self._enter(args)
        if name == 'ExitWorktree':
            return self._exit()
        raise ValueError('Unknown worktree tool')

    def _enter(self, args):
        name = args.get('name', 'work')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', name):
            raise ValueError('Worktree name must be a bounded alphanumeric slug')
        if self.active_root != self.root:
            raise ValueError('A worktree is already active')
        self._check(self.root)
        if self.directory.is_symlink():
            raise ValueError('Worktree directory symlink forbidden')
        if (self.root / '.git').exists() or (self.root / '.git').is_symlink():
            self._owned()
            if self._git(self.root, 'status', '--porcelain', '--untracked-files=all').strip():
                raise ValueError('Primary workspace is dirty')
        else:
            self._git(self.root, 'init', '--initial-branch=main', '.')
            self._record = {'version': 1, 'root': str(self.root), 'active': None}
            self._save(self._record)
            self._stage(self.root)
            self._git(self.root, 'commit', '--allow-empty', '-m', 'Private workspace baseline')
        self.directory.mkdir(exist_ok=True, mode=0o700)
        if len(list(self.directory.iterdir())) >= 16:
            raise ValueError('Worktree count limit reached')
        suffix = uuid.uuid4().hex[:12]
        target = self.directory / (name + '-' + suffix)
        branch = 'local-coder/' + suffix
        baseline = self._git(self.root, 'rev-parse', 'HEAD').strip()
        record = {'version': 1, 'root': str(self.root), 'active': str(target),
                  'branch': branch, 'baseline': baseline}
        self._git(self.root, 'worktree', 'add', '-b', branch, str(target), 'HEAD')
        self._save(record)
        self._active_owned(target)
        self._check(target)
        return {'ok': True, 'root': str(target), 'branch': branch, 'baseline': baseline}

    def _exit(self):
        active = self.active_root
        if active == self.root:
            raise ValueError('No worktree is active')
        self._owned()
        self._active_owned(active)
        self._check(active)
        self._check(self.root)
        if self._git(self.root, 'status', '--porcelain', '--untracked-files=all').strip():
            raise ValueError('Primary workspace is dirty; worktree retained')
        if self._git(self.root, 'rev-parse', 'HEAD').strip() != self._record['baseline']:
            raise ValueError('Primary workspace diverged; worktree retained')
        self._stage(active)
        if self._git(active, 'status', '--porcelain', '--untracked-files=all').strip():
            self._git(active, 'commit', '-m', 'Adopt isolated coding changes')
        commit = self._git(active, 'rev-parse', 'HEAD').strip()
        if not re.fullmatch(r'[a-f0-9]{40,64}', commit):
            raise ValueError('Invalid worktree commit')
        changed = self._git(self.root, 'diff', '--name-only', '-z', self._record['baseline'], commit, '--').split('\0')
        changed = [name for name in changed if name]
        self._git(self.root, 'merge', '--ff-only', '--no-edit', commit)
        self._check(self.root)
        record = {'version': 1, 'root': str(self.root), 'active': None,
                  'last_worktree': str(active), 'last_commit': commit}
        self._save(record)
        # Keep the worktree and branch as recoverable artifacts; never force-clean.
        return {'ok': True, 'root': str(self.root), 'changed': changed,
                'commit': commit, 'retained_worktree': str(active)}
