from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from local_coder.network_tools import NetworkTools
except ImportError:
    NetworkTools = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/drip':
            try:
                for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok':
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(.03)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/ok')
            self.end_headers()
            return
        if self.path == '/loop':
            self.send_response(302)
            self.send_header('Location', '/loop')
            self.end_headers()
            return
        if self.path == '/slow':
            time.sleep(.5)
        if self.path.startswith('/search'):
            body = b'<html><a href="https://example.com/article">Real result</a><a href="javascript:alert(1)">unsafe</a></html>'
        elif self.path == '/big':
            body = b'x' * (1024 * 1024 + 1)
        elif self.path == '/text':
            body = b'x' * 20000
        elif self.path == '/length':
            self.send_response(200)
            self.send_header('Content-Length', str(1024 * 1024 + 100))
            self.end_headers()
            return
        else:
            body = b'<html><h1>Actual page</h1><script>secretScript()</script><style>hide me</style><p>Hello &amp; welcome.</p></html>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        self.root.mkdir()

    def client(self, **overrides):
        self.assertIsNotNone(NetworkTools, 'real network implementation is required')
        return NetworkTools(self.root, {'network': True, 'allow_private_network': True, **overrides})

    def test_network_is_owner_disabled_by_default(self):
        self.assertIsNotNone(NetworkTools)
        for config in ({}, {'network': False}):
            with self.assertRaisesRegex(ValueError, 'disabled'):
                NetworkTools(self.root, config).execute('WebFetch', {'url': self.url})

    def test_private_addresses_are_denied_without_owner_override(self):
        client = self.client(allow_private_network=False)
        for host in ('127.0.0.1', 'localhost', '[::1]', '169.254.169.254'):
            with self.assertRaisesRegex(ValueError, 'public'):
                client.execute('WebFetch', {'url': f'http://{host}/'})

    def test_real_fetch_strips_scripts_and_saves_raw_evidence(self):
        result = self.client().execute('WebFetch', {'url': self.url + '/ok', 'prompt': 'summarize'})
        self.assertIn('Actual page', result['text'])
        self.assertIn('Hello & welcome.', result['text'])
        self.assertNotIn('secretScript', result['text'])
        self.assertNotIn('hide me', result['text'])
        evidence = Path(result['evidence_path'])
        self.assertEqual(evidence.parent, self.root.parent / 'evidence')
        self.assertIn(b'secretScript', evidence.read_bytes())

    def test_redirects_follow_real_location_and_loops_are_bounded(self):
        result = self.client().execute('WebFetch', {'url': self.url + '/redirect'})
        self.assertEqual(result['url'], self.url + '/ok')
        with self.assertRaisesRegex(ValueError, 'redirect'):
            self.client().execute('WebFetch', {'url': self.url + '/loop'})

    def test_response_and_timeout_limits(self):
        for endpoint in ('/big', '/length'):
            with self.assertRaisesRegex(ValueError, '1 MiB'):
                self.client().execute('WebFetch', {'url': self.url + endpoint})
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.client(network_timeout=.1).execute('WebFetch', {'url': self.url + '/slow'})

    def test_real_search_parses_links(self):
        result = self.client(search_endpoint=self.url + '/search').execute('WebSearch', {'query': 'test'})
        self.assertEqual(result['results'], [{'title': 'Real result', 'url': 'https://example.com/article'}])
        self.assertTrue(Path(result['evidence_path']).exists())

    def test_model_cannot_enable_network_or_supply_transport_config(self):
        for key in ('allow_private_network', 'network', 'timeout', 'search_endpoint', 'headers'):
            with self.assertRaises(ValueError):
                self.client().execute('WebFetch', {'url': self.url, key: True})

    def test_credentials_and_non_http_urls_are_denied(self):
        for url in ('file:///etc/passwd', 'http://user:secret@example.com/', 'ftp://example.com'):
            with self.assertRaises(ValueError):
                self.client().execute('WebFetch', {'url': url})

    def test_output_text_is_capped(self):
        result = self.client().execute('WebFetch', {'url': self.url + '/text'})
        self.assertEqual(len(result['text']), 6000)

    def test_slow_drip_cannot_reset_total_deadline(self):
        before = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.client(network_timeout=.15).execute('WebFetch', {'url': self.url + '/drip'})
        self.assertLess(time.monotonic() - before, .5)

    def test_dns_addresses_are_pinned_and_redirects_revalidated(self):
        client = self.client(allow_private_network=False)
        with patch('local_coder.network_tools.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 80))]):
            self.assertEqual(client._resolve('example.com', 80, time.monotonic() + 1), ['93.184.216.34'])
        with patch('local_coder.network_tools._PinnedHTTP') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 302
            response.getheader.return_value = self.url + '/ok'
            with patch('local_coder.network_tools.socket.getaddrinfo', side_effect=[[(2, 1, 6, '', ('93.184.216.34', 80))], [(2, 1, 6, '', ('127.0.0.1', 80))]]):
                with self.assertRaisesRegex(ValueError, 'public'):
                    client.execute('WebFetch', {'url': 'http://example.com/start'})
            self.assertEqual(connection.call_count, 1)
            self.assertEqual(connection.call_args.args[2], '93.184.216.34')

    def test_special_use_addresses_are_not_public_destinations(self):
        client = self.client(allow_private_network=False)
        for address in ('224.0.0.1', 'ff02::1', 'fec0::1', '192.0.0.8', '64:ff9b::7f00:1', '2002:7f00:1::'):
            with patch('local_coder.network_tools.socket.getaddrinfo', return_value=[(2, 1, 6, '', (address, 80))]):
                with self.assertRaisesRegex(ValueError, 'public', msg=address):
                    client._resolve('example.com', 80, time.monotonic() + 1)

if __name__ == '__main__':
    unittest.main()
