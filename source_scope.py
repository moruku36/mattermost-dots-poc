"""Finite authenticated posted-event source; never fetches history."""
import re
import threading
import time
from notification_core import CHANNEL, Refused, strict_json


def posted_metadata(event, floor_ms):
    if event.get('event') != 'posted':
        return None
    broadcast = event.get('broadcast')
    if type(broadcast) is not dict:
        raise Refused('SOURCE_ENVELOPE_REFUSED')
    # Drop other-channel events before parsing their post bodies.
    if broadcast.get('channel_id') != CHANNEL:
        return None
    data = event.get('data')
    if type(data) is not dict or type(data.get('post')) is not str:
        raise Refused('SOURCE_ENVELOPE_REFUSED')
    post = strict_json(data['post'].encode('utf-8'))
    if (post.get('channel_id') != CHANNEL or type(post.get('create_at')) is not int
            or type(post.get('id')) is not str
            or not re.fullmatch(r'[a-z0-9]{26}', post['id'])):
        raise Refused('SOURCE_SCOPE_REFUSED')
    if post.get('delete_at', 0) or post.get('type', '') or post['create_at'] < floor_ms:
        return None
    return {'channel_id': CHANNEL, 'post_id': post['id'], 'created_ms': post['create_at']}


class WebSocketSource:
    def __init__(self, connect, enqueue, drain, *, clock=time.time, monotonic=time.monotonic,
                 wait=None, started=None, deadline=None, authorized=lambda: True):
        self.connect, self.enqueue, self.drain = connect, enqueue, drain
        self.clock, self.monotonic, self.authorized = clock, monotonic, authorized
        self.stop = threading.Event()
        self.wait = wait or self.stop.wait
        self.started, self.deadline = started, deadline
        self.connections = self.messages = self.unique_events = 0
        self.possible_gap = False
        self.outcome = 'NOT_STARTED'
        self._seen = set()

    def remaining(self):
        return max(0, min(self.deadline - self.clock(), self._mono_deadline - self.monotonic()))

    def active(self):
        return not self.stop.is_set() and self.remaining() > 0 and self.authorized()

    def run(self):
        self.started = self.clock() if self.started is None else self.started
        self.deadline = self.started + 300 if self.deadline is None else self.deadline
        if not 0 < self.deadline - self.started <= 300:
            raise Refused('SOURCE_DEADLINE_REFUSED')
        self._mono_deadline = self.monotonic() + min(300, max(0, self.deadline - self.clock()))
        self.outcome = 'MONITORING'
        try:
            while self.active():
                if self.connections >= 3:
                    self.outcome = 'SOURCE_CONNECTION_LIMIT'
                    break
                self.connections += 1
                disconnected = False
                try:
                    socket = self.connect(self.remaining())
                except (ConnectionError, TimeoutError):
                    disconnected = True
                else:
                    try:
                        hello = False
                        hello_deadline = self.monotonic() + min(5, self.remaining())
                        while self.active():
                            if self.messages >= 1000:
                                raise Refused('SOURCE_MESSAGE_LIMIT')
                            if not hello and self.monotonic() >= hello_deadline:
                                raise Refused('SOURCE_AUTH_TIMEOUT')
                            try:
                                raw = socket.recv(timeout=min(0.25, self.remaining()))
                            except TimeoutError:
                                self.drain()
                                continue
                            except ConnectionError:
                                disconnected = True
                                break
                            if not self.active():
                                break
                            self.messages += 1
                            if type(raw) is not str:
                                raise Refused('SOURCE_ENVELOPE_REFUSED')
                            event = strict_json(raw.encode('utf-8'))
                            if event.get('event') == 'hello':
                                hello = True
                                del raw, event
                                continue
                            if not hello:
                                raise Refused('SOURCE_AUTH_REQUIRED')
                            item = posted_metadata(event, int(self.started * 1000))
                            if item and item['post_id'] not in self._seen and self.unique_events < 5:
                                self.enqueue(item)
                                self._seen.add(item['post_id'])
                                self.unique_events += 1
                            del raw, event, item
                            self.drain()
                    finally:
                        socket.close()
                if disconnected:
                    self.possible_gap = True
                    if self.active() and self.connections < 3:
                        self.wait(min(2 ** (self.connections - 1), self.remaining()))
                    elif self.active():
                        self.outcome = 'SOURCE_CONNECTION_LIMIT'
                        break
            if self.outcome == 'MONITORING':
                self.outcome = ('STOPPED' if self.stop.is_set() else
                                'EXPIRED' if self.remaining() == 0 else 'SOURCE_ACCESS_REVOKED')
        except Exception:
            self.outcome = 'SOURCE_STOPPED_SAFELY'
        finally:
            self.stop.set()
