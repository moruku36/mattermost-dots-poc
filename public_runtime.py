"""Owner-configured runtime template. No network on import or --prepare."""
from contextlib import AbstractContextManager
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
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


def existing_backend(arguments, session):
    """GET only, fixed configured channel, 5 recent records, 64KiB, no retry."""
    preflight()
    deadline = session['deadline']
    if arguments != {'channel_id': CHANNEL, 'limit': 5} or time.time() >= deadline:
        raise Refused('SOURCE_SCOPE_REFUSED')
    url = urlsplit(os.environ['MM_BASE_URL'])
    timeout = min(10, max(0.1, deadline - time.time()))
    connection = http.client.HTTPSConnection(url.hostname, url.port or 443,
                                             timeout=timeout, context=ssl.create_default_context())
    try:
        connection.request('GET', '/api/v4/channels/' + CHANNEL + '/posts?page=0&per_page=5',
                           headers={'Authorization': 'Bearer ' + os.environ['MM_TOKEN'],
                                    'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise Refused('SOURCE_READ_FAILED')
        raw = response.read(65537)
        if len(raw) > 65536 or time.time() >= deadline:
            raise Refused('SOURCE_BOUND_REFUSED')
        data = json.loads(raw)
        if type(data) is not dict or type(data.get('order')) is not list or len(data['order']) > 5:
            raise Refused('SOURCE_BOUND_REFUSED')
        posts = data.get('posts')
        if type(posts) is not dict:
            raise Refused('SOURCE_ENVELOPE_REFUSED')
        selected = []
        for ident in data['order']:
            post = posts.get(ident)
            if type(post) is not dict or post.get('channel_id') != CHANNEL or post.get('id') != ident:
                raise Refused('SOURCE_SCOPE_REFUSED')
            if post.get('delete_at', 0) or post.get('type', ''):
                continue
            # The HTTP body is transient; only these 3 source fields leave this adapter.
            selected.append({k: post.get(k) for k in ('id', 'channel_id', 'create_at')})
        body = json.dumps({'bounded': True, 'complete_history': False, 'posts': selected})
        return {'isError': False, 'content': [{'type': 'text', 'text': body}]}
    except Refused:
        raise
    except Exception:
        raise Refused('SOURCE_READ_FAILED') from None
    finally:
        connection.close()


def prepare():
    preflight()
    run = ROOT / 'runtime' / 'run-owner-trial'
    run.mkdir(parents=True, exist_ok=False)
    session = {'native_events_approved': True, 'owner': OWNER, 'channel': CHANNEL,
               'max_reads': 60, 'max_posts': 5, 'seconds': 300,
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
