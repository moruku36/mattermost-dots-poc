import base64
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from notification_core import CHANNEL, Refused
from protected_delivery import Dpapi, callback_target, public_addresses, ProtectedHTTPS
from source_scope import posted_metadata


def source_result(posts=None):
    return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps({
      'bounded': True, 'complete_history': False, 'posts': posts or []})}]}


class PreparationTests(unittest.TestCase):
    def test_scope_diagnostic_file_accepts_only_finite_codes(self):
        import native_service
        from mcp_events_candidate import SCOPE_REASONS
        from public_runtime import OWNER
        with tempfile.TemporaryDirectory() as d:
            run=Path(d)
            (run/'session.json').write_text(json.dumps({'native_events_approved':True,
                'owner':OWNER,'channel':CHANNEL,'source_mode':'websocket','max_connections':3,'max_posts':5,
                'seconds':300,'policy':'public-fixture-policy'}))
            service=native_service.Service(run)
            for reason in SCOPE_REASONS:
                service.diagnostic('FAIL_'+reason)
            with self.assertRaisesRegex(Refused,'DIAGNOSTIC_CODE_REFUSED'):
                service.diagnostic('synthetic-private-arbitrary-value')
            counts=json.loads((run/'native-diagnostics.json').read_text())
            self.assertEqual({'FAIL_'+reason:1 for reason in SCOPE_REASONS},counts)
            self.assertNotIn('synthetic-private',json.dumps(counts))
            self.assertIsNone(service.events)
            self.assertIsNone(service.source)

    def test_process_restart_refuses_resume_and_clears_callback_state(self):
        import native_service
        from public_runtime import OWNER
        from mcp_events_candidate import Events, FixtureTransport
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / 'session.json').write_text(json.dumps({'native_events_approved': True,
                'owner': OWNER, 'channel': CHANNEL, 'source_mode': 'websocket',
                'max_connections': 3, 'max_posts': 5, 'seconds': 300}))
            events = Events(run / 'events.protected.sqlite', trial_deadline=time.time() + 300,
                transport=FixtureTransport(lambda *a: None), authorized=lambda *a: True)
            events.db.execute("INSERT INTO subscriptions(id,owner,url,secret,active,count) VALUES ('fixture','fixture','fixture','fixture',1,0)")
            events.db.commit()
            events.close()
            (run / 'native-started.json').write_text('{}')
            with patch.object(native_service, 'connect_source') as connect:
                service = native_service.Service(run)
                self.assertTrue(service.disabled)
                self.assertIsNone(service.worker)
                connect.assert_not_called()
            import sqlite3
            with sqlite3.connect(run / 'events.protected.sqlite') as db:
                self.assertEqual((0, None, None), db.execute('SELECT active,url,secret FROM subscriptions').fetchone())
            db.close()

    def test_callback_url_scope(self):
        for url in ('http://example.com/cb', 'https://u:p@example.com/cb', 'https://example.com:8080/cb',
                    'https://127.0.0.1/cb', 'https://[::1]/cb', 'https://example.com/cb#fragment',
                    'https://example.com/\r\nheader'):
            with self.assertRaises(Refused):
                callback_target(url)
        self.assertEqual('example.com', callback_target('https://example.com/cb?a=1').hostname)

    def test_public_dns_no_private_mixed_rebind(self):
        def records(addresses):
            return lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addresses]
        for addresses in (['127.0.0.1'], ['10.0.0.2'], ['169.254.169.254'], ['8.8.8.8', '192.168.1.2'], []):
            with self.assertRaises(Refused):
                public_addresses('example.com', records(addresses))
        self.assertEqual(['8.8.8.8'], public_addresses('example.com', records(['8.8.8.8'])))

    @unittest.skipUnless(os.name == 'nt', 'DPAPI Windows-only')
    def test_dpapi_public_fixture_roundtrip_not_plaintext(self):
        public = 'PUBLIC_FIXTURE_NOT_A_REAL_CREDENTIAL'
        protector = Dpapi()
        sealed = protector.seal(public)
        self.assertNotIn(public, sealed)
        self.assertEqual(public, protector.open(sealed))
        with self.assertRaises(Refused):
            protector.open(public)

    @unittest.skipUnless(os.name == 'nt', 'native supervisor Windows-only')
    def test_native_service_discovery_no_connection_subscription_dummy_tls_cleanup(self):
        import native_service
        from public_runtime import OWNER
        from mcp_events_candidate import PROTOCOL
        key = 'whsec_' + base64.b64encode(b'public-fixture-key-32-bytes-long!!').decode()
        seen, notified = [], threading.Event()
        def callback(self, url, headers, raw):
            body = json.loads(raw)
            if body.get('type') == 'verification':
                return 200, {'challenge': body['challenge']}
            seen.append(body)
            notified.set()
            return 200, {}
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / 'session.json').write_text(json.dumps({'native_events_approved': True, 'owner': OWNER,
              'channel': CHANNEL, 'source_mode': 'websocket', 'max_connections': 3, 'max_posts': 5, 'seconds': 300, 'policy': 'public-fixture-policy'}))
            reads = []
            class FixtureSocket:
                sent = False
                hello = False
                closed = False
                def recv(self, timeout):
                    if not self.hello:
                        self.hello = True
                        return json.dumps({'event': 'hello'})
                    if not self.sent:
                        self.sent = True
                        return json.dumps({'event': 'posted', 'broadcast': {'channel_id': CHANNEL},
                            'data': {'post': json.dumps({'channel_id': CHANNEL, 'id': 'b' * 26,
                                'create_at': int(time.time() * 1000), 'message': 'fixture body'})}})
                    time.sleep(timeout)
                    raise TimeoutError()
                def close(self):
                    self.closed = True
            source = FixtureSocket()
            def connect(*args):
                reads.append(1)
                return source
            with patch.object(native_service, 'preflight', lambda: 'public-fixture-policy'), \
                 patch.object(native_service, 'connect_source', connect), patch.object(ProtectedHTTPS, 'post', callback):
                service = native_service.Service(run)
                try:
                    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover'}
                    self.assertEqual([PROTOCOL], service.handle(request)['result']['supportedVersions'])
                    self.assertEqual([], reads)
                    params = {'name': 'message.created', 'arguments': {'channel_id': CHANNEL},
                              'delivery': {'mode': 'webhook', 'url': 'https://example.com/fixture-only', 'secret': key}, 'ttlMs': None}
                    response = service.handle(request | {'method': 'events/subscribe', 'params': params})
                    self.assertIn('result', response)
                    self.assertTrue(notified.wait(2))
                    self.assertEqual({'channel_id', 'post_id', 'created_ms'}, set(seen[0]['data']))
                    params['delivery'].pop('secret')
                    service.handle(request | {'method': 'events/unsubscribe', 'params': params})
                finally:
                    service.finish()
                self.assertEqual(1, len(reads))
                self.assertFalse(service.worker.is_alive())
                self.assertTrue(source.closed)
                self.assertNotIn(key.encode(), (run / 'events.protected.sqlite').read_bytes())
                summary = json.loads((run / 'native-result.json').read_text())
                self.assertTrue(summary['callback_secrets_cleared'])
                diagnostics = json.loads((run / 'native-diagnostics.json').read_text())
                for code in ('SUBSCRIBE_RECEIVED', 'CALLBACK_VALIDATION_STARTED', 'CALLBACK_VALIDATION_SUCCEEDED',
                             'SUBSCRIPTION_SAVED', 'MONITOR_STARTED', 'CLEANUP_COMPLETED'):
                    self.assertEqual(1, diagnostics[code])
                self.assertNotIn(key, json.dumps(diagnostics))
                self.assertNotIn('https:', json.dumps(diagnostics))
