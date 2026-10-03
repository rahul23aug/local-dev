"""Bounded file tools. This is NOT an OS security sandbox."""
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import selectors
import time
import uuid
from .errors import InfrastructureError

MAX_FILE = 256 * 1024
EXCLUDED = {'.git', '.venv', 'node_modules', '__pycache__', '.env'}


def files(root):
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not Path(base, d).is_symlink())
        for name in sorted(names):
            path = Path(base, name)
            if name not in EXCLUDED and not path.is_symlink() and path.is_file():
                yield path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    return {p.relative_to(root).as_posix(): digest(p) for p in files(root)}


class Tools:
    def __init__(self, root, command, protected, timeout=120):
        self.root = Path(root).resolve()
        self.command = command
        self.protected = protected
        self.timeout = timeout

    def path(self, value):
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError('Use a relative workspace path')
        path = self.root / value
        if '..' in Path(value).parts or any(part in EXCLUDED for part in Path(value).parts):
            raise ValueError('Forbidden path')
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Symlinks are not supported')
        if not path.resolve().is_relative_to(self.root): raise ValueError('Path escapes workspace')
        return path

    def execute(self, name, args):
        if name == 'list':
            return {'files': [p.relative_to(self.root).as_posix() for p in files(self.root)][:300]}
        if name == 'verify': return self.verify()
        if name == 'search':
            query = args.get('query')
            if not isinstance(query, str) or not query or len(query) > 200:
                raise ValueError('Search requires a literal query, 1–200 characters')
            hits = []
            for path in files(self.root):
                if path.stat().st_size > MAX_FILE: continue
                for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
                    if query in line:
                        hits.append({'path': path.relative_to(self.root).as_posix(),
                                     'line': number, 'text': line[:300]})
                        if len(hits) >= 50: return {'hits': hits}
            return {'hits': hits}
        if name not in {'read', 'replace', 'write'}: raise ValueError('Unknown tool')
        path = self.path(args.get('path'))
        if path.exists() and path.stat().st_size > MAX_FILE: raise ValueError('File exceeds tool limit')
        if name == 'read':
            start = args.get('start', 1)
            if type(start) is not int or start < 1: raise ValueError('Invalid start line')
            lines = path.read_text().splitlines()
            return {'path': args['path'], 'start': start, 'total_lines': len(lines),
                    'text': '\n'.join(lines[start - 1:start + 79])[:6000]}
        if path.relative_to(self.root).as_posix() in self.protected:
            raise ValueError('Acceptance files are read-only')
        if name == 'replace':
            old, new = args.get('old'), args.get('new')
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise ValueError('Replace requires nonempty old and string new')
            text = path.read_text()
            if text.count(old) != 1: raise ValueError('Old text must match exactly once; read the file')
            text = text.replace(old, new, 1)
        else:
            text = args.get('content')
            if not isinstance(text, str): raise ValueError('Write requires string content')
        if len(text.encode()) > MAX_FILE: raise ValueError('Result exceeds file limit')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as out: out.write(text)
            if path.exists(): os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return {'changed': args['path'], 'sha256': digest(path)}

    def intact(self):
        try:
            return all(digest(self.path(path)) == expected for path, expected in self.protected.items())
        except (OSError, ValueError): return False

    def verify(self):
        try: return self._verify()
        except OSError as exc:
            raise InfrastructureError(f'Verifier infrastructure unavailable: {exc}') from None

    def _verify(self):
        if not self.intact(): return {'passed': False, 'error': 'Acceptance file integrity failed'}
        # Retain raw evidence outside the model-editable workspace. Drain the
        # pipe in bounded chunks and stop floods rather than filling Pi storage.
        directory = self.root.parent / 'evidence'
        directory.mkdir(exist_ok=True)
        log = directory / (uuid.uuid4().hex + '.log')
        with log.open('wb') as output:
            process = subprocess.Popen(self.command, cwd=self.root, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = flooded = False
            size = 0
            deadline = time.monotonic() + self.timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                try:
                    while selector.get_map():
                        if time.monotonic() >= deadline:
                            timed_out = True
                            break
                        for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                            data = os.read(key.fileobj.fileno(), 16384)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            if size + len(data) > 1024 * 1024:
                                flooded = True
                                break
                            output.write(data)
                            size += len(data)
                        if flooded: break
                    if not timed_out and not flooded:
                        try: process.wait(timeout=max(0.01, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired: timed_out = True
                finally:
                    # Also clean up descendants that outlive their test parent.
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()
                    process.stdout.close()
        with log.open('rb') as output:
            output.seek(max(0, size - 6000))
            tail = output.read().decode(errors='replace')
        return {'passed': process.returncode == 0 and not timed_out and not flooded and self.intact(),
                'exit_code': process.returncode, 'timeout': timed_out, 'output_limit': flooded,
                'output': tail, 'output_bytes': size, 'evidence_path': str(log)}
