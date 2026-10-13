"""Bounded local diagnostics without calendar text or CLI transcripts."""

import datetime as dt
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar


LOG_NAME = "calendar-coverage.jsonl"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
LOG_LOCK = threading.Lock()
ACTIVE_LOG = ContextVar("calendar_log", default=None)
ERROR_SIGNALS = {
    "connection": ("reconnecting", "stream disconnected", "connection", "network"),
    "rate_limit": ("rate limit", "usage limit", "quota", "429"),
    "authentication": ("unauthorized", "sign in", "401", "authentication"),
    "model_access": ("model not found", "not supported", "does not exist"),
    "cli_arguments": ("unexpected argument", "unknown feature", "unrecognized"),
}


def log_path(cache_path):
    return cache_path / "logs" / LOG_NAME


def error_details(error):
    # Inspect known signals only; exception text can contain the input prompt.
    cause = error.__cause__ or error
    stderr = getattr(cause, "stderr", None) or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    lowered = stderr.lower()
    return {
        "error_type": type(cause).__name__,
        "exit_code": getattr(cause, "returncode", None),
        "timeout_seconds": getattr(cause, "timeout", None),
        "stderr_characters": len(stderr),
        "signals": [name for name, terms in ERROR_SIGNALS.items()
                    if any(term in lowered for term in terms)],
    }


def record(event, **fields):
    active = ACTIVE_LOG.get()
    if active is None:
        return
    cache_path, run_id, started = active
    row = dict(timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
               run_id=run_id, elapsed_seconds=round(time.monotonic() - started, 3),
               event=event, **fields)
    path = log_path(cache_path)
    try:
        # Serialize rotation and writes so readers see complete JSON lines.
        with LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= LOG_MAX_BYTES:
                for index in range(LOG_BACKUPS, 0, -1):
                    source = path if index == 1 else path.with_name(f"{LOG_NAME}.{index - 1}")
                    if source.exists():
                        source.replace(path.with_name(f"{LOG_NAME}.{index}"))
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as stream:
                stream.write(json.dumps(row) + "\n")
    except OSError:
        # A disk failure must not stop a calendar check or expose its contents.
        logging.getLogger(__name__).warning("Calendar diagnostic log could not be written.")


@contextmanager
def refresh_log(cache_path):
    token = ACTIVE_LOG.set((cache_path, uuid.uuid4().hex, time.monotonic()))
    try:
        record("refresh.started")
        yield
    finally:
        ACTIVE_LOG.reset(token)


@contextmanager
def stage(name, **fields):
    started = time.monotonic()
    record(name + ".started", **fields)
    try:
        yield
    except Exception as error:
        record(name + ".failed", duration_seconds=round(time.monotonic() - started, 3),
               **error_details(error))
        raise
    else:
        record(name + ".completed", duration_seconds=round(time.monotonic() - started, 3))


def read_logs(cache_path):
    path = log_path(cache_path)
    with LOG_LOCK:
        paths = [path.with_name(f"{LOG_NAME}.{index}")
                 for index in range(LOG_BACKUPS, 0, -1)] + [path]
        return "".join(item.read_text() for item in paths if item.exists()) or "No calendar checks logged yet.\n"
