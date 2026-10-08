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
from source_scope import metadata, Poller


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
                'owner':OWNER,'channel':CHANNEL,'max_reads':60,'max_posts':5,
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
            self.assertIsNone(service.poller)

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

    def test_metadata_body_discard_and_wrong_channel(self):
        post = {'channel_id': CHANNEL, 'id': 'a' * 26, 'create_at': 1000,
                'message': 'untrusted body secret must never leave source memory', 'user_id': 'private-user'}
        extracted = metadata(source_result([post]), 900)
        self.assertEqual([{'channel_id': CHANNEL, 'post_id': 'a' * 26, 'created_ms': 1000}], extracted)
        self.assertNotIn('message', json.dumps(extracted))
        self.assertEqual([], metadata(source_result([post]), 1001))
        with self.assertRaises(Refused):
            metadata(source_result([post | {'channel_id': 'other'}]), 0)

    def test_full_page_possible_gap_stops_without_history(self):
        posts = [{'channel_id': CHANNEL, 'id': chr(97 + i) * 26, 'create_at': 1000} for i in range(5)]
        with self.assertRaisesRegex(Refused, 'SOURCE_POSSIBLE_GAP'):
            metadata(source_result(posts), 900)

    def test_poll_prepared_idle_and_exact_sixty_five_seconds(self):
        now, calls = [1000.0], []
        poll = Poller(lambda args, deadline: calls.append((now[0], args, deadline)) or source_result(),
                      lambda _: None, lambda: None, clock=lambda: now[0],
                      wait=lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertEqual([], calls)  # constructing candidate starts no monitoring
        poll.run()
        self.assertEqual(60, len(calls))
        self.assertTrue(all(x[1] == {'channel_id': CHANNEL, 'limit': 5} for x in calls))
        self.assertEqual(5, calls[1][0] - calls[0][0])
        self.assertEqual(1300, now[0])

    def test_poll_restart_keeps_count_deadline_interval(self):
        now, calls = [1000.0], []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'public-budget.sqlite'
            first = None
            def interrupted_wait(seconds):
                first.stop.set()
            first = Poller(lambda *args: calls.append(now[0]) or source_result(), lambda _: None, lambda: None,
                           clock=lambda: now[0], wait=interrupted_wait, state_path=path, started=1000, deadline=1300)
            first.run()
            self.assertEqual(1, first.reads)
            second = Poller(lambda *args: calls.append(now[0]) or source_result(), lambda _: None, lambda: None,
                clock=lambda: now[0], wait=lambda seconds: now.__setitem__(0, now[0] + seconds),
                state_path=path, started=1000, deadline=1300)
            second.run()
            self.assertEqual(60, second.reads)
            self.assertEqual(60, len(calls))
            self.assertEqual(1005, calls[1])

    def test_failed_poll_consumes_attempt_and_stops(self):
        def failed(*_):
            raise RuntimeError('fixture failure; no raw body logged')
        poll = Poller(failed, lambda _: None, lambda: None)
        poll.run()
        self.assertEqual(1, poll.reads)
        self.assertEqual('SOURCE_STOPPED_SAFELY', poll.outcome)

    @unittest.skipUnless(os.name == 'nt', 'native supervisor Windows-only')
    def test_native_service_discovery_no_poll_subscription_dummy_tls_cleanup(self):
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
              'channel': CHANNEL, 'max_reads': 60, 'max_posts': 5, 'seconds': 300, 'policy': 'public-fixture-policy'}))
            reads = []
            def read(*args):
                reads.append(1)
                return source_result([{'channel_id': CHANNEL, 'id': 'a' * 26, 'create_at': int(time.time() * 1000), 'message': 'fixture body'}])
            with patch.object(native_service, 'preflight', lambda: 'public-fixture-policy'), \
                 patch.object(native_service, 'existing_backend', read), patch.object(ProtectedHTTPS, 'post', callback):
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
                self.assertNotIn(key.encode(), (run / 'events.protected.sqlite').read_bytes())
                summary = json.loads((run / 'native-result.json').read_text())
                self.assertTrue(summary['callback_secrets_cleared'])
                diagnostics = json.loads((run / 'native-diagnostics.json').read_text())
                for code in ('SUBSCRIBE_RECEIVED', 'CALLBACK_VALIDATION_STARTED', 'CALLBACK_VALIDATION_SUCCEEDED',
                             'SUBSCRIPTION_SAVED', 'MONITOR_STARTED', 'CLEANUP_COMPLETED'):
                    self.assertEqual(1, diagnostics[code])
                self.assertNotIn(key, json.dumps(diagnostics))
                self.assertNotIn('https:', json.dumps(diagnostics))
