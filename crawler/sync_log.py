"""Bounded sync timings, separate from event data and crawl evidence."""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import datetime as dt
from functools import wraps
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid


APP_NAME = 'SocialCrawler'
LOG_DIRECTORY_ENV = 'SOCIAL_LOG_DIR'
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 3
SECONDS_PRECISION = 6
ACTIVE = ContextVar('sync_timing', default=None)
LOCK = threading.Lock()


def log_directory():
    override = os.environ.get(LOG_DIRECTORY_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Logs' / APP_NAME
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state')) / APP_NAME / 'logs'


def log_path(run_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
        raise ValueError('Invalid run ID for timing log')
    return log_directory() / ('sync-' + run_id + '.jsonl')


def record(event, *, run_id=None, source_id=None, **fields):
    active = ACTIVE.get() or {}
    run_id = run_id or active.get('run_id')
    if not run_id:
        return
    row = dict(timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
               event=event, run_id=run_id, source_id=source_id or active.get('source_id'),
               attempt_id=active.get('attempt_id'), **fields)
    path = log_path(run_id)
    try:
        # One append per line keeps parallel source records readable.
        with LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= LOG_MAX_BYTES:
                for index in range(LOG_BACKUPS, 0, -1):
                    old = path if index == 1 else path.with_name(path.name + '.' + str(index - 1))
                    if old.exists():
                        old.replace(path.with_name(path.name + '.' + str(index)))
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as stream:
                stream.write(json.dumps(row, separators=(',', ':')) + '\n')
    except OSError:
        logging.getLogger(__name__).warning('Could not write sync timing log: %s', path)


@contextmanager
def span(name, *, run_id=None, source_id=None, **fields):
    started = time.perf_counter()
    parent = ACTIVE.get() or {}
    context = dict(parent)
    if run_id:
        context['run_id'] = run_id
    if source_id:
        context['source_id'] = source_id
    if name == 'source':
        context.update(attempt_id=uuid.uuid4().hex, metrics=Counter())
    token = ACTIVE.set(context)
    record(name + '.started', **fields)
    result = dict(fields)
    try:
        yield result
    except BaseException as error:
        result['error_type'] = type(error).__name__
        if name == 'source':
            result.update({key: round(value, SECONDS_PRECISION)
                           for key, value in context['metrics'].items()})
        record(name + '.failed', duration_seconds=round(time.perf_counter() - started, SECONDS_PRECISION), **result)
        raise
    else:
        if name == 'source':
            result.update({key: round(value, SECONDS_PRECISION)
                           for key, value in context['metrics'].items()})
        record(name + '.completed', duration_seconds=round(time.perf_counter() - started, SECONDS_PRECISION), **result)
    finally:
        ACTIVE.reset(token)


def request_timing(kind):
    """Count requests and time transport without logging URLs or request bodies."""
    def decorate(fn):
        @wraps(fn)
        def measured(self, url, *args, **kwargs):
            active = ACTIVE.get()
            if not active:
                return fn(self, url, *args, **kwargs)
            started = time.perf_counter()
            fields = {'host': urlsplit(url).hostname, 'method': kwargs.get('method', 'GET')}
            try:
                with span(kind, **fields) as result:
                    response = fn(self, url, *args, **kwargs)
                    status = getattr(response, 'status', None)
                    if status is not None:
                        result['status'] = status
                    return response
            finally:
                metrics = active.get('metrics')
                if metrics is not None:
                    metrics[kind + '_count'] += 1
                    metrics[kind + '_seconds'] += time.perf_counter() - started
        return measured
    return decorate


def wait(seconds, kind):
    # Separate deliberate waits from network and parser work.
    started = time.perf_counter()
    time.sleep(seconds)
    active = ACTIVE.get() or {}
    metrics = active.get('metrics')
    if metrics is not None:
        metrics[kind + '_seconds'] += time.perf_counter() - started
        metrics[kind + '_count'] += 1


def read_records(run_id):
    path = log_path(run_id)
    paths = [path.with_name(path.name + '.' + str(index))
             for index in range(LOG_BACKUPS, 0, -1)] + [path]
    records = []
    for item in paths:
        if item.exists():
            for line in item.read_text().splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # A live reader can catch an unfinished append.
    return records
