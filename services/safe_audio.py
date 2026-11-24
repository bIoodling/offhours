"""Progressive HTTP audio reader. Pin every connection to public DNS answers.

Feed bytes to FFmpeg's stdin; FFmpeg never follows user supplied network URLs.
HLS/DASH manifests are deliberately excluded from this input path.
"""
import http.client
import ipaddress
import socket
import threading
from urllib.parse import urljoin, urlsplit


def public_address(address):
    address=ipaddress.ip_address(address)
    if isinstance(address,ipaddress.IPv6Address):
        if address.sixtofour and not public_address(address.sixtofour):return False
        if address.teredo and not all(public_address(x) for x in address.teredo):return False
    if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:
        address=address.ipv4_mapped
    return address.is_global and not any((address.is_multicast,address.is_reserved,address.is_unspecified,
                                         address.is_loopback,address.is_link_local))


def validate_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.port not in (None, 80, 443)
                or any(ord(c) < 33 or ord(c) == 127 for c in url)
                or '\\' in url or len(url) > 8192):
            raise ValueError
        host = parsed.hostname.rstrip('.').lower()
        if '%' in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not public_address(address):
            raise ValueError
        return parsed
    except ValueError:
        raise ValueError('Use a public HTTP(S) audio URL without credentials or custom ports.') from None


def public_connection(address, timeout=15, source_address=None, **kwargs):
    host, port = address
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or any(not public_address(a[4][0]) for a in answers):
        raise ValueError('Audio host resolves to a non-public address.')
    error = None
    for family, kind, proto, _, sockaddr in answers:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error or OSError('Audio connection failed.')


class AudioReader:
    def __init__(self, url, headers=None):
        self.connection = None
        self.response = None
        self.socket = None
        self.error = None
        self.closed = False
        self.close_lock = threading.Lock()
        request_headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'audio/*,video/*;q=0.9,*/*;q=0.1', 'Accept-Encoding':'identity'}
        names={'user-agent':'User-Agent','referer':'Referer','origin':'Origin','accept':'Accept'}
        for name,value in (headers or {}).items():
            if name.lower() in ('user-agent','referer','origin','accept') and isinstance(value,str) and not any(ord(c)<32 or ord(c)==127 for c in value):
                request_headers[names[name.lower()]]=value
        for _ in range(6):
            parsed = validate_url(url)
            cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
            conn = cls(parsed.hostname, parsed.port, timeout=15)
            conn._create_connection = public_connection
            self.connection = conn
            try:
                conn.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),headers=request_headers)
                self.socket = conn.sock
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    conn.close()
                    if not location:
                        raise ValueError('Audio redirect has no destination.')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError(f'Audio server returned HTTP {response.status}.')
                content_type = response.getheader('Content-Type', '').split(';')[0].lower()
                if not (content_type.startswith(('audio/', 'video/')) or content_type in (
                        'application/ogg', 'application/octet-stream')):
                    raise ValueError('URL must return progressive audio, not a page or playlist manifest.')
                if 'mpegurl' in content_type or 'dash' in content_type:
                    raise ValueError('This audio source requires an unsupported manifest transport.')
                self.response = response
                break
            except BaseException:
                conn.close()
                raise
        else:
            self.close()
            raise ValueError('Too many audio redirects.')

    def read(self, size):
        if self.closed or self.error:
            return b''
        try:
            return self.response.read(size)
        except (OSError,http.client.HTTPException,ValueError) as exc:
            # discord.py's stdin writer does not catch exceptions from read().
            # Returning EOF closes FFmpeg stdin; preserve the error on the source.
            self.error=RuntimeError('The remote audio connection ended unexpectedly: '+type(exc).__name__)
            self.close()
            return b''

    def close(self):
        with self.close_lock:
            if self.closed:return
            self.closed=True
            # shutdown interrupts a blocked read before HTTPResponse.close waits on it.
            if self.socket:
                try:self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            if self.response:
                self.response.close()
            if self.connection:
                self.connection.close()
