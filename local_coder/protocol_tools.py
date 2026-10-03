"""Bounded real stdio LSP/MCP clients; backend commands are owner configuration.

Each invocation owns a process group and closes it even when a peer stalls.
No command, environment or timeout supplied in model arguments is accepted.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time
from urllib.parse import unquote, urlparse

MAX_BYTES = 1024 * 1024


class ProtocolFailure(Exception):
    def __init__(self, message, status='protocol_error'):
        super().__init__(message)
        self.status = status


class _Peer:
    def __init__(self, root, backend, kind, timeout):
        self.kind = kind
        self.deadline = time.monotonic() + timeout
        self.buffer = bytearray()
        self.received = 0
        self.next_id = 0
        self.diagnostics = {}
        command = backend.get('command')
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x or '\x00' in x for x in command):
            raise ProtocolFailure('Configured server command must be a nonempty argument list', 'unavailable')
        env = backend.get('env', {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) or '\x00' in k + v for k, v in env.items()):
            raise ProtocolFailure('Configured server environment is invalid', 'unavailable')
        self.secrets = [value for value in env.values() if value] + command[1:]
        try:
            self.proc = subprocess.Popen(command, cwd=root, env={**os.environ, **env}, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except (OSError, ValueError):
            raise ProtocolFailure('Configured server dependency could not be started', 'unavailable') from None
        os.set_blocking(self.proc.stdout.fileno(), False)
        os.set_blocking(self.proc.stdin.fileno(), False)

    def close(self):
        # Kill the entire session even if the direct child has already exited.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()

    def _ready(self, file, event):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolFailure('Configured server timed out', 'timeout')
        with selectors.DefaultSelector() as selector:
            selector.register(file, event)
            if not selector.select(remaining):
                raise ProtocolFailure('Configured server timed out', 'timeout')

    def send(self, message):
        try:
            data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, RecursionError):
            raise ProtocolFailure('Arguments must be JSON serializable', 'invalid_arguments') from None
        if len(data) > MAX_BYTES:
            raise ProtocolFailure('Protocol input exceeds 1 MiB limit', 'invalid_arguments')
        if self.kind == 'lsp':
            data = f'Content-Length: {len(data)}\r\n\r\n'.encode() + data
        else:
            data += b'\n'
        view = memoryview(data)
        while view:
            self._ready(self.proc.stdin, selectors.EVENT_WRITE)
            try:
                count = os.write(self.proc.stdin.fileno(), view)
            except BlockingIOError:
                continue
            except OSError:
                raise ProtocolFailure('Configured server exited before receiving request', 'unavailable') from None
            view = view[count:]

    def receive(self):
        while True:
            data = None
            if self.kind == 'mcp':
                end = self.buffer.find(b'\n')
                if end >= 0:
                    data = bytes(self.buffer[:end])
                    del self.buffer[:end + 1]
            else:
                end = self.buffer.find(b'\r\n\r\n')
                if end >= 0:
                    try:
                        headers = dict(line.split(':', 1) for line in self.buffer[:end].decode('ascii').split('\r\n'))
                        length = int(next(value.strip() for key, value in headers.items() if key.lower() == 'content-length'))
                    except (ValueError, StopIteration, UnicodeError):
                        raise ProtocolFailure('Invalid LSP Content-Length frame') from None
                    if not 0 <= length <= MAX_BYTES:
                        raise ProtocolFailure('Protocol output exceeds 1 MiB limit')
                    if len(self.buffer) >= end + 4 + length:
                        data = bytes(self.buffer[end + 4:end + 4 + length])
                        del self.buffer[:end + 4 + length]
                elif len(self.buffer) > 8192:
                    raise ProtocolFailure('LSP header exceeds limit')
            if data is not None:
                try:
                    msg = json.loads(data)
                except (ValueError, UnicodeError, RecursionError):
                    raise ProtocolFailure('Configured server sent invalid JSON') from None
                if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
                    raise ProtocolFailure('Configured server sent invalid JSON-RPC message')
                return msg
            self._ready(self.proc.stdout, selectors.EVENT_READ)
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise ProtocolFailure('Configured server exited before returning a response', 'unavailable')
            self.received += len(chunk)
            if self.received > MAX_BYTES:
                raise ProtocolFailure('Protocol output exceeds 1 MiB limit')
            self.buffer.extend(chunk)

    def notification(self, method, params):
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params})

    def _notification(self, message):
        if 'method' in message and 'id' in message:
            self.send({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32601, 'message': 'Client method not supported'}})
        elif message.get('method') == 'textDocument/publishDiagnostics':
            params = message.get('params', {})
            self.diagnostics[params.get('uri')] = params.get('diagnostics', [])

    def request(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        while True:
            msg = self.receive()
            if 'method' in msg:
                self._notification(msg)
                continue
            if msg.get('id') != request_id:
                raise ProtocolFailure('Configured server returned an unexpected response id')
            if 'error' in msg:
                error = msg['error']
                if not isinstance(error, dict):
                    raise ProtocolFailure('Configured server returned malformed RPC error')
                message = str(error.get('message', 'Server RPC error'))[:2048]
                for secret in self.secrets:
                    message = message.replace(secret, '[redacted]')
                raise ProtocolFailure(f"RPC error {error.get('code')}: {message}")
            if 'result' not in msg:
                raise ProtocolFailure('Configured server returned no result')
            return msg['result']


class ProtocolTools:
    def __init__(self, root: Path, config: dict):
        self.root = Path(root).resolve()
        self.config = copy.deepcopy(config or {})
        try:
            self.timeout = min(15, max(5, float(self.config.get('timeout', 10))))
        except (ValueError, TypeError):
            self.timeout = 10

    def _backends(self, kind):
        entries = self.config.get(kind, {})
        return entries if isinstance(entries, dict) else {}

    def availability(self):
        result = {}
        for kind, names in [('lsp', ['LSP']), ('mcp', ['ListMcpResourcesTool', 'read_mcp_resource', 'call_mcp_tool', 'list_mcp_tools'])]:
            backends = self._backends(kind)
            ready = []
            for name, backend in backends.items():
                command = backend.get('command') if isinstance(backend, dict) else None
                if isinstance(command, list) and command and isinstance(command[0], str) and shutil.which(command[0]):
                    ready.append(name)
            for name in names:
                result[name] = {'available': bool(ready), 'status': 'configured' if ready else ('unavailable' if backends else 'unconfigured'), 'servers': list(backends)}
        return result

    def execute(self, name, args):
        try:
            if not isinstance(args, dict):
                raise ProtocolFailure('Arguments must be an object', 'invalid_arguments')
            if any(key in args for key in ('command', 'env', 'timeout', 'cwd', 'executable')):
                raise ProtocolFailure('Server configuration may only be supplied by the owner', 'invalid_arguments')
            if len(json.dumps(args, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
                raise ProtocolFailure('Protocol input exceeds 1 MiB limit', 'invalid_arguments')
            result = self._lsp(args) if name == 'LSP' else self._mcp(name, args)
            if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
                raise ProtocolFailure('Protocol output exceeds 1 MiB limit')
            return {'ok': True, 'result': result}
        except ProtocolFailure as error:
            return {'ok': False, 'status': error.status, 'error': str(error)}
        except (TypeError, ValueError, OSError, RecursionError):
            return {'ok': False, 'status': 'invalid_arguments', 'error': 'Invalid protocol arguments or workspace file'}

    def _peer(self, backend, kind):
        if not isinstance(backend, dict):
            raise ProtocolFailure('Configured server backend is invalid', 'unavailable')
        timeout = backend.get('timeout', self.timeout)
        try:
            timeout = min(15, max(5, float(timeout)))
        except (TypeError, ValueError):
            timeout = self.timeout
        return _Peer(self.root, backend, kind, timeout)

    def _path(self, value):
        if not isinstance(value, str) or not value:
            raise ProtocolFailure('file_path is required', 'invalid_arguments')
        path = Path(os.path.abspath(self.root / value))
        if not path.is_relative_to(self.root):
            raise ProtocolFailure('LSP file_path must be inside workspace', 'invalid_arguments')
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise ProtocolFailure('LSP symlink paths are not allowed', 'invalid_arguments')
        return path

    def _safe_uri(self, uri):
        if not isinstance(uri, str):
            return False
        parsed = urlparse(uri)
        if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
            return False
        try:
            self._path(unquote(parsed.path))
            return True
        except ProtocolFailure:
            return False

    def _locations(self, value):
        if isinstance(value, list):
            return [clean for item in value if (clean := self._locations(item)) is not None]
        if isinstance(value, dict):
            if any(key in value and not self._safe_uri(value[key]) for key in ('uri', 'targetUri')):
                return None
            return {key: self._locations(item) for key, item in value.items()}
        return value

    def _lsp(self, args):
        methods = {'hover': 'textDocument/hover', 'definition': 'textDocument/definition', 'references': 'textDocument/references', 'document_symbols': 'textDocument/documentSymbol', 'workspace_symbols': 'workspace/symbol', 'diagnostics': None}
        operation = args.get('operation', args.get('action'))
        if operation not in methods:
            raise ProtocolFailure('Unsupported LSP operation', 'invalid_arguments')
        backends = self._backends('lsp')
        if not backends:
            raise ProtocolFailure('No LSP server configured by owner', 'unconfigured')
        path = self._path(args.get('file_path'))
        selected = next(((key, value) for key, value in backends.items() if isinstance(value, dict) and path.suffix in value.get('extensions', [])), None)
        if selected is None:
            raise ProtocolFailure('No configured LSP server supports this file extension', 'unavailable')
        language, backend = selected
        if path.stat().st_size > MAX_BYTES // 2:
            raise ProtocolFailure('LSP document exceeds input size limit', 'invalid_arguments')
        text = path.read_text(encoding='utf-8')
        line, character = args.get('line', 1), args.get('character', 0)
        if type(line) is not int or type(character) is not int or line < 1 or character < 0:
            raise ProtocolFailure('LSP line must be >= 1 and character >= 0', 'invalid_arguments')
        peer = self._peer(backend, 'lsp')
        try:
            peer.request('initialize', {'processId': os.getpid(), 'rootUri': self.root.as_uri(), 'capabilities': {}, 'workspaceFolders': [{'uri': self.root.as_uri(), 'name': self.root.name}]})
            peer.notification('initialized', {})
            peer.notification('textDocument/didOpen', {'textDocument': {'uri': path.as_uri(), 'languageId': backend.get('language_id', language), 'version': 1, 'text': text}})
            if operation == 'diagnostics':
                while path.as_uri() not in peer.diagnostics:
                    peer._notification(peer.receive())
                return self._locations(peer.diagnostics[path.as_uri()])
            params = {'textDocument': {'uri': path.as_uri()}}
            if operation in ('hover', 'definition', 'references'):
                params['position'] = {'line': line - 1, 'character': character}
            if operation == 'references':
                params['context'] = {'includeDeclaration': True}
            if operation == 'workspace_symbols':
                params = {'query': args.get('query', '')}
            return self._locations(peer.request(methods[operation], params))
        finally:
            peer.close()

    def _mcp(self, name, args):
        methods = {'ListMcpResourcesTool': 'resources/list', 'read_mcp_resource': 'resources/read', 'call_mcp_tool': 'tools/call', 'list_mcp_tools': 'tools/list'}
        if name not in methods:
            raise ProtocolFailure('Unknown protocol tool', 'invalid_arguments')
        backends = self._backends('mcp')
        if not backends:
            raise ProtocolFailure('No MCP server configured by owner', 'unconfigured')
        server = args.get('server')
        listing = name in ('ListMcpResourcesTool', 'list_mcp_tools')
        if server is None and not listing:
            raise ProtocolFailure('Configured MCP server name is required', 'invalid_arguments')
        if server is not None and server not in backends:
            raise ProtocolFailure('Requested MCP server is not configured by owner', 'unconfigured')
        selected = [server] if server is not None else list(backends)
        params = {}
        if listing and 'cursor' in args:
            if server is None or not isinstance(args['cursor'], str):
                raise ProtocolFailure('Listing cursor requires a configured server', 'invalid_arguments')
            params['cursor'] = args['cursor']
        if name == 'read_mcp_resource':
            if not isinstance(args.get('uri'), str) or not args['uri']:
                raise ProtocolFailure('Resource uri is required', 'invalid_arguments')
            params = {'uri': args['uri']}
        elif name == 'call_mcp_tool':
            if not isinstance(args.get('name'), str) or not args['name'] or not isinstance(args.get('arguments', {}), dict):
                raise ProtocolFailure('Tool name and arguments object are required', 'invalid_arguments')
            params = {'name': args['name'], 'arguments': args.get('arguments', {})}
        results = []
        cursors = {}
        for server_name in selected:
            peer = self._peer(backends[server_name], 'mcp')
            try:
                init = peer.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'local-coder', 'version': '1.0'}})
                if not isinstance(init, dict) or init.get('protocolVersion') != '2025-06-18':
                    raise ProtocolFailure('MCP server negotiated an unsupported protocol version')
                peer.notification('notifications/initialized', {})
                result = peer.request(methods[name], params)
                if not isinstance(result, dict):
                    raise ProtocolFailure('MCP server returned a non-object result')
                if listing and server is None:
                    key = 'resources' if name == 'ListMcpResourcesTool' else 'tools'
                    for item in result.get(key, []):
                        if isinstance(item, dict):
                            results.append({**item, 'server': server_name})
                    if result.get('nextCursor'):
                        cursors[server_name] = result['nextCursor']
                else:
                    return result
            finally:
                peer.close()
        result = {'resources' if name == 'ListMcpResourcesTool' else 'tools': results}
        if cursors:
            result['next_cursors'] = cursors
        return result
