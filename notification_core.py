"""Public metadata-only protocol support; no message-body processing."""
from poc_config import CHANNEL
from dataclasses import dataclass

import hashlib

import hmac

import json

from pathlib import Path

import re

import sqlite3

import threading

import time

MAX_WIRE = 65536

class Refused(Exception):
    pass

def strict_json(raw):
    if type(raw) is not bytes or len(raw) > MAX_WIRE:
        raise Refused('WIRE_REFUSED')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refused('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(Refused('JSON_REFUSED')))
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('JSON_REFUSED') from None
    if type(result) is not dict:
        raise Refused('JSON_REFUSED')
    return result
