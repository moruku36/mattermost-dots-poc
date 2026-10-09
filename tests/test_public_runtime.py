"""Synthetic adapter contracts; no real credential or source connection."""
import json
import unittest
from unittest.mock import patch
import public_runtime
from notification_core import CHANNEL, Refused


class Connection:
    def recv(self, timeout):
        return '{"event":"hello"}'
    def close(self):
        self.closed = True


class PublicRuntimeTests(unittest.TestCase):
    def test_fixed_wss_header_bounds_tls_no_logs_or_proxy(self):
        calls = []
        connection = Connection()
        with patch.object(public_runtime, 'preflight', lambda: None), \
             patch.dict(public_runtime.os.environ, {'MM_BASE_URL': 'https://source.example.invalid',
                                                    'MM_TOKEN': 'public-fixture-token-not-real'}), \
             patch('websockets.sync.client.connect', lambda *a, **k: calls.append((a, k)) or connection):
            source = public_runtime.connect_source(3)
        args, kwargs = calls[0]
        self.assertEqual(('wss://source.example.invalid/api/v4/websocket',), args)
        self.assertEqual({'Authorization': 'Bearer public-fixture-token-not-real'}, kwargs['additional_headers'])
        self.assertEqual(3, kwargs['open_timeout'])
        self.assertEqual(65536, kwargs['max_size'])
        self.assertEqual(4, kwargs['max_queue'])
        self.assertIsNone(kwargs['proxy'])
        self.assertIsNone(kwargs['compression'])
        self.assertTrue(kwargs['ssl'].check_hostname)
        self.assertTrue(kwargs['logger'].disabled)
        self.assertFalse(kwargs['logger'].propagate)
        self.assertEqual('{"event":"hello"}', source.recv(0.1))
        source.close()
        self.assertTrue(connection.closed)

    def test_redirect_and_auth_failure_refused_without_reconnect(self):
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        for code in (302, 401, 403):
            with patch.object(public_runtime, 'preflight', lambda: None), \
                 patch.dict(public_runtime.os.environ, {'MM_BASE_URL': 'https://source.example.invalid', 'MM_TOKEN': 'fixture'}), \
                 patch('websockets.sync.client.connect', side_effect=InvalidStatus(Response(code, 'fixture', Headers()))) as connect:
                with self.assertRaisesRegex(Refused, 'SOURCE_HANDSHAKE_REFUSED'):
                    public_runtime.connect_source(3)
                self.assertEqual(1, connect.call_count)

    def test_protocol_oversize_close_is_fatal(self):
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close
        connection = Connection()
        with patch.object(connection, 'recv', side_effect=ConnectionClosedError(Close(1009, 'fixture'), None, None)):
            with self.assertRaises(Refused):
                public_runtime.SourceSocket(connection).recv(0.1)

    def test_default_preflight_refuses_without_secret_or_approval(self):
        with patch.dict(public_runtime.os.environ, {}, clear=True):
            with self.assertRaises(Refused):
                public_runtime.preflight()
