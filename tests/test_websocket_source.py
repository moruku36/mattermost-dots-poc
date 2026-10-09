"""Fake-clock WebSocket lifecycle, scope, and reconnect checks."""
import json
import unittest
from notification_core import CHANNEL, Refused
from source_scope import WebSocketSource, posted_metadata


def posted(ident='b', channel=CHANNEL, **fields):
    return {'event': 'posted', 'broadcast': {'channel_id': channel}, 'data': {'post': json.dumps(
        {'channel_id': channel, 'id': ident * 26, 'create_at': 1000001,
         'message': 'PRIVATE_BODY_FIXTURE', 'user_id': 'PRIVATE_AUTHOR_FIXTURE', **fields})}}


class Socket:
    def __init__(self, now, messages):
        self.now, self.messages, self.closed = now, list(messages), False
    def recv(self, timeout):
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item()
            return json.dumps(item)
        self.now[0] += timeout
        raise TimeoutError()
    def close(self):
        self.closed = True


class SourceTests(unittest.TestCase):
    def make(self, sequences, deadline=1300, authorized=lambda: True):
        now, output, calls, sockets = [1000.0], [], [], []
        def connect(remaining):
            calls.append((now[0], remaining))
            sequence = sequences.pop(0)
            if isinstance(sequence, Exception):
                raise sequence
            sock = Socket(now, sequence)
            sockets.append(sock)
            return sock
        source = WebSocketSource(connect, output.append, lambda: None, clock=lambda: now[0],
            monotonic=lambda: now[0], wait=lambda s: now.__setitem__(0, now[0] + s),
            started=1000, deadline=deadline, authorized=authorized)
        return source, now, output, calls, sockets

    def test_reconnect_keeps_dedup_budget_and_original_deadline(self):
        hello = {'event': 'hello'}
        source, now, output, calls, sockets = self.make([
            [hello, posted(), ConnectionError()],
            [hello, posted(), posted('c'), posted('d'), posted('e'), posted('f'), posted('g')]])
        self.assertEqual([], calls)
        source.run()
        self.assertEqual(5, len(output))
        self.assertEqual(5, source.unique_events)
        self.assertEqual(2, source.connections)
        self.assertEqual(1300, now[0])
        self.assertEqual(299, calls[1][1])
        self.assertTrue(source.possible_gap)
        self.assertTrue(all(s.closed for s in sockets))
        self.assertTrue(all(set(p) == {'channel_id', 'post_id', 'created_ms'} for p in output))
        self.assertNotIn('PRIVATE', json.dumps(output))

    def test_other_channel_nonposted_old_system_deleted_ignored(self):
        cases = [posted(channel='z' * 26), {'event': 'post_edited'}, posted(create_at=999999),
                 posted(type='system_join_channel'), posted(delete_at=1000002)]
        cases[0]['data']['post'] = 'malformed other-channel body'
        for event in cases:
            self.assertIsNone(posted_metadata(event, 1000000))
        self.assertEqual('b' * 26, posted_metadata(posted(), 1000000)['post_id'])

    def test_mismatched_channel_and_malformed_metadata_stop(self):
        for event in (posted(create_at=True), posted(id='invalid'), posted(channel_id='z' * 26)):
            with self.assertRaises(Refused):
                posted_metadata(event, 1000000)

    def test_connection_budget_backoff_and_fatal_failure(self):
        source, now, output, calls, sockets = self.make([ConnectionError()] * 3)
        source.run()
        self.assertEqual(3, len(calls))
        self.assertEqual([1000, 1001, 1003], [c[0] for c in calls])
        self.assertEqual('SOURCE_CONNECTION_LIMIT', source.outcome)
        source, *rest = self.make([Refused('fixture auth failure')])
        source.run()
        self.assertEqual(1, source.connections)
        self.assertEqual('SOURCE_STOPPED_SAFELY', source.outcome)

    def test_prehello_post_rejected_and_hello_timeout(self):
        for sequence in ([posted()], []):
            source, now, output, calls, sockets = self.make([sequence])
            source.run()
            self.assertEqual([], output)
            self.assertEqual('SOURCE_STOPPED_SAFELY', source.outcome)
            self.assertTrue(sockets[0].closed)
            self.assertLessEqual(now[0], 1005)

    def test_stop_revoke_and_expired_do_not_connect(self):
        source, now, output, calls, sockets = self.make([], authorized=lambda: False)
        source.run()
        self.assertEqual([], calls)
        self.assertEqual('SOURCE_ACCESS_REVOKED', source.outcome)
        source, now, output, calls, sockets = self.make([])
        source.stop.set()
        source.run()
        self.assertEqual([], calls)
        source, now, output, calls, sockets = self.make([])
        now[0] = 1300
        source.run()
        self.assertEqual([], calls)
        self.assertEqual('EXPIRED', source.outcome)

    def test_event_received_at_deadline_never_forwarded(self):
        source, now, output, calls, sockets = self.make([[]], deadline=1001)
        def late():
            now[0] = 1001
            return json.dumps(posted())
        # Fixed fixture connection, no real network.
        sock = Socket(now, [{'event': 'hello'}, late])
        source.connect = lambda remaining: sock
        source.run()
        self.assertEqual([], output)
        self.assertTrue(sock.closed)

    def test_raw_bound_duplicate_keys_and_message_budget(self):
        for raw in ('x' * 65537, '{"event":"hello","event":"posted"}'):
            source, now, output, calls, sockets = self.make([[lambda: raw]])
            source.run()
            self.assertEqual('SOURCE_STOPPED_SAFELY', source.outcome)
            self.assertEqual(1, source.connections)
        source, now, output, calls, sockets = self.make([[{'event': 'hello'}] + [{'event': 'typing'}] * 1000])
        source.run()
        self.assertEqual(1000, source.messages)
        self.assertEqual('SOURCE_STOPPED_SAFELY', source.outcome)

    def test_idle_drain_runs_and_ttl_not_extended(self):
        source, now, output, calls, sockets = self.make([[{'event': 'hello'}]], deadline=1001)
        drains = []
        source.drain = lambda: drains.append(now[0])
        source.run()
        self.assertTrue(drains)
        self.assertEqual(1001, now[0])
        self.assertEqual('EXPIRED', source.outcome)

    def test_wall_clock_rollback_does_not_extend_socket_window(self):
        wall, mono = [1000.0], [0.0]
        class IdleSocket:
            closed = False
            def recv(self, timeout):
                wall[0] -= 1
                mono[0] += timeout
                raise TimeoutError()
            def close(self):
                self.closed = True
        sock = IdleSocket()
        # Hello is already received once; subsequent reads roll back wall time.
        original = sock.recv
        first = [True]
        def recv(timeout):
            if first[0]:
                first[0] = False
                return '{"event":"hello"}'
            return original(timeout)
        sock.recv = recv
        source = WebSocketSource(lambda _: sock, lambda _: self.fail('unexpected event'), lambda: None,
            clock=lambda: wall[0], monotonic=lambda: mono[0], started=1000, deadline=1001)
        source.run()
        self.assertEqual(1, mono[0])
        self.assertEqual('EXPIRED', source.outcome)
        self.assertTrue(sock.closed)
