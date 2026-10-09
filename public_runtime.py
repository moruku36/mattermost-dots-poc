"""Owner-configured runtime template. No network on import or --prepare."""
from contextlib import AbstractContextManager
import logging
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urlsplit

from poc_config import CHANNEL
from notification_core import Refused

OWNER = 'local-owner'  # Transport identity for this single-owner stdio template.
ROOT = Path(__file__).resolve().parent


def safe_path(path):
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise Refused('PATH_REFUSED')
    return path.resolve()


def preflight():
    if (os.name != 'nt' or os.environ.get('MM_TRIAL_APPROVED') != 'YES'
            or not re.fullmatch(r'[a-z0-9]{26}', CHANNEL) or CHANNEL == 'a' * 26):
        raise Refused('OWNER_PREPARATION_REQUIRED')
    url = urlsplit(os.environ.get('MM_BASE_URL', ''))
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')):
        raise Refused('SOURCE_ORIGIN_REFUSED')
    token = os.environ.get('MM_TOKEN', '')
    if not token or any(c.isspace() for c in token):
        raise Refused('SOURCE_TOKEN_REQUIRED')
    return 'owner-configured-five-minute-trial'


class SingleTrial(AbstractContextManager):
    """Exclusive local process marker. Stale markers require owner inspection."""
    def __init__(self, path):
        self.path = safe_path(path)

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Refused('TRIAL_ALREADY_STARTED') from None
        return self

    def __exit__(self, *args):
        os.close(self.fd)
        self.path.unlink()


class SourceSocket:
    def __init__(self, socket):
        self.socket = socket

    def recv(self, timeout):
        from websockets.exceptions import ConnectionClosed
        try:
            return self.socket.recv(timeout=timeout)
        except ConnectionClosed as exc:
            # Protocol errors, policy refusal, oversize: fail closed, no reconnect.
            codes = [frame.code for frame in (exc.rcvd, exc.sent) if frame is not None]
            if any(code not in (1000, 1001, 1006, 1011, 1012, 1013) for code in codes):
                raise Refused('SOURCE_PROTOCOL_REFUSED') from None
            raise ConnectionError('SOURCE_DISCONNECTED') from None

    def close(self):
        self.socket.close()


def connect_source(remaining):
    """WSS only, verified TLS, no redirects/proxy, no payload/header logging."""
    preflight()
    if remaining <= 0:
        raise Refused('SOURCE_DEADLINE_REFUSED')
    from websockets.sync.client import connect
    from websockets.exceptions import InvalidStatus
    url = urlsplit(os.environ['MM_BASE_URL'])
    # An unregistered disabled logger cannot inherit a host's debug handlers.
    logger = logging.Logger('bounded-source-private')
    logger.disabled = True
    logger.propagate = False
    try:
        socket = connect('wss://' + url.netloc + '/api/v4/websocket',
            additional_headers={'Authorization': 'Bearer ' + os.environ['MM_TOKEN']},
            ssl=ssl.create_default_context(), proxy=None, compression=None,
            open_timeout=min(5, remaining), close_timeout=0.25,
            ping_interval=20, ping_timeout=5, max_size=65536, max_queue=4,
            logger=logger)
        return SourceSocket(socket)
    except InvalidStatus:
        # Includes redirects and 401/403; never forward credentials to a new origin.
        raise Refused('SOURCE_HANDSHAKE_REFUSED') from None
    except ssl.SSLError:
        raise Refused('SOURCE_TLS_REFUSED') from None
    except (OSError, TimeoutError):
        raise ConnectionError('SOURCE_CONNECT_FAILED') from None
    except Exception:
        raise Refused('SOURCE_CONNECT_REFUSED') from None


def prepare():
    preflight()
    run = ROOT / 'runtime' / 'run-owner-trial'
    run.mkdir(parents=True, exist_ok=False)
    session = {'native_events_approved': True, 'owner': OWNER, 'channel': CHANNEL,
               'source_mode': 'websocket', 'max_connections': 3, 'max_posts': 5, 'seconds': 300,
               'policy': 'owner-configured-five-minute-trial'}
    (run / 'session.json').write_text(json.dumps(session), encoding='utf-8')
    print('Prepared one finite trial. No source read or callback delivery performed.')


if __name__ == '__main__':
    if sys.argv[1:] != ['--prepare']:
        print('Disabled. Owner preparation and explicit live configuration are required.')
        raise SystemExit(1)
    try:
        prepare()
    except Exception:
        print('Preparation refused. Review local configuration; no secret details emitted.')
        raise SystemExit(1)
