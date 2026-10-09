"""Prepared native event stdio bridge. Owner-launched supervisor only."""
import json
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

from mcp_events_candidate import Bridge, Events, PROTOCOL, DIAGNOSTIC_CODES, granted_ttl
from notification_core import CHANNEL, MAX_WIRE, Refused, strict_json
from protected_delivery import Dpapi, ProtectedHTTPS
from source_scope import WebSocketSource

PARENT = Path(__file__).resolve().parent
from public_runtime import OWNER, safe_path, preflight, connect_source

# Lifecycle evidence is a separate bounded file: fixed code plus UTC second
# only. The native-diagnostics.json count allowlist is not widened.
LIFECYCLE_CODES = frozenset({
    'UNSUBSCRIBE_RECEIVED', 'UNSUBSCRIBE_SUCCEEDED', 'UNSUBSCRIBE_NOT_ACTIVE', 'UNSUBSCRIBE_REFUSED',
    'STDIN_EOF', 'SERVICE_EXCEPTION', 'OUTPUT_FAILURE',
    'MONITOR_STOP_UNSUBSCRIBE', 'MONITOR_STOP_FINISH',
    'FINISH_STDIN_EOF', 'FINISH_SERVICE_EXCEPTION', 'FINISH_OUTPUT_FAILURE', 'FINISH_UNSPECIFIED',
})
LIFECYCLE_LIMIT = 64
LIFECYCLE_STAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')


class Service:
    def __init__(self, run):
        self.run = safe_path(Path(run))
        self.session = json.loads((self.run / 'session.json').read_text())
        if (self.session.get('native_events_approved') is not True or self.session.get('channel') != CHANNEL
                or self.session.get('owner') != OWNER or self.session.get('source_mode') != 'websocket'
                or self.session.get('max_connections') != 3
                or self.session.get('max_posts') != 5 or self.session.get('seconds') != 300):
            raise Refused('NATIVE_SCOPE_REFUSED')
        self.events, self.source, self.worker = None, None, None
        self.diagnostic_lock = threading.RLock()
        self.disabled = False
        # Process restart never silently resumes a source connection or grant.
        if (self.run / 'native-started.json').exists():
            self.disabled = True
            path = self.run / 'events.protected.sqlite'
            if path.exists():
                # Clear protected values without opening them or issuing network calls.
                with closing(sqlite3.connect(path)) as db:
                    db.execute('PRAGMA secure_delete=ON')
                    db.execute('UPDATE subscriptions SET active=0,secret=NULL,old_secret=NULL,url=NULL')
                    db.execute("UPDATE queue SET status='cancelled',body=NULL WHERE status='pending'")
                    db.commit()

    def diagnostic(self, code):
        allowed = DIAGNOSTIC_CODES
        if code not in allowed:
            raise Refused('DIAGNOSTIC_CODE_REFUSED')
        with self.diagnostic_lock:
            path = self.run / 'native-diagnostics.json'
            counts = json.loads(path.read_text()) if path.exists() else {}
            counts = {k: v for k, v in counts.items() if k in allowed and type(v) is int and v >= 0}
            counts[code] = counts.get(code, 0) + 1
            temp = self.run / 'native-diagnostics.pending'
            temp.write_text(json.dumps(counts, sort_keys=True))
            temp.replace(path)

    def lifecycle(self, code):
        if code not in LIFECYCLE_CODES:
            raise Refused('DIAGNOSTIC_CODE_REFUSED')
        with self.diagnostic_lock:
            path = self.run / 'native-lifecycle.jsonl'
            kept = []
            try:
                for line in path.read_text(errors='replace').splitlines() if path.exists() else ():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if (type(entry) is dict and set(entry) == {'code', 'utc'} and type(entry['code']) is str
                            and entry['code'] in LIFECYCLE_CODES and type(entry['utc']) is str
                            and LIFECYCLE_STAMP.fullmatch(entry['utc'])):
                        kept.append(entry)
                kept.append({'code': code, 'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
                temp = self.run / 'native-lifecycle.pending'
                temp.write_text(''.join(json.dumps(entry, sort_keys=True) + '\n' for entry in kept[-LIFECYCLE_LIMIT:]))
                temp.replace(path)
            except OSError:
                pass  # Evidence only: a failed write never blocks stop or cleanup.

    def authorized(self, principal, channel):
        if principal != OWNER or channel != CHANNEL or self.disabled:
            return False
        try:
            preflight()
            return not (self.run / 'STOP').exists()
        except Exception:
            return False

    def _connect(self, remaining):
        if not self.authorized(OWNER, CHANNEL):
            raise Refused('SOURCE_ACCESS_REVOKED')
        return connect_source(remaining)

    def source_summary(self):
        return {'connection_attempts': self.source.connections if self.source else 0,
                'source_messages': self.source.messages if self.source else 0,
                'unique_events': self.source.unique_events if self.source else 0,
                'possible_gap': self.source.possible_gap if self.source else False,
                'outcome': self.source.outcome if self.source else 'NO_MONITORING'}

    def monitor(self):
        try:
            from public_runtime import SingleTrial
            with SingleTrial(self.run / 'source-worker.lock'):
                self.source.run()
        finally:
            with self.events.lock:
                for sid, in self.events.db.execute('SELECT id FROM subscriptions').fetchall():
                    self.events.cancel(sid)
            (self.run / 'native-monitor-ended.json').write_text(json.dumps({
                **self.source_summary(), 'signing_secrets_cleared': True}))

    def handle(self, request):
        if request.get('method') != 'events/unsubscribe':
            return self.dispatch(request)
        self.lifecycle('UNSUBSCRIBE_RECEIVED')
        try:
            result = self.dispatch(request)
        except Exception:
            self.lifecycle('UNSUBSCRIBE_REFUSED')
            raise
        if 'result' not in result:
            self.lifecycle('UNSUBSCRIBE_REFUSED')
        return result

    def unsubscribe(self, request):
        # Monitoring stops only when this request cancelled the active
        # subscription. A refused or unrelated request leaves the source running.
        active = 'SELECT 1 FROM subscriptions WHERE active=1'
        with self.events.lock:
            before = self.events.db.execute(active).fetchone()
            result = Bridge(None, self.events).handle(request, OWNER)
            cancelled = 'result' in result and before and not self.events.db.execute(active).fetchone()
        if cancelled:
            if self.source and not self.source.stop.is_set():
                self.source.stop.set()
                self.lifecycle('MONITOR_STOP_UNSUBSCRIBE')
            self.lifecycle('UNSUBSCRIBE_SUCCEEDED')
        elif 'result' in result:
            self.lifecycle('UNSUBSCRIBE_NOT_ACTIVE')
        return result

    def dispatch(self, request):
        method = request.get('method')
        ident = request.get('id')
        if not self.authorized(OWNER, CHANNEL):
            raise Refused('AUTH_SCOPE_REFUSED')
        if method == 'initialize':
            return {'jsonrpc': '2.0', 'id': ident, 'result': {'protocolVersion': PROTOCOL,
                    'capabilities': {'tools': {}, 'events': {}},
                    'serverInfo': {'name': 'mattermost-native-event-trial', 'version': '0.3.0'}}}
        if method in ('notifications/initialized', 'notifications/cancelled'):
            return None
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': ident, 'result': {}}
        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': ident, 'result': {'tools': []}}
        if method not in {'server/discover', 'events/list', 'events/subscribe', 'events/unsubscribe'}:
            raise Refused('NATIVE_EVENT_METHOD_ONLY')
        # Discovery uses no callback secrets, no persistent state and no connection.
        if self.events is None and method in {'server/discover', 'events/list'}:
            # Pure schema helper fixture has no filesystem/secret/network state.
            bridge = object.__new__(Bridge)
            bridge.old_gate = None
            bridge.events = self
            return bridge.handle(request, OWNER)
        if method == 'events/subscribe' and self.events is None:
            # All runtime preparations are already complete. The initial grant
            # starts only at the formal subscription, never at tunnel startup.
            self.events = Events(self.run / 'events.protected.sqlite', trial_deadline=time.time() + 300,
                                   transport=ProtectedHTTPS(), protector=Dpapi(), authorized=self.authorized, diagnostic=self.diagnostic)
        if self.events is None:
            if method == 'events/unsubscribe':
                self.lifecycle('UNSUBSCRIBE_NOT_ACTIVE')
            return {'jsonrpc': '2.0', 'id': ident, 'result': {}}
        if method == 'events/unsubscribe':
            return self.unsubscribe(request)
        result = Bridge(None, self.events).handle(request, OWNER)
        if method == 'events/subscribe' and 'result' in result and self.worker is None:
            # Rebase the finite grant after callback verification. No source connection
            # has occurred. A process restart refuses resumption of this grant.
            start = time.time()
            ttl = granted_ttl(request.get('params', {}))
            deadline = start + ttl / 1000
            with self.events.db:
                self.events.db.execute('UPDATE trial SET deadline=?,start_ms=?', (deadline, int(start * 1000)))
                self.events.db.execute('UPDATE subscriptions SET expiry=MIN(?,?) WHERE active=1',
                                       (deadline, deadline))
            self.events.deadline, self.events.start_ms = deadline, int(start * 1000)
            result['result']['refreshBefore'] = __import__('mcp_events_candidate').iso(deadline)
            (self.run / 'native-started.json').write_text(json.dumps({'started': start, 'deadline': deadline}))
            self.diagnostic('MONITOR_STARTED')
            self.source = WebSocketSource(self._connect, self.events.enqueue_metadata, self.events.drain,
                                started=start, deadline=deadline, authorized=lambda: self.authorized(OWNER, CHANNEL))
            self.worker = threading.Thread(target=self.monitor, daemon=True)
            self.worker.start()
        return result

    def finish(self, cause='FINISH_UNSPECIFIED'):
        self.disabled = True
        requested = bool(self.source and not self.source.stop.is_set())
        if self.source:
            self.source.stop.set()
        if requested:
            self.lifecycle('MONITOR_STOP_FINISH')
        self.lifecycle(cause if cause in LIFECYCLE_CODES else 'FINISH_UNSPECIFIED')
        if self.worker:
            self.worker.join(timeout=32)
        if self.events:
            with self.events.lock:
                for sid, in self.events.db.execute('SELECT id FROM subscriptions').fetchall():
                    self.events.cancel(sid)
                self.events.close()
        summary = {**self.source_summary(),
                   'callback_secrets_cleared': True, 'source_stopped': not self.worker or not self.worker.is_alive()}
        (self.run / 'native-result.json').write_text(json.dumps(summary))
        self.diagnostic('CLEANUP_COMPLETED')


def main():
    run = safe_path(Path(os.environ['MM_NATIVE_RUN_DIR']))
    if run.parent != PARENT / 'runtime' or not run.name.startswith('run-'):
        raise Refused('RUN_PATH_REFUSED')
    from public_runtime import SingleTrial
    with SingleTrial(run / 'service.lock'):
        service = Service(run)
        cause = 'FINISH_SERVICE_EXCEPTION'
        try:
            for raw in iter(lambda: sys.stdin.buffer.readline(MAX_WIRE + 1), b''):
                response = None
                ident = None
                try:
                    request = strict_json(raw)
                    ident = request.get('id')
                    if (request.get('jsonrpc') != '2.0' or type(request.get('method')) is not str
                            or set(request) - {'jsonrpc', 'id', 'method', 'params'}
                            or type(request.get('params', {})) is not dict
                            or (ident is not None and type(ident) not in (str, int))):
                        raise Refused('RPC_ENVELOPE_REFUSED')
                    response = service.handle(request)
                except Exception:
                    response = {'jsonrpc': '2.0', 'id': ident,
                                'error': {'code': -32600, 'message': 'Native event trial refused request'}}
                if response is not None:
                    # Never echo request, callback URL, secret or exception bodies.
                    try:
                        sys.stdout.write(json.dumps(response) + '\n')
                        sys.stdout.flush()
                    except BaseException:
                        # Recorded and re-raised: the loop never continues.
                        cause = 'FINISH_OUTPUT_FAILURE'
                        service.lifecycle('OUTPUT_FAILURE')
                        raise
            cause = 'FINISH_STDIN_EOF'
            service.lifecycle('STDIN_EOF')
        except BaseException:
            if cause == 'FINISH_SERVICE_EXCEPTION':
                service.lifecycle('SERVICE_EXCEPTION')
            raise
        finally:
            service.finish(cause)
