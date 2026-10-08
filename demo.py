"""Offline synthetic event/expiry demonstration. No network or live credentials."""
import base64
import json
from pathlib import Path
import tempfile

from mcp_events_candidate import Events, FixtureTransport
from notification_core import CHANNEL


def main():
    now = [1000.0]
    delivered = []
    def callback(url, headers, raw):
        body = json.loads(raw)
        if body.get('type') == 'verification':
            return 200, {'challenge': body['challenge']}
        delivered.append(body)
        return 200, {}
    with tempfile.TemporaryDirectory() as directory:
        events = Events(Path(directory) / 'fixture.sqlite', trial_deadline=1300,
                        transport=FixtureTransport(callback), authorized=lambda p, c: p == 'fixture-owner' and c == CHANNEL,
                        clock=lambda: now[0])
        try:
            params = {'name': 'message.created', 'arguments': {'channel_id': CHANNEL},
                      'delivery': {'mode': 'webhook', 'url': 'https://callback.fixture.invalid/demo',
                                   'secret': 'whsec_' + base64.b64encode(b'public-fixture-only-not-a-live-key').decode()},
                      'ttlMs': 300000}
            events.subscribe('fixture-owner', params)
            events.enqueue_metadata({'channel_id': CHANNEL, 'post_id': 'b' * 26, 'created_ms': 1001000})
            events.drain()
            now[0] = 1300
            events.drain()
            active = events.db.execute('SELECT COUNT(*) FROM subscriptions WHERE active=1').fetchone()[0]
            protected = events.db.execute('SELECT COUNT(*) FROM subscriptions WHERE secret IS NOT NULL OR url IS NOT NULL').fetchone()[0]
            assert len(delivered) == 1 and active == 0 and protected == 0
            assert set(delivered[0]['data']) == {'channel_id', 'post_id', 'created_ms'}
            print('Offline fixture: delivered=1 active_after_expiry=0 callback_values_after_expiry=0 network_calls=0')
        finally:
            events.close()


if __name__ == '__main__':
    main()
