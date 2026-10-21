"""Small browser POST guard for the direct stdlib HTTP listener (no proxy trust)."""
import ipaddress
import socket
from urllib.parse import urlsplit


def authority(value):
    if not value or any(c.isspace() for c in value) or any(c in value for c in '/\\?#@,'):
        raise ValueError('invalid authority')
    parsed = urlsplit('http://' + value)
    if not parsed.hostname or parsed.username or parsed.password or value.endswith(':') or parsed.port == 0:
        raise ValueError('invalid authority')
    return parsed.hostname.lower(), 80 if parsed.port is None else parsed.port


def post_allowed(handler):
    """Allow direct local/LAN hosts and exact HTTP origins; absence is compatible.

    Browser Origin is not a credential. Controlled callers can omit it, while
    Fetch Metadata still rejects a browser cross-site POST without Origin.
    Forwarded/X-Forwarded-* are deliberately not trusted.
    """
    headers = handler.headers
    if len(headers.get_all('Host', [])) != 1 or len(headers.get_all('Origin', [])) > 1:
        return False
    try:
        host, port = authority(headers['Host'])
        if port != handler.server.server_port:
            return False
        names = {'localhost', socket.gethostname().lower(), socket.getfqdn().lower()}
        if host not in names:
            # Compare literal IPs to the connection's actual local interface.
            if ipaddress.ip_address(host) != ipaddress.ip_address(handler.connection.getsockname()[0]):
                return False
        if headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            return False
        origin = headers.get('Origin')
        if origin is None:
            return True
        parsed = urlsplit(origin)
        if parsed.scheme != 'http' or origin != 'http://' + parsed.netloc:
            return False
        return authority(parsed.netloc) == (host, port)
    except (ValueError, TypeError):
        return False
