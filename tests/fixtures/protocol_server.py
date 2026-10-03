"""Real stdio peer used to exercise local protocol tools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

kind, mode = sys.argv[1:3]
initialized = opened = False

def read():
    if kind == 'mcp':
        line = sys.stdin.buffer.readline()
        return json.loads(line) if line else None
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b'\r\n':
            break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers['content-length'])))

def send(message):
    payload = json.dumps(message).encode()
    if kind == 'lsp':
        sys.stdout.buffer.write(f'Content-Length: {len(payload)}\r\n\r\n'.encode())
    sys.stdout.buffer.write(payload + (b'\n' if kind == 'mcp' else b''))
    sys.stdout.buffer.flush()

while True:
    msg = read()
    if msg is None:
        break
    method = msg.get('method')
    if method == 'initialize':
        if mode == 'cleanup':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            Path(os.environ['FIXTURE_PID_PATH']).write_text(f'{os.getpid()} {child.pid}')
        if mode == 'dead':
            sys.exit(3)
        if mode == 'timeout':
            time.sleep(60)
        if mode == 'flood':
            sys.stdout.buffer.write(b'x' * (1024 * 1024 + 10))
            sys.stdout.buffer.flush()
            time.sleep(60)
        if mode == 'wrong_id':
            send({'jsonrpc': '2.0', 'id': 900, 'result': {}})
            continue
        if mode == 'no_receipt':
            sys.stdin.close()
            sys.exit(0)
        result = {'capabilities': {'textDocumentSync': 1}, 'serverInfo': {'name': 'fixture'}}
        if kind == 'mcp':
            assert msg['params']['protocolVersion'] == '2025-06-18'
            result['protocolVersion'] = '2025-06-18'
        else:
            assert msg['params']['rootUri'].startswith('file://')
        send({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})
        continue
    if method in ('initialized', 'notifications/initialized'):
        initialized = True
        continue
    if method == 'textDocument/didOpen':
        opened = True
        uri = msg['params']['textDocument']['uri']
        send({'jsonrpc': '2.0', 'method': 'textDocument/publishDiagnostics', 'params': {'uri': uri, 'diagnostics': [{'message': 'fixture diagnostic'}]}})
        continue
    if 'id' not in msg:
        continue
    assert initialized
    if mode == 'error':
        send({'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32602, 'message': 'fixture rejected request'}})
        continue
    send({'jsonrpc': '2.0', 'id': 'server-question', 'method': 'fixture/unsupported', 'params': {}})
    answer = read()
    assert answer['error']['code'] == -32601
    params = msg.get('params', {})
    if kind == 'lsp':
        if method != 'workspace/symbol':
            assert opened
        if method in ('textDocument/definition', 'textDocument/references'):
            result = [{'uri': uri, 'range': {'start': params['position'], 'end': params['position']}}, {'uri': 'file:///etc/passwd', 'range': {}}]
        elif method == 'textDocument/hover':
            result = {'contents': {'kind': 'plaintext', 'value': 'actual hover'}}
        elif method == 'textDocument/documentSymbol':
            result = [{'name': 'fixture_symbol', 'kind': 12}]
        else:
            result = [{'name': params.get('query', 'fixture')}]
    elif method == 'resources/list':
        result = {'resources': [{'uri': 'fixture://hello', 'name': 'hello'}]}
        if mode == 'pagination' and not params.get('cursor'):
            result['nextCursor'] = 'page2'
        if mode == 'pagination' and params.get('cursor'):
            result = {'resources': [{'uri': 'fixture://page2', 'name': 'page2'}]}
    elif method == 'resources/read':
        result = {'contents': [{'uri': params['uri'], 'text': 'real resource'}]}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'inputSchema': {'type': 'object'}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': json.dumps(params['arguments'])}]}
    send({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})
