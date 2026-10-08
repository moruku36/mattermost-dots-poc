"""Offline public-adapter contracts; all HTTP and environment values are synthetic."""
import json
import time
import unittest
from unittest.mock import patch

from notification_core import CHANNEL, Refused
import public_runtime


class Connection:
    def __init__(self, status, data):
        self.status, self.data = status, data
        self.calls = []
        self.closed = False

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))

    def getresponse(self):
        return self

    def read(self, size):
        return self.data[:size]

    def close(self):
        self.closed = True


class PublicRuntimeTests(unittest.TestCase):
    def invoke(self, connection, arguments=None):
        with patch.object(public_runtime, 'preflight', lambda: None), \
             patch.dict(public_runtime.os.environ, {'MM_BASE_URL': 'https://source.example.invalid',
                                                    'MM_TOKEN': 'public-fixture-token-not-real'}), \
             patch.object(public_runtime.http.client, 'HTTPSConnection', lambda *a, **k: connection):
            return public_runtime.existing_backend(arguments or {'channel_id': CHANNEL, 'limit': 5},
                                                   {'deadline': time.time() + 10})

    def test_get_only_and_body_fields_are_discarded(self):
        ident = 'b' * 26
        post = {'id': ident, 'channel_id': CHANNEL, 'create_at': 1000,
                'message': 'PRIVATE_BODY_FIXTURE', 'user_id': 'PRIVATE_AUTHOR_FIXTURE'}
        connection = Connection(200, json.dumps({'order': [ident], 'posts': {ident: post}}).encode())
        result = self.invoke(connection)
        self.assertTrue(connection.closed)
        self.assertEqual('GET', connection.calls[0][0])
        self.assertEqual('/api/v4/channels/' + CHANNEL + '/posts?page=0&per_page=5', connection.calls[0][1])
        self.assertNotIn('PRIVATE_BODY_FIXTURE', json.dumps(result))
        self.assertNotIn('PRIVATE_AUTHOR_FIXTURE', json.dumps(result))
        payload = json.loads(result['content'][0]['text'])
        self.assertEqual([{'id': ident, 'channel_id': CHANNEL, 'create_at': 1000}], payload['posts'])

    def test_wrong_channel_refused_before_request(self):
        connection = Connection(200, b'{}')
        with self.assertRaises(Refused):
            self.invoke(connection, {'channel_id': 'not-approved', 'limit': 5})
        self.assertEqual([], connection.calls)

    def test_redirect_error_and_oversize_are_refused(self):
        for status, data in [(302, b'{}'), (401, b'{}'), (200, b'x' * 65537)]:
            connection = Connection(status, data)
            with self.assertRaises(Refused):
                self.invoke(connection)
            self.assertTrue(connection.closed)

    def test_more_than_five_or_wrong_scope_refused(self):
        for data in [{'order': ['b'] * 6, 'posts': {}},
                     {'order': ['b'], 'posts': {'b': {'id': 'b', 'channel_id': 'other'}}}]:
            with self.assertRaises(Refused):
                self.invoke(Connection(200, json.dumps(data).encode()))

    def test_default_preflight_refuses_without_secret_or_approval(self):
        with patch.dict(public_runtime.os.environ, {}, clear=True):
            with self.assertRaises(Refused):
                public_runtime.preflight()


if __name__ == '__main__':
    unittest.main()
