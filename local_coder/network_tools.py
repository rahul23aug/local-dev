"""Owner-authorized, DNS-pinned bounded HTTP tools with raw evidence."""
from __future__ import annotations

import copy
import http.client
from html.parser import HTMLParser
import ipaddress
import math
from pathlib import Path
import queue
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
import uuid

MAX_BYTES = 1024 * 1024
MAX_TEXT = 6000
# Conservative exclusions independent of older Python ipaddress registries.
# Translation/tunneling ranges can embed an otherwise forbidden destination.
SPECIAL_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '192.0.0.0/24', '64:ff9b::/96', '64:ff9b:1::/48',
    '2001::/23', '2002::/16', '3fff::/20', 'fec0::/10',
))


def _public(ip):
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and not ip.is_unspecified and not ip.is_loopback and not ip.is_link_local
            and not any(ip.version == network.version and ip in network for network in SPECIAL_NETWORKS))


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.text = []
        self.links = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'template', 'svg'):
            self.hidden += 1
        if self.hidden:
            return
        if tag == 'a':
            self.anchor = {'url': dict(attrs).get('href', ''), 'title': []}
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'template', 'svg'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'a' and self.anchor is not None:
            self.links.append({'url': self.anchor['url'], 'title': ' '.join(self.anchor['title']).strip()})
            self.anchor = None

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)
            if self.anchor is not None:
                self.anchor['title'].append(data)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout, secure=False):
        super().__init__(host, port, timeout=timeout)
        self.address = address
        self.secure = secure
        self.transport_socket = None
        self.deadline = time.monotonic() + timeout

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.transport_socket = self.sock
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.abort()
            raise socket.timeout()
        self.sock.settimeout(remaining)
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
            self.transport_socket = self.sock

    def abort(self):
        sock = self.transport_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class NetworkTools:
    def __init__(self, root: Path, config: dict):
        self.root = Path(root).resolve()
        self.config = copy.deepcopy(config or {})
        network = self.config.get('network', False)
        self.enabled = network is True or (isinstance(network, dict) and network.get('enabled') is True)
        self.allow_private = self.config.get('allow_private_network') is True
        try:
            timeout = float(self.config.get('network_timeout', 10))
            self.timeout = max(.1, min(15, timeout)) if math.isfinite(timeout) else 10
        except (ValueError, TypeError):
            self.timeout = 10

    def execute(self, name, args):
        if not self.enabled:
            raise ValueError('Network access is disabled by owner policy')
        if not isinstance(args, dict):
            raise ValueError('Network arguments must be an object')
        allowed = {'url', 'prompt'} if name == 'WebFetch' else {'query'}
        if set(args) - allowed:
            raise ValueError('Network transport configuration may only be supplied by the owner')
        if name == 'WebFetch':
            result, body = self._fetch(args.get('url'))
            result['text'] = self._text(body, result['content_type'])
            return result
        if name == 'WebSearch':
            query = args.get('query')
            if not isinstance(query, str) or not query.strip() or len(query) > 1000:
                raise ValueError('Search query must contain 1 to 1000 characters')
            endpoint = self.config.get('search_endpoint', 'https://html.duckduckgo.com/html/')
            parsed = self._url(endpoint)
            existing = parse_qs(parsed.query)
            existing['q'] = [query]
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', urlencode(existing, doseq=True), ''))
            result, body = self._fetch(url)
            parser = _HTML()
            parser.feed(self._decode(body, result['content_type']))
            links, seen = [], set()
            for link in parser.links:
                target = urljoin(result['url'], link['url'])
                try:
                    target_url = self._url(target)
                    if target_url.hostname and target_url.hostname.endswith('duckduckgo.com'):
                        wrapped = parse_qs(target_url.query).get('uddg')
                        if not wrapped:
                            continue
                        target = wrapped[0]
                        target_url = self._url(target)
                    # Search links are not fetched, but private literal URLs
                    # are still unsuitable as public-search results.
                    try:
                        address = ipaddress.ip_address(target_url.hostname)
                    except ValueError:
                        address = None
                    if address is not None and not _public(address):
                        continue
                except ValueError:
                    continue
                title = ' '.join(link['title'].split())[:200]
                if target not in seen and title:
                    links.append({'title': title, 'url': target[:2048]})
                    seen.add(target)
                if len(links) >= 20:
                    break
            return {'query': query, 'results': links, 'evidence_path': result['evidence_path'], 'source_url': result['url']}
        raise ValueError('Unknown network tool')

    @staticmethod
    def _url(url):
        if not isinstance(url, str) or not url or len(url) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError('A valid bounded HTTP URL is required')
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None or '\\' in parsed.netloc:
                raise ValueError
            parsed.port
        except ValueError:
            raise ValueError('Only HTTP(S) URLs without credentials are allowed') from None
        return parsed

    def _resolve(self, host, port, deadline):
        output = queue.Queue(maxsize=1)

        def resolve():
            try:
                output.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError:
                output.put(None)

        threading.Thread(target=resolve, daemon=True).start()
        try:
            answers = output.get(timeout=self._remaining(deadline))
        except queue.Empty:
            raise ValueError('Network DNS resolution timed out') from None
        if not answers:
            raise ValueError('Network hostname could not be resolved')
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not self.allow_private:
            for address in addresses:
                try:
                    ip = ipaddress.ip_address(address)
                    # IPv4-mapped IPv6 must obey the underlying IPv4 policy.
                    ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
                except ValueError:
                    raise ValueError('Network destination must resolve to public addresses') from None
                if not _public(ip):
                    raise ValueError('Network destination must resolve only to public addresses')
        return addresses

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('Network request timed out')
        return remaining

    def _fetch(self, initial_url):
        current = initial_url
        deadline = time.monotonic() + self.timeout
        for redirect in range(6):
            parsed = self._url(current)
            host = parsed.hostname.encode('idna').decode('ascii')
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            addresses = self._resolve(host, port, deadline)
            # The validated numeric destination is passed directly to the
            # socket. HTTP Host/TLS SNI still name the original DNS hostname.
            conn = _PinnedHTTP(host, port, addresses[0], self._remaining(deadline), parsed.scheme == 'https')
            deadline_timer = threading.Timer(self._remaining(deadline), conn.abort)
            deadline_timer.daemon = True
            deadline_timer.start()
            try:
                path = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
                conn.request('GET', path, headers={'User-Agent': 'local-coder/1.0', 'Accept': 'text/html,text/plain,application/json;q=0.8', 'Accept-Encoding': 'identity'})
                if conn.sock:
                    conn.sock.settimeout(self._remaining(deadline))
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise ValueError('Network redirect lacks Location')
                    if redirect == 5:
                        raise ValueError('Network redirect limit exceeded')
                    current = urljoin(current, location)
                    continue
                if not 200 <= response.status < 300:
                    raise ValueError(f'Network server returned HTTP {response.status}')
                encoding = response.getheader('Content-Encoding', 'identity').lower()
                if encoding not in ('', 'identity'):
                    raise ValueError('Compressed network responses are not accepted')
                declared = response.getheader('Content-Length')
                if declared:
                    try:
                        length = int(declared)
                    except ValueError:
                        raise ValueError('Network response has invalid Content-Length') from None
                    if length > MAX_BYTES:
                        raise ValueError('Network response exceeds 1 MiB limit')
                    if length < 0:
                        raise ValueError('Network response has invalid Content-Length')
                chunks, size = [], 0
                while True:
                    if conn.sock:
                        conn.sock.settimeout(self._remaining(deadline))
                    else:
                        self._remaining(deadline)
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError('Network response exceeds 1 MiB limit')
                    chunks.append(chunk)
                body = b''.join(chunks)
                evidence = self._evidence(body)
                return {'url': current, 'status': response.status, 'content_type': response.getheader('Content-Type', 'text/plain'), 'evidence_path': str(evidence), 'bytes': size}, body
            except (socket.timeout, TimeoutError):
                raise ValueError('Network request timed out') from None
            except (OSError, http.client.HTTPException, UnicodeError):
                if time.monotonic() >= deadline:
                    raise ValueError('Network request timed out') from None
                raise ValueError('Network request failed') from None
            finally:
                deadline_timer.cancel()
                conn.close()
        raise ValueError('Network redirect limit exceeded')

    def _evidence(self, body):
        directory = self.root.parent / 'evidence'
        if directory.is_symlink():
            raise ValueError('Network evidence directory must not be a symlink')
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / f'network-{uuid.uuid4().hex}.bin'
        with target.open('xb') as stream:
            stream.write(body)
        target.chmod(0o600)
        return target

    @staticmethod
    def _decode(body, content_type):
        charset = 'utf-8'
        for part in content_type.split(';')[1:]:
            if part.strip().lower().startswith('charset='):
                charset = part.strip().split('=', 1)[1].strip('"\' ')
        try:
            return body.decode(charset, errors='replace')
        except (LookupError, UnicodeError):
            return body.decode('utf-8', errors='replace')

    def _text(self, body, content_type):
        decoded = self._decode(body, content_type)
        if 'html' in content_type.lower():
            parser = _HTML()
            parser.feed(decoded)
            decoded = ' '.join(parser.text)
        text = ' '.join(decoded.split())
        return ''.join(char for char in text if ord(char) >= 32 and ord(char) != 127)[:MAX_TEXT]
