"""Compatible tool coordinator; fixed owner verifier retains completion authority."""
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import selectors
import time
import uuid
import json
import re
import math
from .catalog import contracts, validate
from .native_tools import NativeTools, FILE_TOOLS
from .protocol_tools import ProtocolTools
from .tool_state import ToolState
from .errors import InfrastructureError

MAX_FILE = 256 * 1024
EXCLUDED = {'.git', '.venv', 'node_modules', '__pycache__', '.env', '.state', '.tools', '.worktrees'}


def files(root):
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.env')
                         and not Path(base, d).is_symlink())
        for name in sorted(names):
            path = Path(base, name)
            if name not in EXCLUDED and not name.startswith('.env') and not path.is_symlink() and path.is_file():
                yield path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    return {p.relative_to(root).as_posix(): digest(p) for p in files(root)}


class Tools:
    def __init__(self, root, command, protected, timeout=120, config=None, run_id=None, agent_callback=None):
        self.root = Path(root).resolve()
        self.command = command
        self.protected = protected
        self.timeout = timeout
        self.config = dict(config or {})
        self.run_id = run_id or hashlib.sha256(str(self.root).encode()).hexdigest()
        self.agent_callback = agent_callback
        self._state = None
        self._worktree = None
        # Dependencies are project-private. Owner overrides only; model Config
        # cannot change commands, network policy, allowlists or verifier.
        pylsp = Path(__file__).resolve().parents[1] / '.venv/bin/pylsp'
        self.protocol_config = dict(self.config)
        if 'lsp' not in self.protocol_config and pylsp.is_file():
            self.protocol_config['lsp'] = {'python': {'command': [str(pylsp)],
                'extensions': ['.py'], 'language_id': 'python'}}

    @property
    def state(self):
        if self._state is None:
            self._state = ToolState(self.root.parent / 'tools.sqlite3', self.run_id)
        return self._state

    @property
    def worktree(self):
        if self._worktree is None:
            from .worktree_tools import WorktreeTools
            self._worktree = WorktreeTools(self.root, self.protected)
        return self._worktree

    @property
    def active_root(self):
        # Metadata is outside the writable copy, so resumed tools retain routing.
        return self.worktree.active_root

    @property
    def native(self):
        native = NativeTools(self.active_root, self.protected, self.timeout,
                             network=bool(self.config.get('shell_network', False)))
        native.evidence_directory = self.root.parent / 'evidence'
        return native

    def close(self):
        if self._state is not None: self._state.close()

    def catalogue(self):
        available = self.native.availability()
        available.update(self.worktree.availability())
        available.update(ProtocolTools(self.active_root, self.protocol_config).availability())
        entries = contracts()
        for entry in entries:
            name = entry['name']
            status = dict(available.get(name, {'available': True, 'reason': ''}))
            status.setdefault('reason', '' if status['available'] else status.get('status', 'unavailable'))
            network = self.config.get('network')
            network_enabled = network is True or isinstance(network, dict) and network.get('enabled') is True
            if name in {'WebFetch', 'WebSearch'} and not network_enabled:
                status = {'available': False, 'reason': 'Owner network policy is disabled'}
            if name in {'Agent', 'Task'} and self.agent_callback is None:
                status = {'available': False, 'reason': 'Requires an active governed Engine run'}
            if name == 'RemoteTrigger' and not self.config.get('remote_triggers'):
                status = {'available': False, 'reason': 'No owner handlers configured'}
            if name == 'call_mcp_tool':
                allowed = any(b.get('allowed_tools') for b in self.config.get('mcp', {}).values())
                if not allowed: status = {'available': False, 'reason': 'No MCP calls owner-allowlisted'}
            entry['availability'] = status
        return entries

    def tick(self):
        if self.state.mode == 'plan': return []
        return self.state.tick(lambda command: self.native.execute('Bash', {'command': command}))

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
        # Legacy checkpoint/fixture names remain compatible, but never expose
        # the old unconstrained shell alias.
        if not isinstance(name, str) or not isinstance(args, dict): raise ValueError('Invalid tool call')
        if name not in {'list', 'search', 'read', 'replace', 'write', 'verify'}:
            canonical = next((t['name'] for t in contracts() if t['name'].lower() == name.lower()), None)
            if canonical is None: raise ValueError('Unknown tool')
            validate(canonical, args)
            return self._canonical(canonical, args)
        if name == 'list':
            return self._canonical('Glob', {'pattern': '**/*'})
        if name == 'verify': return self._canonical('Verify', {})
        if name == 'search':
            query = args.get('query')
            if not isinstance(query, str) or not query or len(query) > 200:
                raise ValueError('Search requires a literal query, 1–200 characters')
            return self._canonical('Grep', {'pattern': re.escape(query)})
        if name not in {'read', 'replace', 'write'}: raise ValueError('Unknown tool')
        self.path(args.get('path'))  # Legacy names remain relative-only.
        if name == 'read':
            start = args.get('start', 1)
            if type(start) is not int or start < 1: raise ValueError('Invalid start line')
            result = self._canonical('Read', {'file_path': args['path'], 'offset': start})
            return dict(result, text=result['content'])
        if name == 'replace':
            return self._canonical('Edit', {'file_path': args['path'], 'old_string': args.get('old'),
                                           'new_string': args.get('new')})
        return self._canonical('Write', {'file_path': args['path'], 'content': args.get('content')})

    def _canonical(self, name, args):
        mutating = {'Write', 'Edit', 'NotebookEdit', 'Bash', 'PowerShell', 'REPL',
                    'Agent', 'Task', 'RemoteTrigger', 'EnterWorktree', 'ExitWorktree',
                    'CronCreate', 'call_mcp_tool', 'read_mcp_resource', 'Verify'}
        if name in mutating and self.state.mode == 'plan':
            raise ValueError('Plan mode is read-only; ExitPlanMode before executing changes')
        if name in FILE_TOOLS | {'Bash', 'PowerShell', 'REPL'}:
            native_args = dict(args)
            if name == 'NotebookEdit':
                native_args['file_path'] = native_args.pop('notebook_path')
                if 'cell_index' in native_args: native_args['cell_number'] = native_args.pop('cell_index')
            return self.native.execute(name, native_args)
        if name in ToolState.TOOL_NAMES:
            return self.state.execute(name, args)
        if name in {'EnterWorktree', 'ExitWorktree'}:
            return self.worktree.execute(name, args)
        if name in {'LSP', 'ListMcpResourcesTool', 'list_mcp_tools', 'read_mcp_resource', 'call_mcp_tool'}:
            if name == 'call_mcp_tool':
                backend = self.config.get('mcp', {}).get(args['server'], {})
                if args['name'] not in backend.get('allowed_tools', []):
                    raise ValueError('MCP tool is not owner-allowlisted')
            return ProtocolTools(self.active_root, self.protocol_config).execute(name, args)
        if name in {'WebFetch', 'WebSearch'}:
            from .network_tools import NetworkTools
            return NetworkTools(self.root, self.config).execute(name, args)
        if name in {'Agent', 'Task'}:
            if self.agent_callback is None: raise ValueError('Agent requires an active Engine run')
            return self.agent_callback(args, self)
        if name == 'RemoteTrigger':
            handler = self.config.get('remote_triggers', {}).get(args['name'])
            if not isinstance(handler, dict): raise ValueError('Unknown owner remote trigger')
            return self.native.run_argv(handler.get('command'),
                input_data=json.dumps(args.get('payload', {}), allow_nan=False))
        if name == 'ToolSearch':
            query = args['query'].strip().lower()
            limit = args.get('limit', 5)
            if not query or type(limit) is not int or not 1 <= limit <= 20: raise ValueError('Invalid search')
            entries = self.catalogue()
            exact = [t for t in entries if t['name'].lower() == query]
            matches = [t for t in entries if query in (t['name'] + ' ' + t['description']).lower()]
            return {'tools': (exact + [t for t in matches if t not in exact])[:limit]}
        if name == 'Skill':
            if not re.fullmatch('[a-z][a-z0-9-]{0,40}', args['skill']): raise ValueError('Invalid skill')
            path = Path(__file__).resolve().parents[1] / 'skills' / args['skill'] / 'SKILL.md'
            if not path.is_file(): raise ValueError('Unknown bundled skill')
            return {'skill': args['skill'], 'content': path.read_text()[:12000]}
        if name == 'Sleep':
            seconds = args.get('seconds', 1)
            if not math.isfinite(seconds) or not 0 <= seconds <= 5: raise ValueError('Sleep limit is five seconds')
            time.sleep(seconds)
            return {'slept_seconds': seconds}
        if name == 'ReadEvidence':
            filename = args['file']
            if not re.fullmatch('(?:[a-f0-9]{32}\\.(?:log|txt|json|html)|network-[a-f0-9]{32}\\.bin)', filename):
                raise ValueError('Invalid evidence filename')
            path = self.root.parent / 'evidence' / filename
            if path.is_symlink(): raise ValueError('Symlinks are forbidden')
            offset, limit = args.get('offset', 0), args.get('limit', 6000)
            if not 0 <= offset <= 1024*1024 or not 1 <= limit <= 6000: raise ValueError('Invalid evidence page')
            with path.open('rb') as evidence:
                evidence.seek(offset)
                text = evidence.read(limit).decode(errors='replace')
            return {'file': filename, 'offset': offset, 'text': text, 'total_bytes': path.stat().st_size}
        if name == 'Verify':
            if self.active_root != self.root: raise ValueError('ExitWorktree before authoritative verification')
            return self.verify()
        if name == 'Finish': raise ValueError('Finish is governed by Engine, not direct tools')
        raise ValueError('Unknown tool')

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
