"""Real WebSocket framing on loopback with synthetic auth, never Mattermost."""
import json
import threading
import time
import unittest
from unittest.mock import patch
from websockets.sync.client import connect as real_connect
from websockets.sync.server import serve
from notification_core import CHANNEL
from source_scope import WebSocketSource
import public_runtime


class WireFixtureTests(unittest.TestCase):
    def test_header_auth_posted_reconnect_duplicate_other_channel_and_close(self):
        attempts, checks, output = [], [], []
        def post(ident, channel=CHANNEL):
            return json.dumps({'event': 'posted', 'broadcast': {'channel_id': channel},
                'data': {'post': json.dumps({'channel_id': channel, 'id': ident * 26,
                    'create_at': int(time.time() * 1000), 'message': 'PRIVATE_BODY_FIXTURE'})}})
        def handler(socket):
            index = len(attempts)
            attempts.append(1)
            checks.append((socket.request.path, socket.request.headers.get('Authorization')))
            socket.send(json.dumps({'event': 'hello'}))
            socket.send(post('b'))
            if index == 0:
                socket.close(1001)
            else:
                socket.send(post('c', 'z' * 26))
                socket.send(post('d'))
                socket.close(1000)
        with serve(handler, '127.0.0.1', 0) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            port = server.socket.getsockname()[1]
            def fixture_connect(uri, **kwargs):
                self.assertEqual('wss://source.example.invalid/api/v4/websocket', uri)
                # Only this test reroutes to an unencrypted loopback fixture.
                kwargs.pop('ssl')
                return real_connect(f'ws://127.0.0.1:{port}/api/v4/websocket', **kwargs)
            with patch.object(public_runtime, 'preflight', lambda: None), \
                 patch.dict(public_runtime.os.environ, {'MM_BASE_URL': 'https://source.example.invalid', 'MM_TOKEN': 'fixture-not-a-real-token'}), \
                 patch('websockets.sync.client.connect', fixture_connect):
                source = WebSocketSource(public_runtime.connect_source, output.append, lambda: None,
                    wait=lambda _: None, authorized=lambda: len(output) < 2)
                source.run()
            server.shutdown()
            worker.join(3)
        self.assertEqual(2, source.connections)
        self.assertEqual(2, len(output))
        self.assertEqual(['b' * 26, 'd' * 26], [p['post_id'] for p in output])
        self.assertTrue(source.possible_gap)
        self.assertTrue(source.stop.is_set())
        self.assertTrue(all(p == '/api/v4/websocket' and a == 'Bearer fixture-not-a-real-token' for p, a in checks))
        self.assertNotIn('PRIVATE', json.dumps(output))
