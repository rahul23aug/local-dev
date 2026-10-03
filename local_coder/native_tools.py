"""Native compatible coding tools; all execution is fail-closed bubblewrap."""
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

from .errors import InfrastructureError

MAX_OUTPUT = 1024 * 1024
TAIL = 6144
FILE_TOOLS = {'Read', 'Write', 'Edit', 'Glob', 'Grep', 'NotebookEdit'}
EXEC_PATH = '/usr/local/bin:/usr/bin:/bin'
EXCLUDED = {'node_modules', '__pycache__'}


class NativeTools:
    def __init__(self, root: Path, protected: dict, timeout=120, network=False):
        self.root = Path(root).resolve(strict=True)
        self.protected = dict(protected)
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        self.network = bool(network)
        self.runtime = Path(__file__).parent / 'runtime'
        self.evidence_directory = self.root.parent / 'evidence'
        self._sandbox_ok = None

    def _path(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError('A workspace path is required')
        raw = Path(value)
        if raw.is_absolute():
            try: raw = raw.relative_to(self.root)
            except ValueError: raise ValueError('Path escapes workspace') from None
        if '..' in raw.parts or any(part in EXCLUDED or (part.startswith('.') and part != '.') for part in raw.parts):
            raise ValueError('Hidden and parent paths are forbidden')
        result = self.root / raw
        current = self.root
        for part in raw.parts:
            current = current / part
            if current.is_symlink(): raise ValueError('Symlinks are forbidden')
        if not result.resolve().is_relative_to(self.root):
            raise ValueError('Path escapes workspace')
        return result

    def _pwsh(self):
        trusted = Path(__file__).resolve().parents[1] / '.tools' / 'pwsh' / 'pwsh'
        if trusted.is_file() and os.access(trusted, os.X_OK): return str(trusted.resolve())
        installed = shutil.which('pwsh', path=EXEC_PATH) or shutil.which('powershell', path=EXEC_PATH)
        if installed: return str(Path(installed).resolve())
        local = self.root / '.tools' / 'pwsh' / 'pwsh'
        if local.is_file() and os.access(local, os.X_OK): return str(local.resolve())
        return None

    def _mounts(self, cwd, passwd_fd=None):
        bwrap = shutil.which('bwrap')
        if not bwrap: raise InfrastructureError('bubblewrap is required; no unsandboxed fallback')
        cmd = [bwrap, '--die-with-parent', '--new-session', '--unshare-user', '--unshare-pid',
               '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL']
        if not self.network: cmd += ['--unshare-net']
        cmd += ['--ro-bind', '/usr', '/usr']
        for name in ('bin', 'sbin', 'lib', 'lib64'):
            host = Path('/') / name
            if host.is_symlink(): cmd += ['--symlink', os.readlink(host), '/' + name]
            elif host.exists(): cmd += ['--ro-bind', str(host), str(host)]
        cmd += ['--dir', '/etc']
        if passwd_fd is not None:
            cmd += ['--ro-bind-data', str(passwd_fd), '/etc/passwd']
        for name in ('ld.so.cache', 'localtime'):
            host = Path('/etc') / name
            if host.exists(): cmd += ['--ro-bind', str(host.resolve()), str(host)]
        if self.network:
            for name in ('resolv.conf', 'hosts', 'nsswitch.conf', 'ssl/certs'):
                host = Path('/etc') / name
                if host.exists(): cmd += ['--ro-bind', str(host.resolve()), str(host)]
        cmd += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/home',
                '--dir', '/home/worker', '--bind', str(self.root), '/workspace',
                '--ro-bind', str(self.runtime), '/opt/native-runtime', '--clearenv',
                '--setenv', 'PATH', EXEC_PATH, '--setenv', 'HOME', '/home/worker',
                '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'TMPDIR', '/tmp']
        anchored = set()
        for name in self.protected:
            p = self._path(name)
            if not p.is_file(): raise InfrastructureError('Protected file missing: ' + name)
            if p.stat().st_nlink != 1:
                raise InfrastructureError('Protected files must not have hardlink aliases: ' + name)
            # The file's read-only mount protects its inode; anchoring ancestors
            # as mountpoints also prevents rename/replacement of its pathname.
            ancestors = list(p.parent.relative_to(self.root).parents)
            relative_parent = p.parent.relative_to(self.root)
            for relative in reversed([relative_parent, *ancestors]):
                if relative == Path('.') or relative in anchored: continue
                cmd += ['--bind', str(self.root / relative), '/workspace/' + relative.as_posix()]
                anchored.add(relative)
        if self.runtime.resolve().is_relative_to(self.root):
            relative_runtime = self.runtime.resolve().relative_to(self.root)
            for relative in reversed([relative_runtime.parent, *relative_runtime.parent.parents]):
                if relative == Path('.') or relative in anchored: continue
                cmd += ['--bind', str(self.root / relative), '/workspace/' + relative.as_posix()]
                anchored.add(relative)
            cmd += ['--ro-bind', str(self.runtime), '/workspace/' + relative_runtime.as_posix()]
        # Mask after ancestor bindings, so remounting a protected parent cannot
        # reintroduce its original hidden host entries.
        for base, dirs, names in os.walk(self.root, followlinks=False):
            for name in dirs[:] + names:
                p = Path(base) / name
                if name.startswith('.'):
                    target = '/workspace/' + p.relative_to(self.root).as_posix()
                    if p.is_dir() and not p.is_symlink():
                        cmd += ['--tmpfs', target, '--remount-ro', target]
                    else: cmd += ['--ro-bind', '/dev/null', target]
            dirs[:] = [d for d in dirs if not d.startswith('.') and not (Path(base) / d).is_symlink()]
        for name in self.protected:
            p = self._path(name)
            cmd += ['--ro-bind', str(p), '/workspace/' + p.relative_to(self.root).as_posix()]
        pwsh = self._pwsh()
        if pwsh and not str(Path(pwsh).resolve()).startswith('/usr/'):
            cmd += ['--ro-bind', str(Path(pwsh).parent.resolve()), '/opt/pwsh']
        return cmd + ['--chdir', '/workspace' + ('/' + cwd if cwd != '.' else ''), '--']

    def _check_sandbox(self):
        if self._sandbox_ok: return
        try:
            probe = subprocess.run(self._mounts('.') + ['/bin/true'], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=5, env={'PATH': '/usr/bin:/bin'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfrastructureError(f'Sandbox unavailable: {exc}') from None
        if probe.returncode:
            raise InfrastructureError('Sandbox unavailable: ' + probe.stderr.decode(errors='replace')[-1000:])
        self._sandbox_ok = True

    def availability(self):
        try:
            self._check_sandbox()
            reason = ''
        except InfrastructureError as exc:
            reason = str(exc)
        node, rg = shutil.which('node', path=EXEC_PATH), shutil.which('rg', path=EXEC_PATH)
        result = {}
        for name in sorted(FILE_TOOLS | {'Bash', 'REPL', 'PowerShell'}):
            why = reason
            if not why and name in FILE_TOOLS | {'REPL'} and not node: why = 'node executable is missing'
            if not why and name in {'Grep', 'Glob'} and not rg: why = 'ripgrep executable is missing'
            if not why and name == 'PowerShell' and not self._pwsh(): why = 'PowerShell executable is not installed'
            result[name] = {'available': not bool(why), 'reason': why}
        return result

    def _integrity(self):
        try:
            return all(hashlib.sha256(self._path(p).read_bytes()).hexdigest() == h for p, h in self.protected.items())
        except (OSError, ValueError): return False

    def _executable(self, argv):
        if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, str) or '\0' in a for a in argv):
            raise ValueError('Command requires an argv string array')
        first = argv[0]
        if first.startswith('/workspace/') or first.startswith('/opt/'):
            return list(argv)
        candidate = shutil.which(first, path=EXEC_PATH) if '/' not in first else first
        if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
            raise InfrastructureError('Executable unavailable: ' + first)
        path = Path(candidate).resolve()
        if path.is_relative_to(self.root):
            candidate = '/workspace/' + path.relative_to(self.root).as_posix()
        elif not path.is_relative_to('/usr'):
            pwsh = self._pwsh()
            if pwsh and path == Path(pwsh).resolve(): candidate = '/opt/pwsh/' + path.name
            else: raise InfrastructureError('Executable is not exposed in sandbox: ' + first)
        return [candidate, *argv[1:]]

    def run_argv(self, argv, cwd=None, timeout=None, input_data=None):
        if isinstance(input_data, str): input_data = input_data.encode()
        if input_data is not None and not isinstance(input_data, bytes):
            raise ValueError('input_data must be a string or bytes')
        return self._run(argv, cwd, timeout, input_data=input_data)

    def _run(self, argv, cwd=None, timeout=None, input_data=None, full=False):
        cwd_path = self._path(cwd or '.')
        if not cwd_path.is_dir(): raise ValueError('cwd must be a directory')
        duration = self.timeout if timeout is None else min(self.timeout, float(timeout))
        if not math.isfinite(duration) or duration <= 0: raise ValueError('timeout must be finite and positive')
        argv = self._executable(argv)
        self._check_sandbox()
        if not self._integrity(): raise InfrastructureError('Protected file integrity failed')
        directory = self.evidence_directory
        directory.mkdir(exist_ok=True)
        log = directory / (uuid.uuid4().hex + '.log')
        with tempfile.TemporaryFile() as request, tempfile.TemporaryFile() as identity, log.open('wb') as output:
            # .NET/PowerShell calls getpwuid during initialization. Supply only
            # the current sandbox identity, never the host account database.
            identity.write(f'worker:x:{os.getuid()}:{os.getgid()}:Sandbox worker:/home/worker:/bin/bash\n'.encode())
            identity.seek(0)
            if input_data is not None:
                request.write(input_data)
                request.seek(0)
            try:
                process = subprocess.Popen(self._mounts(cwd_path.relative_to(self.root).as_posix(), identity.fileno()) + argv,
                                           stdin=request, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           start_new_session=True, env={'PATH': '/usr/bin:/bin'},
                                           pass_fds=(identity.fileno(),))
            except OSError as exc: raise InfrastructureError(f'Sandbox process unavailable: {exc}') from None
            timed_out = flooded = False
            size = 0
            deadline = time.monotonic() + duration
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                try:
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        for key, _ in selector.select(min(.1, remaining)):
                            data = os.read(key.fileobj.fileno(), 16384)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            keep = data[:MAX_OUTPUT - size]
                            output.write(keep)
                            size += len(keep)
                            if len(keep) != len(data):
                                flooded = True
                                break
                        if flooded: break
                    if not timed_out and not flooded:
                        try: process.wait(timeout=max(.01, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired: timed_out = True
                finally:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()
                    process.stdout.close()
        with log.open('rb') as output:
            output.seek(0 if full else max(0, size - TAIL))
            tail = output.read().decode(errors='replace' if full else 'ignore')
        if not timed_out and not flooded and tail.startswith('bwrap:'):
            raise InfrastructureError('Sandbox setup failed: ' + tail[-1000:])
        return {'passed': process.returncode == 0 and not timed_out and not flooded and self._integrity(),
                'exit_code': process.returncode, 'timeout': timed_out, 'output_limit': flooded,
                'output': tail, 'output_bytes': size, 'evidence_path': str(log)}

    def execute(self, name, args):
        if not isinstance(args, dict): raise ValueError('Tool arguments must be an object')
        if name in FILE_TOOLS:
            args = dict(args)
            key = 'file_path' if name not in {'Glob', 'Grep'} else 'path'
            path = self._path(args.get(key, '.' if key == 'path' else None))
            relative = path.relative_to(self.root).as_posix()
            if name in {'Write', 'Edit', 'NotebookEdit'} and relative in self.protected:
                raise ValueError('Acceptance files are read-only')
            if name in {'Write', 'Edit', 'NotebookEdit'} and path.resolve().is_relative_to(self.runtime.resolve()):
                raise ValueError('Native runtime files are read-only')
            args[key] = relative
            request = json.dumps({'name': name, 'args': args, 'protected': list(self.protected)}).encode()
            result = self._run(['node', '/opt/native-runtime/worker.cjs'], input_data=request, full=True)
            if not result['passed']: raise InfrastructureError('Native worker failed: ' + result['output'][-2000:])
            try: response = json.loads(result['output'])
            except json.JSONDecodeError: raise InfrastructureError('Invalid native worker response') from None
            if 'error' in response:
                if response.get('infrastructure'): raise InfrastructureError(response['error'])
                raise ValueError(response['error'])
            return response
        if name == 'Bash':
            if not isinstance(args.get('command'), str): raise ValueError('command must be a string')
            milliseconds = args.get('timeout', args.get('timeout_ms'))
            return self.run_argv(['/bin/bash', '-c', args['command']], args.get('cwd'),
                                 float(milliseconds) / 1000 if milliseconds is not None else None)
        if name == 'REPL':
            if not isinstance(args.get('code'), str): raise ValueError('code must be a string')
            return self.run_argv(['node', '-e', args['code']], args.get('cwd'))
        if name == 'PowerShell':
            executable = self._pwsh()
            if not executable: raise InfrastructureError('PowerShell executable is not installed')
            if not isinstance(args.get('command'), str): raise ValueError('command must be a string')
            return self.run_argv([executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', args['command']], args.get('cwd'))
        raise ValueError('Unknown native tool: ' + str(name))
