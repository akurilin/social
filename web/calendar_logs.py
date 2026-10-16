"""Bounded local diagnostics without calendar text or CLI transcripts."""

import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


LOG_NAME = "calendar-coverage.jsonl"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
PROGRESS_INTERVAL_SECONDS = 10
MAX_DIAGNOSTIC_LINE_BYTES = 64_000
MAX_CLIENT_EVENTS = 100
MAX_REQUEST_IDS = 5
MAX_ERROR_FRAMES = 8
MAX_CAUSE_DEPTH = 8
WEB_ROOT = Path(__file__).resolve().parent
CLIENT_EVENTS = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.updated", "item.completed", "error",
})
CLIENT_ITEMS = frozenset({
    "agent_message", "reasoning", "command_execution", "file_change",
    "mcp_tool_call", "web_search", "todo_list", "plan_update",
})
FAILURE_CODES = frozenset({
    "insufficient_quota", "rate_limit_exceeded", "invalid_api_key",
    "context_length_exceeded", "model_not_found", "server_error",
    "service_unavailable", "invalid_request_error", "permission_denied",
})
REQUEST_ID_PATTERN = re.compile(r"\breq_[A-Za-z0-9]{8,80}\b")
HTTP_STATUS_PATTERN = re.compile(r"(?:http(?:/\d(?:\.\d)?)?\s+|status(?: code)?[\s:=]+)([45]\d{2})\b", re.I)
LOG_LOCK = threading.Lock()
ACTIVE_LOG = ContextVar("calendar_log", default=None)
ERROR_SIGNALS = {
    "connection": ("reconnecting", "stream disconnected", "connection", "network"),
    "rate_limit": ("rate limit", "usage limit", "quota", "429"),
    "authentication": ("unauthorized", "sign in", "401", "authentication"),
    "model_access": ("model not found", "not supported", "does not exist"),
    "cli_arguments": ("unexpected argument", "unknown feature", "unrecognized"),
    "dns": ("name or service not known", "nodename nor servname", "dns", "failed to resolve"),
    "tls": ("certificate verify failed", "tls", "ssl error", "invalid certificate"),
    "proxy": ("proxy error", "proxy connection", "proxy authentication"),
    "upstream_timeout": ("request timed out", "upstream timeout", "gateway timeout"),
    "retry": ("reconnecting", "retrying", "retry attempt"),
}


def log_path(cache_path):
    return cache_path / "logs" / LOG_NAME


def diagnostic_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def error_details(error):
    # Keep causal types and numeric OS codes, never exception messages or paths.
    chain = []
    cause = error
    seen = set()
    while cause is not None and id(cause) not in seen and len(chain) < MAX_CAUSE_DEPTH:
        seen.add(id(cause))
        chain.append(type(cause).__name__)
        next_cause = cause.__cause__ or cause.__context__
        if next_cause is None:
            break
        cause = next_cause
    stderr = diagnostic_text(getattr(cause, "stderr", None))
    stdout = diagnostic_text(getattr(cause, "stdout", None) or getattr(cause, "output", None))
    output = CommandOutput(emit_events=False)
    output.observe(stdout, stderr, final=True)
    # Source locations locate local bugs without a traceback's source text or locals.
    frames = []
    trace = cause.__traceback__
    while trace is not None:
        filename = Path(trace.tb_frame.f_code.co_filename)
        if filename.is_relative_to(WEB_ROOT):
            frames.append({"file": filename.name, "function": trace.tb_frame.f_code.co_name,
                           "line": trace.tb_lineno})
        trace = trace.tb_next
    return dict(
        error_type=type(cause).__name__, cause_types=chain,
        error_frames=frames[-MAX_ERROR_FRAMES:],
        exit_code=getattr(cause, "returncode", None),
        timeout_seconds=getattr(cause, "timeout", None),
        errno=getattr(cause, "errno", None),
        stdout_characters=len(stdout), stderr_characters=len(stderr),
        **output.failure_details(),
    )


class CommandOutput:
    """Extract bounded diagnostic facts while discarding all client text."""

    def __init__(self, *, emit_events=True):
        self.emit_events = emit_events
        self.command_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.next_progress = self.started + PROGRESS_INTERVAL_SECONDS
        self.sizes = {"stdout": 0, "stderr": 0}
        self.pending = {"stdout": b"", "stderr": b""}
        self.discarding = {"stdout": False, "stderr": False}
        self.first_output = None
        self.last_output = None
        self.last_event = None
        self.event_count = 0
        self.skipped_lines = 0
        self.retry_messages = 0
        self.signals = set()
        self.codes = set()
        self.statuses = set()
        self.request_ids = set()
        self.error_fingerprint = None

    def scan_error(self, text):
        # Store only fixed labels, status codes and opaque provider request IDs.
        lowered = text.lower()
        matched = {name for name, terms in ERROR_SIGNALS.items()
                   if any(term in lowered for term in terms)}
        self.signals.update(matched)
        if "retry" in matched:
            self.retry_messages += 1
        self.codes.update(code for code in FAILURE_CODES if code in lowered)
        self.statuses.update(int(code) for code in HTTP_STATUS_PATTERN.findall(text))
        for request_id in REQUEST_ID_PATTERN.findall(text):
            if len(self.request_ids) < MAX_REQUEST_IDS:
                self.request_ids.add(request_id)
        if matched or self.codes or self.statuses:
            self.error_fingerprint = hashlib.sha256(text.encode()).hexdigest()

    def line(self, line, stream):
        text = line.decode("utf-8", errors="replace")
        if stream == "stderr":
            self.scan_error(text)
            return
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            self.scan_error(text)
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if isinstance(kind, str) and kind in CLIENT_EVENTS:
            self.last_event = kind
            self.event_count += 1
            item = payload.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            fields = {"client_event": kind, "command_id": self.command_id}
            if isinstance(item_type, str) and item_type in CLIENT_ITEMS:
                fields["item_type"] = item_type
            usage = payload.get("usage")
            if isinstance(usage, dict):
                fields["usage"] = {key: usage[key] for key in (
                    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
                    if type(usage.get(key)) is int and usage[key] >= 0}
            if self.emit_events and self.event_count <= MAX_CLIENT_EVENTS:
                record("command.client_event", **fields)
        if kind == "error" or kind == "turn.failed" or payload.get("is_error") is True:
            # Error bodies may contain prompts and tokens; reduce them to safe facts.
            self.scan_error(text)
            self.error_fingerprint = hashlib.sha256(text.encode()).hexdigest()

    def observe(self, stdout=None, stderr=None, *, final=False):
        now = time.monotonic()
        for stream, value in (("stdout", stdout), ("stderr", stderr)):
            data = value if isinstance(value, bytes) else diagnostic_text(value).encode()
            delta = data[self.sizes[stream]:]
            if delta:
                self.first_output = self.first_output if self.first_output is not None else now
                self.last_output = now
                self.sizes[stream] = len(data)
                chunks = (self.pending[stream] + delta).split(b"\n")
                self.pending[stream] = chunks.pop()
                for line in chunks:
                    if not self.discarding[stream] and len(line) <= MAX_DIAGNOSTIC_LINE_BYTES:
                        self.line(line, stream)
                    else:
                        self.skipped_lines += 1
                    self.discarding[stream] = False
                if len(self.pending[stream]) > MAX_DIAGNOSTIC_LINE_BYTES:
                    self.pending[stream] = b""
                    self.discarding[stream] = True
            if final and self.pending[stream]:
                if not self.discarding[stream]:
                    self.line(self.pending[stream], stream)
                self.pending[stream] = b""
        if self.emit_events and not final and now >= self.next_progress:
            record("command.progress", **self.details())
            self.next_progress = now + PROGRESS_INTERVAL_SECONDS

    def failure_details(self):
        return dict(signals=sorted(self.signals), failure_codes=sorted(self.codes),
                    http_statuses=sorted(self.statuses), request_ids=sorted(self.request_ids),
                    error_fingerprint=self.error_fingerprint)

    def details(self):
        now = time.monotonic()
        return dict(
            command_id=self.command_id,
            stdout_bytes=self.sizes["stdout"], stderr_bytes=self.sizes["stderr"],
            first_output_seconds=(round(self.first_output - self.started, 3)
                                  if self.first_output is not None else None),
            output_idle_seconds=(round(now - self.last_output, 3)
                                 if self.last_output is not None else None),
            client_events=self.event_count, last_client_event=self.last_event,
            skipped_lines=self.skipped_lines, retry_messages=self.retry_messages,
            client_event_limit_reached=self.event_count > MAX_CLIENT_EVENTS,
            output_state="silent" if self.first_output is None else "received",
            **self.failure_details(),
        )


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
        record(name + ".failed", **(fields | error_details(error) | {
            "duration_seconds": round(time.monotonic() - started, 3)}))
        raise
    else:
        record(name + ".completed", **(fields | {
            "duration_seconds": round(time.monotonic() - started, 3)}))


def read_logs(cache_path):
    path = log_path(cache_path)
    with LOG_LOCK:
        paths = [path.with_name(f"{LOG_NAME}.{index}")
                 for index in range(LOG_BACKUPS, 0, -1)] + [path]
        return "".join(item.read_text() for item in paths if item.exists()) or "No calendar checks logged yet.\n"
