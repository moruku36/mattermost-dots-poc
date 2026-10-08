import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from mcp_events_candidate import Bridge, Events, FixtureTransport, PROTOCOL
from notification_core import CHANNEL, Refused

SECRET = 'whsec_' + base64.b64encode(b'public-fixture-key-32-bytes-long!!').decode()
URL = 'https://callback.fixture.invalid/events'


class OldGate:
    def __init__(self):
        self.reads = 0
    def handle(self, request):
        if request['method'] == 'tools/call':
            self.reads += 1
            return {'error': 'read consumed'} if self.reads > 1 else {'result': 'dummy single read'}
        return {'result': 'old gate'}


class NativeEventsTests(unittest.TestCase):
    def test_normal_delivery_envelope_is_distinct_from_verification(self):
        result=self.events.subscribe('fixture-owner',self.params)
        verification=json.loads(self.sent[0][2])
        self.assertEqual({'type','challenge'},set(verification))
        self.assertEqual('verification',verification['type'])
        post=self.post()
        self.events.enqueue_metadata(post)
        self.events.drain()
        self.assertEqual(2,len(self.sent))
        _,headers,raw=self.sent[1]
        event=json.loads(raw)
        self.assertEqual({'eventId','name','timestamp','data','cursor'},set(event))
        self.assertEqual('message.created',event['name'])
        self.assertEqual('1970-01-01T00:16:40Z',event['timestamp'])
        self.assertEqual(post,event['data'])
        self.assertIsNone(event['cursor'])
        self.assertEqual(event['eventId'],headers['webhook-id'])
        self.assertEqual(result['id'],headers['X-MCP-Subscription-Id'])
        self.assertEqual('1000',headers['webhook-timestamp'])
        self.assertEqual('application/json',headers['Content-Type'])
        self.assertTrue(headers['webhook-signature'].startswith('v1,'))
        # callback() independently verifies HMAC over exactly these body bytes.
        self.events.enqueue_metadata(post)
        self.events.drain()
        self.assertEqual(2,len(self.sent),'duplicate event is not redelivered')

    def test_official_optional_fields_keep_scope_and_finite_default(self):
        for optional in ({}, {'cursor': None}, {'maxAgeMs': 0}, {'maxAgeMs': 300000},
                         {'ttlMs': None}, {'ttlMs': 86400000},
                         {'_meta': {'progressToken': 'synthetic-private-marker'}},
                         {'maxAgeMs': 300000, 'ttlMs': None, '_meta': {}}):
            with self.subTest(optional_fields=tuple(optional)):
                result = self.events.subscribe('fixture-owner', self.params | optional)
                self.assertEqual('1970-01-01T00:21:40Z', result['refreshBefore'])
                self.assertIsNone(result['cursor'])
        self.assertEqual(1, len(self.sent), 'idempotency is unchanged')

    def test_scope_rejections_identify_fixed_fields_without_values(self):
        cases = [('PARAMS_TYPE', None),
                 ('PARAMS_UNKNOWN_FIELDS', self.params | {'synthetic-private-key': SECRET}),
                 ('META_TYPE', self.params | {'_meta': None}),
                 ('NAME_MISSING', {k:v for k,v in self.params.items() if k != 'name'}),
                 ('NAME_TYPE', self.params | {'name': 1}),
                 ('NAME_SCOPE', self.params | {'name': 'synthetic-private-marker'}),
                 ('ARGUMENTS_MISSING', {k:v for k,v in self.params.items() if k != 'arguments'}),
                 ('ARGUMENTS_TYPE', self.params | {'arguments': []}),
                 ('ARGUMENTS_UNKNOWN_FIELDS', self.params | {'arguments': {'channel_id': CHANNEL, 'synthetic-private-key': SECRET}}),
                 ('CHANNEL_MISSING', self.params | {'arguments': {}}),
                 ('CHANNEL_TYPE', self.params | {'arguments': {'channel_id': True}}),
                 ('CHANNEL_SCOPE', self.params | {'arguments': {'channel_id': 'synthetic-private-marker'}}),
                 ('MAX_AGE_TYPE', self.params | {'maxAgeMs': None}),
                 ('MAX_AGE_TYPE', self.params | {'maxAgeMs': True}),
                 ('MAX_AGE_TYPE', self.params | {'maxAgeMs': -1}),
                 ('CURSOR_TYPE', self.params | {'cursor': 3}),
                 ('CURSOR_REPLAY', self.params | {'cursor': 'synthetic-private-marker'}),
                 ('DELIVERY_MISSING', {k:v for k,v in self.params.items() if k != 'delivery'}),
                 ('DELIVERY_TYPE', self.params | {'delivery': []})]
        delivery = self.params['delivery']
        for field in ('mode', 'url', 'secret'):
            cases.append(('DELIVERY_' + field.upper() + '_MISSING', self.params | {'delivery': {k:v for k,v in delivery.items() if k != field}}))
            cases.append(('DELIVERY_' + field.upper() + '_TYPE', self.params | {'delivery': delivery | {field: None}}))
        cases.extend([('DELIVERY_UNKNOWN_FIELDS', self.params | {'delivery': delivery | {'synthetic-private-key': SECRET}}),
                      ('DELIVERY_MODE_SCOPE', self.params | {'delivery': delivery | {'mode':'poll'}})])
        for code, params in cases:
            with self.subTest(reason=code):
                codes = []
                self.events.diagnostic = codes.append
                result = Bridge(OldGate(), self.events).handle({'jsonrpc':'2.0', 'id':1, 'method':'events/subscribe', 'params':params}, 'fixture-owner')
                self.assertEqual(code.lower(), result['error']['data']['reason'])
                self.assertIn('FAIL_' + code, codes)
                self.assertNotIn('CALLBACK_VALIDATION_STARTED', codes)
                rendered = json.dumps(result) + json.dumps(codes)
                for private in (SECRET, URL, 'synthetic-private-key', 'synthetic-private-marker'):
                    self.assertNotIn(private, rendered)
        self.assertEqual([], self.sent)
        self.assertEqual(0, self.events.db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0])

    def test_envelope_and_principal_have_distinct_fixed_reasons(self):
        valid={'jsonrpc':'2.0','id':1,'method':'events/subscribe','params':self.params}
        cases=[('REQUEST_TYPE', [], 'fixture-owner'),
               ('REQUEST_JSONRPC',valid | {'jsonrpc':'bad'},'fixture-owner'),
               ('REQUEST_FIELDS',valid | {'synthetic-private-key':SECRET},'fixture-owner'),
               ('REQUEST_ID',valid | {'id':True},'fixture-owner'),
               ('REQUEST_METHOD',valid | {'method':None},'fixture-owner'),
               ('PRINCIPAL_TYPE',valid,None),('PRINCIPAL_EMPTY',valid,''),
               ('PRINCIPAL_ACCESS',valid,'synthetic-private-marker')]
        for code,request,principal in cases:
            with self.subTest(reason=code):
                codes=[]
                self.events.diagnostic=codes.append
                result=Bridge(OldGate(),self.events).handle(request,principal)
                self.assertEqual(code.lower(),result['error']['data']['reason'])
                self.assertIn('FAIL_'+code,codes)
                self.assertNotIn(SECRET,json.dumps(result)+json.dumps(codes))
        self.assertEqual([],self.sent)

    def test_null_and_long_requested_ttl_grant_finite_trial(self):
        for ttl in (None, 86400000):
            result = self.events.subscribe('fixture-owner', self.params | {'ttlMs': ttl})
            self.assertEqual('1970-01-01T00:21:40Z', result['refreshBefore'])
        self.assertEqual(1, len(self.sent))

    def test_callback_contract_diagnostics_and_no_secret_in_errors(self):
        codes = []
        self.events.diagnostic = codes.append
        self.events.subscribe('fixture-owner', self.params | {'ttlMs': None})
        self.assertEqual(['SUBSCRIBE_RECEIVED', 'CALLBACK_VALIDATION_STARTED',
                          'CALLBACK_VALIDATION_SUCCEEDED', 'SUBSCRIPTION_SAVED'], codes)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'events/subscribe', 'params': self.params | {'ttlMs': 0}}
        result = Bridge(OldGate(), self.events).handle(request, 'fixture-owner')
        self.assertEqual('ttl_refused', result['error']['data']['reason'])
        self.assertIn('FAIL_TTL_REFUSED', codes)
        self.assertNotIn(SECRET, json.dumps(result) + json.dumps(codes))
        self.assertNotIn(URL, json.dumps(result) + json.dumps(codes))

    def test_callback_transport_exception_is_fixed_and_not_saved(self):
        codes = []
        self.events.diagnostic = codes.append
        def fail(*args):
            raise OSError('private callback and key must never be logged: ' + SECRET)
        self.transport.handler = fail
        result = Bridge(OldGate(), self.events).handle({'jsonrpc': '2.0', 'id': 1,
          'method': 'events/subscribe', 'params': self.params}, 'fixture-owner')
        self.assertEqual('transport_failed', result['error']['data']['reason'])
        self.assertEqual(0, self.events.db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0])
        self.assertIn('CALLBACK_VALIDATION_FAILED', codes)
        self.assertNotIn(SECRET, json.dumps(result) + json.dumps(codes))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'dummy-events.sqlite'
        self.now, self.access = 1000.0, True
        self.sent, self.statuses = [], []
        self.transport = FixtureTransport(self.callback)
        self.events = self.open()
        self.params = {'name': 'message.created', 'arguments': {'channel_id': CHANNEL},
                       'delivery': {'mode': 'webhook', 'url': URL, 'secret': SECRET}, 'cursor': None}

    def open(self):
        return Events(self.path, trial_deadline=1300, transport=self.transport,
                      authorized=lambda owner, channel: owner == 'fixture-owner' and self.access and channel == CHANNEL,
                      clock=lambda: self.now)

    def tearDown(self):
        self.events.close()
        self.temp.cleanup()

    def callback(self, url, headers, body):
        self.sent.append((url, dict(headers), bytes(body)))
        value = json.loads(body)
        if value.get('type') == 'verification':
            # Verify independently using the public fixture secret.
            msg = headers['webhook-id'].encode() + b'.' + headers['webhook-timestamp'].encode() + b'.' + body
            expected = 'v1,' + base64.b64encode(hmac.new(base64.b64decode(SECRET[6:]), msg, hashlib.sha256).digest()).decode()
            self.assertIn(expected, headers['webhook-signature'].split())
            return 200, {'challenge': value['challenge']}
        return (self.statuses.pop(0) if self.statuses else 200), {}

    def post(self, char='a'):
        return {'channel_id': CHANNEL, 'post_id': char * 26, 'created_ms': 1000000}

    def test_discovery_and_old_gate_preserved(self):
        gate = OldGate()
        bridge = Bridge(gate, self.events)
        def req(method):
            return {'jsonrpc': '2.0', 'id': 1, 'method': method}
        result = bridge.handle(req('server/discover'), 'fixture-owner')['result']
        self.assertEqual([PROTOCOL], result['supportedVersions'])
        self.assertIn('events', result['capabilities'])
        self.assertEqual('message.created', bridge.handle(req('events/list'), 'fixture-owner')['result']['events'][0]['name'])
        self.assertIn('result', bridge.handle(req('tools/call'), 'fixture-owner'))
        self.assertIn('error', bridge.handle(req('tools/call'), 'fixture-owner'))
        self.assertIn('error', bridge.handle(req('events/list'), 'other-owner'))

    def test_subscribe_idempotent_and_no_expiry_extension(self):
        first = self.events.subscribe('fixture-owner', self.params)
        self.now = 1010
        second = self.events.subscribe('fixture-owner', self.params)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['refreshBefore'], second['refreshBefore'])
        self.assertEqual(1, len(self.sent))
        self.assertIsNone(first['cursor'])

    def test_wrong_scope_secret_callback_and_replay_refused(self):
        bad = [self.params | {'name': 'other'}, self.params | {'arguments': {'channel_id': 'other'}},
               self.params | {'cursor': 'fake'}, self.params | {'ttlMs': 0},
               self.params | {'delivery': self.params['delivery'] | {'secret': 'whsec_YQ=='}},
               self.params | {'delivery': self.params['delivery'] | {'url': 'http://127.0.0.1/events'}}]
        for params in bad:
            with self.assertRaises(Refused):
                self.events.subscribe('fixture-owner', params)
        self.assertEqual([], self.sent)

    def test_verification_failure_never_activates(self):
        self.events.transport = FixtureTransport(lambda *args: (200, {'challenge': 'wrong'}))
        with self.assertRaisesRegex(Refused, 'CALLBACK_CHALLENGE_FAILED'):
            self.events.subscribe('fixture-owner', self.params)
        self.assertEqual(0, self.events.db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0])

    def test_signed_metadata_only_duplicate_delivery(self):
        result = self.events.subscribe('fixture-owner', self.params)
        self.events.enqueue_metadata(self.post())
        self.events.enqueue_metadata(self.post())
        self.events.drain()
        self.assertEqual(2, len(self.sent))
        _, headers, raw = self.sent[-1]
        value = json.loads(raw)
        self.assertEqual(headers['webhook-id'], value['eventId'])
        self.assertEqual(result['id'], headers['X-MCP-Subscription-Id'])
        self.assertEqual({'channel_id', 'post_id', 'created_ms'}, set(value['data']))
        self.assertNotIn('text', value['data'])
        with self.assertRaises(Refused):
            self.events.enqueue_metadata(self.post() | {'text': 'secret'})

    def test_retry_retains_id_body_and_refreshes_signing_timestamp(self):
        self.events.subscribe('fixture-owner', self.params)
        self.events.enqueue_metadata(self.post())
        self.statuses = [503, 200]
        self.events.drain()
        self.now += 2
        self.events.drain()
        a, b = self.sent[-2:]
        self.assertEqual(a[1]['webhook-id'], b[1]['webhook-id'])
        self.assertEqual(a[2], b[2])
        self.assertNotEqual(a[1]['webhook-timestamp'], b[1]['webhook-timestamp'])
        self.assertEqual('delivered', self.events.db.execute('SELECT status FROM queue').fetchone()[0])

    def test_410_413_no_retry_and_max_attempts(self):
        self.events.subscribe('fixture-owner', self.params)
        self.events.enqueue_metadata(self.post())
        self.statuses = [413]
        self.events.drain()
        self.now += 10
        self.events.drain()
        self.assertEqual(2, len(self.sent))
        self.events.enqueue_metadata(self.post('b'))
        self.statuses = [503, 503, 503]
        for _ in range(4):
            self.events.drain()
            self.now += 10
        self.assertEqual(5, len(self.sent))
        self.events.enqueue_metadata(self.post('c'))
        self.statuses = [410]
        self.events.drain()
        self.assertEqual(0, self.events.db.execute('SELECT active FROM subscriptions').fetchone()[0])

    def test_restart_preserves_pending_budget_and_expiry(self):
        self.events.subscribe('fixture-owner', self.params)
        self.events.enqueue_metadata(self.post())
        self.events.close()
        self.events = self.open()
        self.events.drain()
        self.assertEqual(2, len(self.sent))
        for char in 'bcdefg':
            self.events.enqueue_metadata(self.post(char))
        self.assertEqual(5, self.events.db.execute('SELECT count FROM subscriptions').fetchone()[0])
        self.assertEqual(5, self.events.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0])

    def test_revocation_and_expiry_stop_and_clear_secret(self):
        self.events.subscribe('fixture-owner', self.params)
        self.events.enqueue_metadata(self.post())
        self.access = False
        self.events.drain()
        self.assertEqual(1, len(self.sent))
        self.assertIsNone(self.events.db.execute('SELECT secret FROM subscriptions').fetchone()[0])
        self.access = True
        self.events.subscribe('fixture-owner', self.params)
        self.now = 1300
        self.events.drain()
        with self.assertRaises(Refused):
            self.events.subscribe('fixture-owner', self.params)

    def test_unsubscribe_idempotent_owner_authorized(self):
        self.events.subscribe('fixture-owner', self.params)
        self.events.enqueue_metadata(self.post())
        params = self.params | {'delivery': {'mode': 'webhook', 'url': URL}}
        params.pop('cursor')
        with self.assertRaises(Refused):
            self.events.unsubscribe('other-owner', params)
        self.assertEqual({}, self.events.unsubscribe('fixture-owner', params))
        self.assertEqual({}, self.events.unsubscribe('fixture-owner', params))
        self.events.drain()
        self.assertEqual(1, len(self.sent))

    def test_expired_idle_subscription_clears_dummy_secret(self):
        self.events.subscribe('fixture-owner', self.params)
        self.now = 1300
        self.events.drain()
        self.assertEqual((0, None), self.events.db.execute('SELECT active,secret FROM subscriptions').fetchone())

    def test_key_rotation_two_signatures_then_old_key_removed(self):
        self.events.subscribe('fixture-owner', self.params)
        replacement = 'whsec_' + base64.b64encode(b'next-public-fixture-key-32-bytes!').decode()
        original_callback = self.events.transport.handler
        def rotated_callback(url, headers, body):
            value = json.loads(body)
            if value.get('type') == 'verification':
                return 200, {'challenge': value['challenge']}
            return original_callback(url, headers, body)
        self.events.transport = FixtureTransport(rotated_callback)
        self.events.subscribe('fixture-owner', self.params | {'delivery': self.params['delivery'] | {'secret': replacement}})
        self.events.enqueue_metadata(self.post())
        self.events.drain()
        self.assertEqual(2, len(self.sent[-1][1]['webhook-signature'].split()))
        self.now += 31
        self.events.enqueue_metadata(self.post('b'))
        self.events.drain()
        self.assertEqual(1, len(self.sent[-1][1]['webhook-signature'].split()))
        self.assertIsNone(self.events.db.execute('SELECT old_secret FROM subscriptions').fetchone()[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
