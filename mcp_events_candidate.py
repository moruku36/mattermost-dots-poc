"""Native MCP Events local candidate. Only an injected fixture callback is usable.

No real subscription, callback network, credential storage or old gate mutation.
"""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from notification_core import CHANNEL, Refused

EVENT = 'message.created'
PROTOCOL = '2026-07-28'
SCOPE_REASONS = frozenset({
    'PRINCIPAL_TYPE', 'PRINCIPAL_EMPTY', 'PRINCIPAL_ACCESS', 'PARAMS_TYPE',
    'PARAMS_UNKNOWN_FIELDS', 'META_TYPE', 'NAME_MISSING', 'NAME_TYPE', 'NAME_SCOPE',
    'ARGUMENTS_MISSING', 'ARGUMENTS_TYPE', 'ARGUMENTS_UNKNOWN_FIELDS',
    'CHANNEL_MISSING', 'CHANNEL_TYPE', 'CHANNEL_SCOPE', 'MAX_AGE_TYPE',
    'CURSOR_TYPE', 'CURSOR_REPLAY', 'DELIVERY_MISSING', 'DELIVERY_TYPE',
    'DELIVERY_UNKNOWN_FIELDS', 'DELIVERY_MODE_MISSING', 'DELIVERY_URL_MISSING',
    'DELIVERY_SECRET_MISSING', 'DELIVERY_MODE_TYPE', 'DELIVERY_MODE_SCOPE',
    'DELIVERY_URL_TYPE', 'DELIVERY_SECRET_TYPE', 'REQUEST_TYPE',
    'REQUEST_JSONRPC', 'REQUEST_FIELDS', 'REQUEST_ID', 'REQUEST_METHOD',
})
DIAGNOSTIC_CODES = frozenset({
    'SUBSCRIBE_RECEIVED', 'CALLBACK_VALIDATION_STARTED', 'CALLBACK_VALIDATION_SUCCEEDED',
    'CALLBACK_VALIDATION_FAILED', 'SUBSCRIPTION_SAVED', 'MONITOR_STARTED',
    'CLEANUP_COMPLETED', 'SUBSCRIBE_REFUSED', 'REQUEST_REFUSED', 'FAIL_TTL_REFUSED',
    'FAIL_SCOPE_REFUSED', 'FAIL_DELIVERY_REFUSED', 'FAIL_SIGNING_KEY_REFUSED',
    'FAIL_TRANSPORT_FAILED', 'FAIL_CHALLENGE_FAILED', 'FAIL_SUBSCRIPTION_EXISTS',
    'FAIL_EXPIRED_OR_REPLAY',
}) | frozenset('FAIL_' + code for code in SCOPE_REASONS)


def granted_ttl(params):
    ttl = params.get('ttlMs', 300000)
    # Official null means no requested expiration; this trial still grants 5m.
    if ttl is None:
        return 300000
    if type(ttl) is not int or ttl < 1:
        raise Refused('TTL_REFUSED')
    return min(300000, ttl)


def canonical(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True, ensure_ascii=False)


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def key(secret):
    try:
        if type(secret) is not str or not secret.startswith('whsec_'):
            raise ValueError()
        raw = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(raw) <= 64:
            raise ValueError()
        return raw
    except (ValueError, TypeError):
        raise Refused('SIGNING_KEY_REFUSED') from None


def sign(secret, ident, timestamp, body):
    raw = ident.encode() + b'.' + str(timestamp).encode() + b'.' + body
    return 'v1,' + base64.b64encode(hmac.digest(key(secret), raw, 'sha256')).decode()


class FixtureTransport:
    """No network. A real implementation must pin public DNS+TLS per connection."""
    is_fixture = True
    def __init__(self, handler):
        self.handler = handler

    def post(self, url, headers, body):
        # The caller-supplied fixture never sees real callback values.
        return self.handler(url, headers, body)


class Events:
    def __init__(self, path, *, trial_deadline, transport, authorized, clock=time.time, protector=None, diagnostic=None):
        from protected_delivery import ProtectedHTTPS, Dpapi
        if (type(transport) not in (FixtureTransport, ProtectedHTTPS) or not callable(authorized)
                or (type(transport) is ProtectedHTTPS and type(protector) is not Dpapi)):
            raise Refused('LIVE_DELIVERY_NOT_ENABLED')
        self.protector = protector
        self.diagnostic = diagnostic or (lambda code: None)
        if not 0 < trial_deadline - clock() <= 300:
            raise Refused('TRIAL_DEADLINE_REFUSED')
        self.clock, self.deadline, self.transport, self.authorized = clock, trial_deadline, transport, authorized
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS trial (deadline REAL, start_ms INTEGER);
          CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY, owner TEXT, url TEXT, secret TEXT, old_secret TEXT,
            rotate_until REAL, expiry REAL, active INTEGER, count INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS queue (
            sid TEXT, eid TEXT, body BLOB, attempts INTEGER DEFAULT 0,
            next_at REAL, status TEXT DEFAULT 'pending', PRIMARY KEY(sid,eid));
        ''')
        persisted = self.db.execute('SELECT deadline,start_ms FROM trial').fetchone()
        if persisted and persisted[0] != trial_deadline:
            self.db.close()
            raise Refused('TRIAL_EXTENSION_REFUSED')
        if not persisted:
            self.db.execute('INSERT INTO trial VALUES (?,?)', (trial_deadline, int(clock() * 1000)))
            self.db.commit()
        self.start_ms = self.db.execute('SELECT start_ms FROM trial').fetchone()[0]

    def scope(self, principal, params, *, subscription=False):
        # Only documented event fields and standard MCP metadata are accepted.
        # Metadata is ignored, never used for identity/scope or diagnostic values.
        permitted = {'name', 'arguments', 'delivery', '_meta'}
        if subscription:
            permitted |= {'cursor', 'ttlMs', 'maxAgeMs'}
        if type(principal) is not str: raise Refused('PRINCIPAL_TYPE')
        if not principal: raise Refused('PRINCIPAL_EMPTY')
        if not self.authorized(principal, CHANNEL): raise Refused('PRINCIPAL_ACCESS')
        if type(params) is not dict: raise Refused('PARAMS_TYPE')
        if set(params) - permitted: raise Refused('PARAMS_UNKNOWN_FIELDS')
        if '_meta' in params and type(params['_meta']) is not dict: raise Refused('META_TYPE')
        if 'name' not in params: raise Refused('NAME_MISSING')
        if type(params['name']) is not str: raise Refused('NAME_TYPE')
        if params['name'] != EVENT: raise Refused('NAME_SCOPE')
        if 'arguments' not in params: raise Refused('ARGUMENTS_MISSING')
        arguments = params['arguments']
        if type(arguments) is not dict: raise Refused('ARGUMENTS_TYPE')
        if set(arguments) - {'channel_id'}: raise Refused('ARGUMENTS_UNKNOWN_FIELDS')
        if 'channel_id' not in arguments: raise Refused('CHANNEL_MISSING')
        if type(arguments['channel_id']) is not str: raise Refused('CHANNEL_TYPE')
        if arguments['channel_id'] != CHANNEL: raise Refused('CHANNEL_SCOPE')
        if 'maxAgeMs' in params and (type(params['maxAgeMs']) is not int or params['maxAgeMs'] < 0):
            raise Refused('MAX_AGE_TYPE')
        # No replay is granted, including with maxAgeMs. Null/omitted = now.
        if 'cursor' in params and params['cursor'] is not None:
            if type(params['cursor']) is not str: raise Refused('CURSOR_TYPE')
            raise Refused('CURSOR_REPLAY')
        if 'delivery' not in params: raise Refused('DELIVERY_MISSING')
        delivery = params.get('delivery')
        fields = {'mode', 'url', 'secret'} if subscription else {'mode', 'url'}
        if type(delivery) is not dict: raise Refused('DELIVERY_TYPE')
        if set(delivery) - fields: raise Refused('DELIVERY_UNKNOWN_FIELDS')
        for field in ('mode', 'url', 'secret') if subscription else ('mode', 'url'):
            if field not in delivery: raise Refused('DELIVERY_' + field.upper() + '_MISSING')
        if type(delivery['mode']) is not str: raise Refused('DELIVERY_MODE_TYPE')
        if delivery['mode'] != 'webhook': raise Refused('DELIVERY_MODE_SCOPE')
        if type(delivery['url']) is not str: raise Refused('DELIVERY_URL_TYPE')
        if subscription and type(delivery['secret']) is not str: raise Refused('DELIVERY_SECRET_TYPE')
        parsed = urlsplit(delivery.get('url', ''))
        # Dummy callback namespace only. Real callback generated by ChatGPT is
        # not guessed and remains disabled until protected storage/TLS approval.
        if type(self.transport) is FixtureTransport:
            if (parsed.scheme != 'https' or parsed.hostname != 'callback.fixture.invalid'
                    or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
                raise Refused('FIXTURE_CALLBACK_ONLY')
        else:
            self.transport.validate_url(delivery['url'])
        ident = hashlib.sha256(canonical([principal, delivery['url'], EVENT, params['arguments']]).encode()).hexdigest()
        return ident, delivery

    def signed_post(self, sid, delivery, body, ident, old_secret=None):
        stamp = int(self.clock())
        signature = sign(delivery['secret'], ident, stamp, body)
        if old_secret:
            signature += ' ' + sign(old_secret, ident, stamp, body)
        headers = {'Content-Type': 'application/json', 'webhook-id': ident,
                   'webhook-timestamp': str(stamp), 'webhook-signature': signature,
                   'X-MCP-Subscription-Id': sid}
        return self.transport.post(delivery['url'], headers, body)

    def seal(self, value):
        return self.protector.seal(value) if self.protector and value is not None else value

    def open_value(self, value):
        return self.protector.open(value) if self.protector and value is not None else value

    def subscribe(self, principal, params):
        with self.lock:
            self.diagnostic('SUBSCRIBE_RECEIVED')
            sid, delivery = self.scope(principal, params, subscription=True)
            if self.clock() >= self.deadline or params.get('cursor') is not None:
                raise Refused('EXPIRED_OR_REPLAY_REFUSED')
            key(delivery['secret'])
            ttl = granted_ttl(params)
            expiry = min(self.deadline, self.clock() + ttl / 1000)
            row = self.db.execute('SELECT secret,count FROM subscriptions WHERE id=?', (sid,)).fetchone()
            other = self.db.execute('SELECT 1 FROM subscriptions WHERE active=1 AND id<>?', (sid,)).fetchone()
            if other:
                raise Refused('ONE_SUBSCRIPTION_ONLY')
            if row:
                row = (self.open_value(row[0]), row[1])
            if not row or row[0] != delivery['secret']:
                # Fresh short-lived challenge is memory-only and never logged.
                challenge = secrets.token_urlsafe(24)
                body = canonical({'type': 'verification', 'challenge': challenge}).encode()
                start = self.clock()
                self.diagnostic('CALLBACK_VALIDATION_STARTED')
                try:
                    code, response = self.signed_post(sid, delivery, body, 'verify_' + secrets.token_hex(12))
                except Exception:
                    self.diagnostic('CALLBACK_VALIDATION_FAILED')
                    raise Refused('CALLBACK_TRANSPORT_FAILED') from None
                if (not 200 <= code < 300 or self.clock() - start > 10 or self.clock() >= expiry
                        or type(response) is not dict or type(response.get('challenge')) is not str
                        or not hmac.compare_digest(response['challenge'], challenge)):
                    self.diagnostic('CALLBACK_VALIDATION_FAILED')
                    raise Refused('CALLBACK_CHALLENGE_FAILED')
                self.diagnostic('CALLBACK_VALIDATION_SUCCEEDED')
            old = row[0] if row and row[0] != delivery['secret'] else None
            with self.db:
                self.db.execute('''INSERT INTO subscriptions(id,owner,url,secret,old_secret,rotate_until,expiry,active,count)
                  VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,
                  old_secret=excluded.old_secret,rotate_until=excluded.rotate_until,expiry=excluded.expiry,active=1''',
                  (sid, principal, self.seal(delivery['url']), self.seal(delivery['secret']), self.seal(old),
                   min(self.deadline, self.clock() + 30) if old else 0, expiry, row[1] if row else 0))
            self.diagnostic('SUBSCRIPTION_SAVED')
            return {'id': sid, 'refreshBefore': iso(expiry), 'cursor': None, 'truncated': False}

    def cancel(self, sid):
        with self.db:
            self.db.execute('UPDATE subscriptions SET active=0,secret=NULL,old_secret=NULL,url=NULL WHERE id=?', (sid,))
            self.db.execute("UPDATE queue SET status='cancelled',body=NULL WHERE sid=? AND status='pending'", (sid,))

    def unsubscribe(self, principal, params):
        with self.lock:
            sid, _ = self.scope(principal, params)
            self.cancel(sid)
            return {}

    def enqueue_metadata(self, post):
        # Input adapter must establish create-event provenance and active-session
        # floor. No arbitrary body/user/team fields can leak to callback storage.
        with self.lock:
            if type(post) is not dict or set(post) != {'channel_id', 'post_id', 'created_ms'} or post.get('channel_id') != CHANNEL:
                raise Refused('METADATA_SCHEMA_REFUSED')
            import re
            if (type(post['post_id']) is not str or not re.fullmatch(r'[a-z0-9]{26}', post['post_id'])
                    or type(post['created_ms']) is not int or post['created_ms'] < self.start_ms
                    or post['created_ms'] > int(self.clock() * 1000) + 60000):
                raise Refused('METADATA_SCHEMA_REFUSED')
            eid = 'evt_' + hashlib.sha256(canonical([CHANNEL, post['post_id']]).encode()).hexdigest()
            body = canonical({'eventId': eid, 'name': EVENT, 'timestamp': iso(post['created_ms'] / 1000),
                              'data': post, 'cursor': None}).encode()
            for sid, owner, expiry, active, count in self.db.execute('SELECT id,owner,expiry,active,count FROM subscriptions').fetchall():
                if not active:
                    continue
                if self.clock() >= expiry or not self.authorized(owner, CHANNEL):
                    self.cancel(sid)
                    continue
                total = self.db.execute('SELECT COALESCE(SUM(count),0) FROM subscriptions').fetchone()[0]
                if total >= 5 or self.db.execute('SELECT 1 FROM queue WHERE sid=? AND eid=?', (sid, eid)).fetchone():
                    continue
                with self.db:
                    self.db.execute('INSERT INTO queue(sid,eid,body,next_at) VALUES (?,?,?,?)', (sid, eid, body, self.clock()))
                    self.db.execute('UPDATE subscriptions SET count=count+1 WHERE id=?', (sid,))

    def drain(self):
        with self.lock:
            for sid, owner, expiry in self.db.execute('SELECT id,owner,expiry FROM subscriptions WHERE active=1').fetchall():
                if self.clock() >= expiry or not self.authorized(owner, CHANNEL):
                    self.cancel(sid)
            with self.db:
                self.db.execute('UPDATE subscriptions SET old_secret=NULL WHERE rotate_until<=?', (self.clock(),))
            rows = self.db.execute("SELECT sid,eid,body,attempts FROM queue WHERE status='pending' AND next_at<=?", (self.clock(),)).fetchall()
            for sid, eid, body, attempts in rows:
                if attempts >= 3:
                    with self.db:
                        self.db.execute("UPDATE queue SET status='failed' WHERE sid=? AND eid=?", (sid, eid))
                    continue
                row = self.db.execute('SELECT owner,url,secret,old_secret,rotate_until,expiry,active FROM subscriptions WHERE id=?', (sid,)).fetchone()
                if not row or not row[6] or self.clock() >= row[5] or not self.authorized(row[0], CHANNEL):
                    self.cancel(sid)
                    continue
                # Reserve attempt before sending: crash cannot reset retry budget.
                with self.db:
                    self.db.execute('UPDATE queue SET attempts=attempts+1,next_at=? WHERE sid=? AND eid=?', (self.clock() + 2 ** attempts, sid, eid))
                try:
                    status, _ = self.signed_post(sid, {'url': self.open_value(row[1]), 'secret': self.open_value(row[2])}, body, eid,
                                                self.open_value(row[3]) if self.clock() < row[4] else None)
                except (TimeoutError, ConnectionError):
                    status = 503
                state = 'delivered' if 200 <= status < 300 else 'pending'
                if status in (410, 413) or (300 <= status < 500 and status != 429) or attempts + 1 >= 3:
                    state = 'failed' if state != 'delivered' else state
                with self.db:
                    self.db.execute('UPDATE queue SET status=? WHERE sid=? AND eid=?', (state, sid, eid))
                if status == 410:
                    self.cancel(sid)

    def close(self):
        self.db.close()


class Bridge:
    """Authenticated principal comes from transport, never JSON params."""
    def __init__(self, old_gate, events):
        self.old_gate, self.events = old_gate, events

    def handle(self, request, principal):
        ident = request.get('id') if type(request) is dict else None
        try:
            if type(request) is not dict: raise Refused('REQUEST_TYPE')
            if request.get('jsonrpc') != '2.0': raise Refused('REQUEST_JSONRPC')
            if set(request) - {'jsonrpc', 'id', 'method', 'params'}: raise Refused('REQUEST_FIELDS')
            if type(ident) not in (str, int): raise Refused('REQUEST_ID')
            if type(request.get('method')) is not str: raise Refused('REQUEST_METHOD')
            if type(principal) is not str: raise Refused('PRINCIPAL_TYPE')
            if not principal: raise Refused('PRINCIPAL_EMPTY')
            if not self.events.authorized(principal, CHANNEL): raise Refused('PRINCIPAL_ACCESS')
            method, params = request.get('method'), request.get('params', {})
            if method == 'server/discover':
                result = {'resultType': 'complete', 'supportedVersions': [PROTOCOL], 'capabilities': {'tools': {}, 'events': {}}}
            elif method == 'events/list':
                result = {'events': [{'name': EVENT, 'description': 'New-post metadata in the single approved channel; five-minute bounded trial.',
                    'delivery': ['webhook'], 'inputSchema': {'type': 'object', 'properties': {'channel_id': {'type': 'string', 'enum': [CHANNEL]}},
                    'required': ['channel_id'], 'additionalProperties': False},
                    'payloadSchema': {'type': 'object', 'properties': {'channel_id': {'type': 'string'}, 'post_id': {'type': 'string'}, 'created_ms': {'type': 'integer'}},
                    'required': ['channel_id', 'post_id', 'created_ms'], 'additionalProperties': False}}]}
            elif method == 'events/subscribe':
                result = self.events.subscribe(principal, params)
            elif method == 'events/unsubscribe':
                result = self.events.unsubscribe(principal, params)
            else:
                return self.old_gate.handle(request)
            return {'jsonrpc': '2.0', 'id': ident, 'result': result}
        except (Refused, ValueError, TypeError) as error:
            # Never echo arbitrary exception strings, URLs, principal or secrets.
            reason = 'candidate_refused'
            fixed = {'TTL_REFUSED': 'ttl_refused', 'AUTH_SCOPE_REFUSED': 'scope_refused',
                     'DELIVERY_REFUSED': 'delivery_refused', 'SIGNING_KEY_REFUSED': 'signing_key_refused',
                     'CALLBACK_TRANSPORT_FAILED': 'transport_failed', 'CALLBACK_CHALLENGE_FAILED': 'challenge_failed',
                     'ONE_SUBSCRIPTION_ONLY': 'subscription_exists', 'EXPIRED_OR_REPLAY_REFUSED': 'expired_or_replay'}
            fixed.update({code: code.lower() for code in SCOPE_REASONS})
            if type(error) is Refused:
                reason = fixed.get(str(error), reason)
            diagnostic = getattr(self.events, 'diagnostic', None)
            if diagnostic:
                diagnostic('SUBSCRIBE_REFUSED' if type(request) is dict and request.get('method') == 'events/subscribe' else 'REQUEST_REFUSED')
                if reason in fixed.values():
                    diagnostic('FAIL_' + reason.upper())
            return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32015 if type(request) is dict and request.get('method') == 'events/subscribe' else -32600,
                    'message': 'Event candidate refused request', 'data': {'reason': reason}}}
