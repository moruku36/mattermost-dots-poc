"""Bounded approved-channel readpoll adapter. No backend invocation on import."""
import json
import re
import sqlite3
import threading
import time

from notification_core import CHANNEL, Refused


def metadata(result, floor_ms):
    if type(result) is not dict or result.get('isError') is not False:
        raise Refused('SOURCE_READ_FAILED')
    content = result.get('content')
    if type(content) is not list or len(content) != 1 or content[0].get('type') != 'text':
        raise Refused('SOURCE_ENVELOPE_REFUSED')
    raw = content[0].get('text')
    if type(raw) is not str or len(raw.encode()) > 65536:
        raise Refused('SOURCE_BOUND_REFUSED')
    try:
        value = json.loads(raw)
    except ValueError:
        raise Refused('SOURCE_JSON_REFUSED') from None
    posts = value.get('posts')
    if (value.get('bounded') is not True or value.get('complete_history') is not False
            or type(posts) is not list or len(posts) > 5):
        raise Refused('SOURCE_BOUND_REFUSED')
    output = []
    for post in posts:
        if (type(post) is not dict or post.get('channel_id') != CHANNEL or type(post.get('create_at')) is not int
                or type(post.get('id')) is not str or not re.fullmatch(r'[a-z0-9]{26}', post['id'])):
            raise Refused('SOURCE_SCOPE_REFUSED')
        if post['create_at'] >= floor_ms:
            output.append({'channel_id': CHANNEL, 'post_id': post['id'], 'created_ms': post['create_at']})
    # At a full source page we cannot claim continuity from this small snapshot.
    # Do not fetch more history without permission; report a possible gap and stop.
    if len(posts) == 5 and len(output) == 5:
        raise Refused('SOURCE_POSSIBLE_GAP')
    return output


class Poller:
    def __init__(self, read, enqueue, drain, *, clock=time.time, wait=None, state_path=None,
                 started=None, deadline=None):
        self.read, self.enqueue, self.drain, self.clock = read, enqueue, drain, clock
        self.stop = threading.Event()
        self.wait = wait or self.stop.wait
        self.reads, self.started, self.deadline = 0, started, deadline
        self.state_path = state_path
        self.outcome = 'NOT_STARTED'

    def run(self):
        # Called only after successful formal subscription and full preparation.
        if self.started is None:
            self.started = self.clock()
        if self.deadline is None:
            self.deadline = self.started + 300
        if not 0 < self.deadline - self.started <= 300:
            raise Refused('SOURCE_DEADLINE_REFUSED')
        floor = int(self.started * 1000)
        budget = None
        if self.state_path:
            budget = sqlite3.connect(self.state_path)
            budget.execute('PRAGMA synchronous=FULL')
            budget.execute('CREATE TABLE IF NOT EXISTS budget(start REAL,deadline REAL,reads INTEGER,next_at REAL)')
            existing = budget.execute('SELECT start,deadline,reads,next_at FROM budget').fetchone()
            if existing and existing[:2] != (self.started, self.deadline):
                budget.close()
                raise Refused('SOURCE_RESTART_SCOPE_REFUSED')
            if not existing:
                budget.execute('INSERT INTO budget VALUES (?,?,0,?)', (self.started, self.deadline, self.started))
                budget.commit()
            self.reads = budget.execute('SELECT reads FROM budget').fetchone()[0]
        self.outcome = 'MONITORING'
        try:
            while not self.stop.is_set() and self.clock() < self.deadline and self.reads < 60:
                if budget:
                    budget.execute('BEGIN IMMEDIATE')
                    reads, next_at = budget.execute('SELECT reads,next_at FROM budget').fetchone()
                    self.reads = reads
                    if reads >= 60:
                        budget.rollback()
                        break
                    if self.clock() < next_at:
                        budget.rollback()
                        self.wait(min(next_at - self.clock(), self.deadline - self.clock()))
                        continue
                    budget.execute('UPDATE budget SET reads=reads+1,next_at=?', (self.clock() + 5,))
                    budget.commit()
                self.reads += 1  # failed reads consume budget as well
                result = self.read({'channel_id': CHANNEL, 'limit': 5}, self.deadline)
                for item in metadata(result, floor):
                    self.enqueue(item)
                self.drain()
                self.wait(min(5, max(0, self.deadline - self.clock())))
            self.outcome = 'STOPPED' if self.stop.is_set() else 'EXPIRED'
        except Refused as exc:
            self.outcome = 'SOURCE_POSSIBLE_GAP' if str(exc) == 'SOURCE_POSSIBLE_GAP' else 'SOURCE_STOPPED_SAFELY'
        except Exception:
            self.outcome = 'SOURCE_STOPPED_SAFELY'
        finally:
            self.stop.set()
            if budget:
                budget.close()
