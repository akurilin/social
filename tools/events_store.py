#!/usr/bin/env python3
"""
events_store.py — SQLite-backed event store and crawl ledger.

Dependency-free (stdlib only). Python 3.8+.

Static source configuration lives in sources.json. Dynamic state lives in
social.db, using five tables only:

  runs          one active or completed crawl execution
  source_runs   one source planned or attempted during one run
  discoveries   one parsed or rejected event candidate from a source run
  events        canonical deduplicated event instances and their assessment
  event_urls    event-specific URL aliases for canonical event instances

Large raw responses and screenshots stay in .cache; source_runs stores paths
to those artifacts. Never hand-edit social.db — use this tool.

COMMANDS
  plan-run     [--date YYYY-MM-DD] [--mail-available] [--only-source ID]
               Create a database-backed run for all enabled sources, or one
               targeted source.

  run-work     --run-id ID
               Print the pending retrieval instructions for a stored run.

  record-source --run-id ID --source-id ID [--input FILE|-]
               Read one source result from stdin by default and stage it in
               the crawl ledger without merging events.

  finalize-run --run-id ID [--dry-run]
               Validate a stored run, deduplicate staged candidates, update
               events, and mark the run complete.

  migrate      --events-json FILE [--discoveries FILE]
               One-time import from the legacy JSON store.

  merge        --input FILE [--seen YYYY-MM-DD] [--run-id ID] [--dry-run]
               Backward-compatible import for an older crawl JSON file or a
               legacy event array.

  needs-rank  [--days N] [--all] [--host X] [--grouped] [--json]
  rank        --set ID=high [--set ID=low ...] | --file FILE
  upcoming    [--days 10] [--min-rank medium] [--source-id X] [--json]
  catalog-check
  source-stats [--json]
  stats

EXIT CODES: 0 ok, 1 usage/IO error.
"""

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.environ.get("SOCIAL_DB", os.path.join(ROOT, "social.db"))
SOURCES = os.path.join(ROOT, "sources.json")

RANKS = ("high", "medium", "low")
RANK_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}
SOURCE_STATES = {
    "ok", "ok_via_fallback", "empty_verified", "empty_suspicious",
    "parse_failed", "validation_failed", "fetch_failed", "auth_required",
    "blocked",
}
LEGACY_SOURCE_STATES = SOURCE_STATES | {"not_due"}
RETRY_STATES = {
    "empty_suspicious", "parse_failed", "validation_failed", "fetch_failed",
}
SCHEMA_VERSION = 5

VOLATILE_FIELDS = (
    "price", "is_free", "capacity_flag", "venue", "neighborhood",
    "address", "end", "status", "signup_url", "explicit_age_min",
    "explicit_age_max",
)

SAME_SOURCE_CLEARABLE_FIELDS = (
    "is_free", "capacity_flag", "explicit_age_min", "explicit_age_max",
)

EVENT_COLUMNS = (
    "id", "dedup_key", "url", "title", "host", "start", "end", "venue",
    "neighborhood", "address", "signup_url", "price", "is_free",
    "explicit_age_min", "explicit_age_max", "source_id", "description",
    "fit_note", "rank", "format_tags_json", "capacity_flag", "catch",
    "status", "first_seen", "last_seen", "ranked_on",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    seen_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL DEFAULT 'running',
    catalog_hash TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'unknown',
    recipe_version TEXT,
    audit INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    found_count INTEGER NOT NULL DEFAULT 0,
    parsed_count INTEGER NOT NULL DEFAULT 0,
    qualified_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    rejection_counts_json TEXT NOT NULL DEFAULT '{}',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    work_json TEXT NOT NULL DEFAULT '{}',
    detail TEXT,
    error TEXT,
    PRIMARY KEY (run_id, source_id)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    start TEXT NOT NULL,
    end TEXT,
    venue TEXT NOT NULL DEFAULT '',
    neighborhood TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    signup_url TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL DEFAULT '',
    is_free INTEGER NOT NULL DEFAULT 0,
    explicit_age_min INTEGER,
    explicit_age_max INTEGER,
    source_id TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    fit_note TEXT NOT NULL DEFAULT '',
    rank TEXT CHECK (rank IN ('high', 'medium', 'low') OR rank IS NULL),
    format_tags_json TEXT NOT NULL DEFAULT '[]',
    capacity_flag TEXT,
    catch TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    ranked_on TEXT
);

CREATE TABLE IF NOT EXISTS event_urls (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    canonical_url TEXT NOT NULL,
    url TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (event_id, canonical_url)
);

CREATE TABLE IF NOT EXISTS discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    source_event_key TEXT,
    raw_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    rejection_reason TEXT,
    event_id TEXT REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_id, run_id);
CREATE INDEX IF NOT EXISTS idx_discoveries_run_source ON discoveries(run_id, source_id);
CREATE INDEX IF NOT EXISTS idx_discoveries_event ON discoveries(event_id);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
CREATE INDEX IF NOT EXISTS idx_event_urls_canonical ON event_urls(canonical_url);

CREATE VIEW IF NOT EXISTS latest_source_health AS
SELECT sr.*, r.seen_date, r.started_at AS run_started_at,
       (SELECT MAX(a.finished_at)
        FROM source_runs a
        WHERE a.source_id = sr.source_id AND a.audit = 1
          AND a.state IN ('ok', 'ok_via_fallback', 'empty_verified'))
       AS last_audited_at
FROM source_runs sr
JOIN runs r ON r.id = sr.run_id
WHERE NOT EXISTS (
    SELECT 1
    FROM source_runs newer
    JOIN runs nr ON nr.id = newer.run_id
    WHERE newer.source_id = sr.source_id
      AND nr.state = 'completed'
      AND (nr.started_at > r.started_at
           OR (nr.started_at = r.started_at AND newer.run_id > sr.run_id))
) AND r.state = 'completed';
"""


# ---------------------------------------------------------------- database

def connect(path=DB):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    # Procedural retrieval can run in parallel, but staging and finalization use
    # one writer. Rollback journaling also avoids WAL shared-memory files, which
    # can be stranded as .fuse_hidden files on FUSE mounts.
    con.execute("PRAGMA journal_mode = DELETE")
    con.executescript(SCHEMA)
    migrate_schema(con)
    con.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))
    return con


def column_names(con, table):
    return {row[1] for row in con.execute("PRAGMA table_info({})".format(table))}


def migrate_schema(con):
    """Keep crawl data while moving all active run state into SQLite."""
    source_run_columns = column_names(con, "source_runs")
    event_columns = column_names(con, "events")
    with con:
        if "work_json" not in source_run_columns:
            con.execute(
                "ALTER TABLE source_runs ADD COLUMN work_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "signup_url" not in event_columns:
            con.execute(
                "ALTER TABLE events ADD COLUMN signup_url TEXT NOT NULL DEFAULT ''"
            )
        if "explicit_age_min" not in event_columns:
            con.execute("ALTER TABLE events ADD COLUMN explicit_age_min INTEGER")
        if "explicit_age_max" not in event_columns:
            con.execute("ALTER TABLE events ADD COLUMN explicit_age_max INTEGER")
        for row in con.execute(
                "SELECT id, url, source_id, first_seen, last_seen FROM events"):
            upsert_event_alias(
                con, row["id"], row["url"], row["source_id"],
                row["first_seen"], row["last_seen"],
            )
        view = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' "
            "AND name='latest_source_health'"
        ).fetchone()
        if view and "r.state = 'completed'" not in (view[0] or ""):
            con.execute("DROP VIEW latest_source_health")
            con.executescript(SCHEMA[SCHEMA.index("CREATE VIEW IF NOT EXISTS"):])


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today():
    return dt.date.today()


def catalog_hash():
    if not os.path.exists(SOURCES):
        return None
    with open(SOURCES, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


def load_catalog():
    with open(SOURCES, "r", encoding="utf-8") as fh:
        return json.load(fh)


def plan_source(source, profiles, latest):
    profile_name = source.get("retrieval_profile")
    profile = profiles[profile_name]
    previous = latest.get(source["id"])

    if previous is None:
        reason = "never checked"
        audit = True
    elif previous.get("state") in RETRY_STATES:
        reason = "retry after {}".format(previous["state"])
        audit = True
    else:
        reason = "selected for this run"
        audit = False

    primary = profile["primary"]
    return {
        "source_id": source["id"], "state": "pending",
        "method": primary["method"],
        "recipe_version": primary["recipe"],
        "audit": audit,
        "detail": reason,
    }


def build_run_plan(catalog, latest_rows, seen, mail_available=False, run_id=None,
                   only_source=None):
    """Build a run plan for all enabled sources or one targeted source."""
    profiles = catalog.get("retrieval_profiles") or {}
    latest = {row["source_id"]: row for row in latest_rows}
    catalog_sources = catalog.get("sources") or []
    inbox = catalog.get("inbox_sources") or {}
    inbox_items = inbox.get("items") or []
    catalog_ids = {source["id"] for source in catalog_sources}
    inbox_ids = {source["id"] for source in inbox_items}
    disabled_ids = {
        source["id"] for source in catalog_sources
        if source.get("enabled", True) is False
    }
    disabled_ids.update(
        source["id"] for source in inbox_items
        if source.get("enabled", True) is False
    )
    if only_source and only_source not in catalog_ids | inbox_ids:
        raise ValueError("unknown --only-source id: {}".format(only_source))
    if only_source in disabled_ids:
        raise ValueError("disabled --only-source id: {}".format(only_source))
    if only_source:
        catalog_sources = [
            source for source in catalog_sources if source["id"] == only_source
        ]
        inbox_items = [
            source for source in inbox_items if source["id"] == only_source
        ]
    rows = []
    work = []

    for source in catalog_sources:
        if source.get("enabled", True) is False:
            continue
        row = plan_source(source, profiles, latest)
        if source["id"] == only_source:
            row["detail"] = "targeted source crawl"
        rows.append(row)
        if row["state"] == "pending":
            work.append({
                "source_id": source["id"],
                "title": source.get("title"),
                "url": source.get("url"),
                "geo": source.get("geo"),
                "parse_hint": source.get("parse_hint"),
                "retrieval_profile": source.get("retrieval_profile"),
                "adapter": source.get("adapter"),
                "primary": profiles[source["retrieval_profile"]]["primary"],
                "fallbacks": profiles[source["retrieval_profile"]].get("fallbacks", []),
                "required_fields": profiles[source["retrieval_profile"]]["required_fields"],
                "empty_signal": profiles[source["retrieval_profile"]]["empty_signal"],
                "audit": row["audit"],
            })

    for source in inbox_items:
        if source.get("enabled", True) is False:
            continue
        if mail_available:
            row = {
                "source_id": source["id"], "state": "pending",
                "method": "gmail", "audit": False,
                "recipe_version": inbox.get("retrieval_profile"),
                "detail": ("targeted source crawl" if source["id"] == only_source
                           else "selected for this run"),
            }
            work.append({
                "source_id": source["id"], "title": source.get("title"),
                "query": inbox.get("query_template", "").format(
                    senders=source.get("sender_hint", "")),
                "retrieval_profile": inbox.get("retrieval_profile"),
                "required_fields": profiles[inbox["retrieval_profile"]]["required_fields"],
                "empty_signal": profiles[inbox["retrieval_profile"]]["empty_signal"],
                "audit": False,
            })
        else:
            row = {
                "source_id": source["id"], "state": "blocked",
                "method": "not_run", "audit": False,
                "detail": "mail connector unavailable",
            }
        rows.append(row)

    ids = [row["source_id"] for row in rows]
    if len(ids) != len(set(ids)):
        duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
        raise ValueError("duplicate planned source ids: {}".format(", ".join(duplicates)))

    run_id = run_id or make_run_id(seen.isoformat())
    return {
        "run": {
            "id": run_id,
            "seen_date": seen.isoformat(),
            "started_at": now_iso(),
            "catalog_hash": catalog_hash(),
            "manifest_source_ids": ids,
        },
        "work": work,
        "sources": rows,
        "events": [],
        "rejections": [],
    }


def make_run_id(seen):
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    return "{}-{}-{}".format(seen, stamp, uuid.uuid4().hex[:6])


def ensure_run(con, run_id, seen, meta=None):
    meta = meta or {}
    con.execute(
        """INSERT INTO runs
           (id, seen_date, started_at, state, catalog_hash)
           VALUES (?, ?, ?, 'running', ?)
           ON CONFLICT(id) DO NOTHING""",
        (run_id, seen, meta.get("started_at") or now_iso(),
         meta.get("catalog_hash") or catalog_hash()),
    )


# ---------------------------------------------------------------- utilities

def norm_text(s):
    s = unicodedata.normalize("NFKD", s or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def series_key(ev):
    """Conservative recurring-series key used only to compact ranking queues."""
    title = norm_text(ev.get("title"))
    title = re.sub(
        r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b|"
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
        r"(?:uary|ch|il|e|y|ust|tember|ober|ember)?\b",
        " ", title,
    )
    title = re.sub(r"\b20\d{2}\b|\b\d+(?:st|nd|rd|th)?\b", " ", title)
    title = re.sub(r"\bpart\s+(?:[ivxlcdm]+|\d+)\b", " ", title)
    title = re.sub(r"\s+", " ", title).strip() or norm_text(ev.get("title"))
    return "|".join((norm_text(ev.get("source_id")),
                     norm_text(ev.get("host")), title))


def grouped_rank_queue(events):
    groups = {}
    for ev in events:
        groups.setdefault(series_key(ev), []).append(ev)
    out = []
    occurrence_fields = (
        "id", "title", "start", "end", "venue", "neighborhood", "address",
        "price", "is_free", "url", "capacity_flag", "catch",
    )
    for key, members in groups.items():
        members.sort(key=lambda ev: ev.get("start") or "")
        out.append({
            "series_key": key,
            "representative": members[0],
            "occurrences": [
                {name: ev.get(name) for name in occurrence_fields if name in ev}
                for ev in members
            ],
        })
    out.sort(key=lambda group: group["representative"].get("start") or "")
    return out


def canon_url(u):
    """Normalize a URL while keeping query values that identify an event."""
    if not u:
        return ""
    value = u.strip()
    parsed = urlsplit(value if "://" in value else "//" + value)
    host = parsed.netloc.lower()
    host = re.sub(r"^www\.", "", host)
    if host == "lu.ma":
        host = "luma.com"
    path = parsed.path.rstrip("/")
    tracking_keys = {
        "_ga", "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid",
    }
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in tracking_keys:
            continue
        query.append((lowered, item))
    query.sort()
    canonical = host + path
    if query:
        canonical += "?" + urlencode(query)
    return canonical.lower()


def parse_dt(s):
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def bucket15(d):
    return d.replace(minute=(d.minute // 15) * 15, second=0, microsecond=0)


GENERIC_SEGMENTS = {
    "", "calendar", "calendars", "events", "event", "upcoming", "index",
    "home", "schedule", "programs", "program", "tastingevents", "film",
    "films", "whats-on", "listings", "shop", "tickets",
}


def is_event_url(cu):
    if not cu or "/" not in cu:
        return False
    path, _, query = cu.partition("?")
    if query:
        identity_keys = {
            "eid", "event", "event_id", "eventid", "id", "p", "post",
            "vista_film_id", "wfea_eb_id",
        }
        keys = {key for key, _ in parse_qsl(query, keep_blank_values=True)}
        if keys & identity_keys or any(key.endswith("_id") for key in keys):
            return True
    seg = path.rsplit("/", 1)[-1]
    seg = re.sub(r"\.(aspx|html?|php)$", "", seg)
    if seg in GENERIC_SEGMENTS:
        return False
    return len(seg) >= 6 or any(c.isdigit() for c in seg) or "-" in seg


def event_key(ev):
    """Stable dedup key: event URL+time, else place+time+title."""
    d = parse_dt(ev.get("start") or ev.get("start_datetime"))
    stamp = bucket15(d).isoformat() if d else "nodate"
    cu = canon_url(ev.get("url"))
    if is_event_url(cu):
        return "u:" + cu + "|" + stamp
    place = norm_text(ev.get("venue") or ev.get("venue_name")) \
        or norm_text(ev.get("neighborhood"))
    title = norm_text(ev.get("title"))[:40]
    return "v:" + place + "|" + stamp + "|" + title


def make_id(key):
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def start_date(ev):
    d = parse_dt(ev.get("start"))
    return d.date() if d else None


def normalise_incoming(raw, seen):
    def optional_int(value):
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    start = raw.get("start") or raw.get("start_datetime") or ""
    parsed = parse_dt(start)
    ev = {
        "id": None,
        "url": (raw.get("url") or "").strip(),
        "title": (raw.get("title") or "").strip(),
        "host": (raw.get("host") or "").strip(),
        "start": parsed.isoformat() if parsed else start,
        "end": raw.get("end") or raw.get("end_datetime") or None,
        "venue": (raw.get("venue") or raw.get("venue_name") or "").strip(),
        "neighborhood": (raw.get("neighborhood") or "").strip(),
        "address": (raw.get("address") or "").strip(),
        "signup_url": (raw.get("signup_url") or "").strip(),
        "price": str(raw.get("price") or "").strip(),
        "is_free": bool(raw.get("is_free")),
        "explicit_age_min": optional_int(raw.get("explicit_age_min")),
        "explicit_age_max": optional_int(raw.get("explicit_age_max")),
        "source_id": (raw.get("source_id") or "").strip(),
        "description": (raw.get("description")
                        or raw.get("format_note") or "").strip(),
        "fit_note": raw.get("fit_note") or "",
        "rank": raw.get("rank") if raw.get("rank") in RANKS else None,
        "format_tags": raw.get("format_tags") or [],
        "capacity_flag": (raw.get("capacity_flag").strip()
                          if isinstance(raw.get("capacity_flag"), str)
                          else raw.get("capacity_flag")),
        "catch": raw.get("catch") or "",
        "status": raw.get("status") or "active",
        "first_seen": raw.get("first_seen") or seen,
        "last_seen": raw.get("last_seen") or seen,
        "ranked_on": raw.get("ranked_on"),
    }
    key = raw.get("dedup_key") or event_key(ev)
    ev["dedup_key"] = key
    ev["id"] = raw.get("id") or make_id(key)
    return ev


def event_values(ev):
    return (
        ev["id"], ev.get("dedup_key") or event_key(ev), ev.get("url") or "",
        ev.get("title") or "", ev.get("host") or "", ev.get("start") or "",
        ev.get("end"), ev.get("venue") or "", ev.get("neighborhood") or "",
        ev.get("address") or "", ev.get("signup_url") or "",
        ev.get("price") or "", int(bool(ev.get("is_free"))),
        ev.get("explicit_age_min"), ev.get("explicit_age_max"),
        ev.get("source_id") or "", ev.get("description") or "",
        ev.get("fit_note") or "", ev.get("rank"),
        json.dumps(ev.get("format_tags") or [], ensure_ascii=False),
        ev.get("capacity_flag"), ev.get("catch") or "",
        ev.get("status") or "active", ev.get("first_seen") or today().isoformat(),
        ev.get("last_seen") or today().isoformat(), ev.get("ranked_on"),
    )


def row_to_event(row, include_dedup_key=False):
    ev = dict(row)
    ev["is_free"] = bool(ev["is_free"])
    ev["format_tags"] = json.loads(ev.pop("format_tags_json") or "[]")
    if ev.get("capacity_flag") is None:
        ev.pop("capacity_flag", None)
    if not include_dedup_key:
        ev.pop("dedup_key", None)
    return ev


def get_event(con, eid):
    row = con.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    return row_to_event(row, include_dedup_key=True) if row else None


def upsert_event_alias(con, event_id, url, source_id="", first_seen=None,
                       last_seen=None):
    """Store one event-specific URL alias. Return true for a new alias."""
    canonical = canon_url(url)
    if not is_event_url(canonical):
        return False
    first_seen = first_seen or today().isoformat()
    last_seen = last_seen or first_seen
    exists = con.execute(
        "SELECT 1 FROM event_urls WHERE event_id=? AND canonical_url=?",
        (event_id, canonical),
    ).fetchone()
    con.execute(
        """INSERT INTO event_urls
           (event_id, canonical_url, url, source_id, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id, canonical_url) DO UPDATE SET
             url=CASE WHEN excluded.url != '' THEN excluded.url ELSE event_urls.url END,
             source_id=CASE WHEN event_urls.source_id = '' THEN excluded.source_id
                            ELSE event_urls.source_id END,
             first_seen=MIN(event_urls.first_seen, excluded.first_seen),
             last_seen=MAX(event_urls.last_seen, excluded.last_seen)""",
        (event_id, canonical, url, source_id or "", first_seen, last_seen),
    )
    return exists is None


def get_event_urls(con, event_id):
    return [dict(row) for row in con.execute(
        """SELECT event_id, canonical_url, url, source_id, first_seen, last_seen
           FROM event_urls WHERE event_id=? ORDER BY first_seen, canonical_url""",
        (event_id,),
    ).fetchall()]


def delete_source_aliases_added_on(con, source_id, first_seen):
    """Delete generated URL aliases for one source and one exact crawl date."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id is required")
    try:
        parsed_date = dt.date.fromisoformat(first_seen)
    except (TypeError, ValueError) as error:
        raise ValueError("first_seen must be an ISO date") from error
    cursor = con.execute(
        "DELETE FROM event_urls WHERE source_id=? AND first_seen=?",
        (source_id.strip(), parsed_date.isoformat()),
    )
    return cursor.rowcount


def invalidate_source_events_by_id(con, source_id, event_ids, reason):
    """Reject history and delete an exact set of invalid generated events."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id is required")
    ids = list(dict.fromkeys(event_ids or []))
    if not ids or any(not isinstance(event_id, str) or not event_id.strip()
                      for event_id in ids):
        raise ValueError("event_ids must contain one or more event IDs")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")
    marks = ",".join("?" for _ in ids)
    rows = con.execute(
        "SELECT id FROM events WHERE source_id=? AND id IN ({})".format(marks),
        [source_id.strip()] + ids,
    ).fetchall()
    found = {row["id"] for row in rows}
    if found != set(ids):
        raise ValueError("one or more event IDs do not belong to source {}".format(
            source_id.strip()))
    run_rows = con.execute(
        "SELECT DISTINCT run_id FROM discoveries WHERE event_id IN ({})".format(marks),
        ids,
    ).fetchall()
    run_ids = [row["run_id"] for row in run_rows]
    con.execute(
        "UPDATE discoveries SET outcome='rejected', rejection_reason=?, event_id=NULL "
        "WHERE event_id IN ({})".format(marks),
        [reason.strip()] + ids,
    )
    cursor = con.execute(
        "DELETE FROM events WHERE source_id=? AND id IN ({})".format(marks),
        [source_id.strip()] + ids,
    )
    for run_id in run_ids:
        rows = con.execute(
            "SELECT outcome, rejection_reason FROM discoveries "
            "WHERE run_id=? AND source_id=?",
            (run_id, source_id.strip()),
        ).fetchall()
        outcomes = {}
        rejection_counts = {}
        for row in rows:
            outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
            if row["outcome"] == "rejected":
                key = row["rejection_reason"] or "rejected"
                rejection_counts[key] = rejection_counts.get(key, 0) + 1
        qualified = sum(outcomes.get(key, 0) for key in
                        ("new", "updated", "unchanged", "duplicate"))
        repaired = rejection_counts.get(reason.strip(), 0)
        con.execute(
            """UPDATE source_runs SET state='validation_failed',
               found_count=?, parsed_count=?, qualified_count=?, rejected_count=?,
               new_count=?, updated_count=?, unchanged_count=?, duplicate_count=?,
               rejection_counts_json=?, error=?
               WHERE run_id=? AND source_id=?""",
            (len(rows), qualified + repaired, qualified,
             outcomes.get("rejected", 0), outcomes.get("new", 0),
             outcomes.get("updated", 0), outcomes.get("unchanged", 0),
             outcomes.get("duplicate", 0),
             json.dumps(rejection_counts, ensure_ascii=False), reason.strip(),
             run_id, source_id.strip()),
        )
    return cursor.rowcount, run_ids


def upsert_event(con, ev):
    columns = ", ".join(EVENT_COLUMNS)
    marks = ", ".join("?" for _ in EVENT_COLUMNS)
    updates = ", ".join("{} = excluded.{}".format(c, c)
                        for c in EVENT_COLUMNS if c != "id")
    con.execute(
        "INSERT INTO events ({}) VALUES ({}) ON CONFLICT(id) DO UPDATE SET {}"
        .format(columns, marks, updates),
        event_values(ev),
    )
    upsert_event_alias(
        con, ev["id"], ev.get("url"), ev.get("source_id"),
        ev.get("first_seen"), ev.get("last_seen"),
    )
    upsert_event_alias(
        con, ev["id"], ev.get("signup_url"), ev.get("source_id"),
        ev.get("first_seen"), ev.get("last_seen"),
    )


def load_store(path=DB):
    con = connect(path)
    try:
        rows = con.execute("SELECT * FROM events ORDER BY start, title").fetchall()
        return [row_to_event(r) for r in rows]
    finally:
        con.close()


def save_store(events, path=DB):
    con = connect(path)
    try:
        with con:
            for ev in events:
                upsert_event(con, ev)
    finally:
        con.close()


def load_source_runs(run_id, path=DB):
    if not run_id:
        return []
    con = connect(path)
    try:
        rows = con.execute(
            """SELECT sr.*, r.seen_date, r.started_at AS run_started_at,
                      (SELECT prior.state
                       FROM source_runs prior
                       JOIN runs pr ON pr.id = prior.run_id
                       WHERE prior.source_id = sr.source_id
                         AND (pr.started_at < r.started_at
                              OR (pr.started_at = r.started_at
                                  AND prior.run_id < sr.run_id))
                       ORDER BY pr.started_at DESC, prior.run_id DESC LIMIT 1)
                      AS previous_state
               FROM source_runs sr JOIN runs r ON r.id = sr.run_id
               WHERE sr.run_id = ? ORDER BY sr.source_id""", (run_id,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["rejection_counts"] = json.loads(
                item.pop("rejection_counts_json") or "{}")
            item["artifacts"] = json.loads(item.pop("artifacts_json") or "[]")
            item["work"] = json.loads(item.pop("work_json") or "{}")
            item["state_changed"] = bool(
                item.get("previous_state")
                and item["previous_state"] != item.get("state"))
            out.append(item)
        return out
    finally:
        con.close()


def load_run_totals(run_id, path=DB):
    if not run_id:
        return None
    con = connect(path)
    try:
        exists = con.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not exists:
            return None
        row = con.execute(
            """SELECT COUNT(*) AS scraped,
                      SUM(CASE WHEN outcome NOT IN ('duplicate', 'rejected')
                               THEN 1 ELSE 0 END) AS after_dedup,
                      SUM(CASE WHEN outcome = 'new' THEN 1 ELSE 0 END) AS new
               FROM discoveries WHERE run_id = ?""", (run_id,)
        ).fetchone()
        store = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "scraped": row["scraped"] or 0,
            "after_dedup": row["after_dedup"] or 0,
            "new": row["new"] or 0,
            "store": store,
        }
    finally:
        con.close()


def load_latest_source_health(path=DB):
    con = connect(path)
    try:
        return [dict(row) for row in con.execute(
            "SELECT * FROM latest_source_health ORDER BY source_id"
        ).fetchall()]
    finally:
        con.close()


# ------------------------------------------------------------- crawl helpers

def source_row_from_payload(source):
    def count(*names):
        for name in names:
            value = source.get(name)
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return 0

    work = source.get("work")
    if work is None:
        work = source.get("work_json") or "{}"
    work_json = work if isinstance(work, str) else json.dumps(
        work or {}, ensure_ascii=False)
    rejection_counts = source.get("rejection_counts")
    rejection_counts_json = source.get("rejection_counts_json") \
        if rejection_counts is None else None
    if not isinstance(rejection_counts_json, str):
        rejection_counts_json = json.dumps(
            rejection_counts or {}, ensure_ascii=False)
    artifacts = source.get("artifacts")
    artifacts_json = source.get("artifacts_json") if artifacts is None else None
    if not isinstance(artifacts_json, str):
        artifacts_json = json.dumps(artifacts or [], ensure_ascii=False)

    return {
        "source_id": source.get("source_id") or source.get("id") or "unknown",
        "method": source.get("method") or "unknown",
        "recipe_version": source.get("recipe_version"),
        "audit": int(bool(source.get("audit"))),
        "state": ("empty_verified" if source.get("state") == "empty"
                  else source.get("state") or "ok"),
        "started_at": source.get("started_at"),
        "finished_at": source.get("finished_at"),
        "found_count": count("found_count", "found", "events_found"),
        "parsed_count": count("parsed_count", "parsed"),
        "qualified_count": count(
            "qualified_count", "kept", "events_kept", "qualified",
            "events_qualified"),
        "rejected_count": count("rejected_count", "rejected"),
        "new_count": count("new_count", "new"),
        "updated_count": count("updated_count", "updated"),
        "unchanged_count": count("unchanged_count", "unchanged"),
        "duplicate_count": count("duplicate_count", "duplicates"),
        "rejection_counts_json": rejection_counts_json,
        "artifacts_json": artifacts_json,
        "work_json": work_json,
        "detail": source.get("detail"),
        "error": source.get("error"),
    }


def upsert_source_run(con, run_id, source):
    s = source_row_from_payload(source)
    con.execute(
        """INSERT INTO source_runs
           (run_id, source_id, method, recipe_version, audit, state, started_at,
            finished_at, found_count, parsed_count, qualified_count,
            rejected_count, new_count, updated_count, unchanged_count,
            duplicate_count, rejection_counts_json, artifacts_json, work_json,
            detail, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id, source_id) DO UPDATE SET
             method=excluded.method, recipe_version=excluded.recipe_version,
             audit=excluded.audit, state=excluded.state,
             started_at=excluded.started_at,
             finished_at=excluded.finished_at, found_count=excluded.found_count,
             parsed_count=excluded.parsed_count,
             qualified_count=excluded.qualified_count,
             rejected_count=excluded.rejected_count, new_count=excluded.new_count,
             updated_count=excluded.updated_count,
             unchanged_count=excluded.unchanged_count,
             duplicate_count=excluded.duplicate_count,
             rejection_counts_json=excluded.rejection_counts_json,
             artifacts_json=excluded.artifacts_json,
             work_json=excluded.work_json,
             detail=excluded.detail, error=excluded.error""",
        (run_id, s["source_id"], s["method"], s["recipe_version"], s["audit"],
         s["state"], s["started_at"], s["finished_at"], s["found_count"], s["parsed_count"],
         s["qualified_count"], s["rejected_count"], s["new_count"],
         s["updated_count"], s["unchanged_count"], s["duplicate_count"],
         s["rejection_counts_json"], s["artifacts_json"], s["work_json"],
         s["detail"], s["error"]),
    )


def record_discovery(con, run_id, source_id, raw, outcome,
                     event_id=None, rejection_reason=None):
    key = event_key(raw) if isinstance(raw, dict) else None
    con.execute(
        """INSERT INTO discoveries
           (run_id, source_id, source_event_key, raw_json, outcome,
            rejection_reason, event_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, source_id or "unknown", key,
         json.dumps(raw, ensure_ascii=False), outcome, rejection_reason, event_id),
    )


def text_similarity(left, right):
    left = norm_text(left)
    right = norm_text(right)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def minutes_apart(left, right):
    left = parse_dt(left)
    right = parse_dt(right)
    if not left or not right:
        return None
    left_aware = left.tzinfo is not None and left.utcoffset() is not None
    right_aware = right.tzinfo is not None and right.utcoffset() is not None
    if left_aware and right_aware:
        delta = left.astimezone(dt.timezone.utc) - right.astimezone(dt.timezone.utc)
    else:
        delta = left.replace(tzinfo=None) - right.replace(tzinfo=None)
    return abs(delta.total_seconds()) / 60


def same_event_occurrence(incoming, candidate):
    """Use strong event facts to compare candidates that have different URLs."""
    distance = minutes_apart(incoming.get("start"), candidate.get("start"))
    title_score = text_similarity(incoming.get("title"), candidate.get("title"))
    if distance is None or distance > 30 or title_score < 0.88:
        return False

    address_score = text_similarity(
        incoming.get("address"), candidate.get("address"))
    venue_score = text_similarity(
        incoming.get("venue"), candidate.get("venue"))
    address_match = address_score >= 0.92
    venue_match = venue_score >= 0.90
    # The same host can run simultaneous editions in different rooms. Do not
    # let a host match override strong, nonempty evidence that both the venue
    # and address are different.
    known_place_conflict = (
        norm_text(incoming.get("venue")) != ""
        and norm_text(candidate.get("venue")) != ""
        and venue_score < 0.70
        and norm_text(incoming.get("address")) != ""
        and norm_text(candidate.get("address")) != ""
        and address_score < 0.70
    )
    if known_place_conflict:
        return False
    host_match = text_similarity(
        incoming.get("host"), candidate.get("host")) >= 0.92
    neighborhood_match = (
        title_score == 1.0
        and norm_text(incoming.get("neighborhood")) != ""
        and norm_text(incoming.get("neighborhood"))
        == norm_text(candidate.get("neighborhood"))
    )
    return address_match or venue_match or host_match or neighborhood_match


def find_alias_match(con, ev):
    event_day = start_date(ev)
    canonicals = []
    for value in (ev.get("url"), ev.get("signup_url")):
        canonical = canon_url(value)
        if is_event_url(canonical) and canonical not in canonicals:
            canonicals.append(canonical)
    if not canonicals or not event_day:
        return None
    candidates_by_id = {}
    for canonical in canonicals:
        rows = con.execute(
            """SELECT e.* FROM event_urls u
               JOIN events e ON e.id = u.event_id
               WHERE u.canonical_url=? AND substr(e.start, 1, 10)=?""",
            (canonical, event_day.isoformat()),
        ).fetchall()
        for row in rows:
            candidates_by_id[row["id"]] = row_to_event(
                row, include_dedup_key=True)
    candidates = list(candidates_by_id.values())
    if len(candidates) == 1:
        return candidates[0]
    close = []
    for candidate in candidates:
        distance = minutes_apart(ev.get("start"), candidate.get("start"))
        if distance is not None and distance <= 30:
            close.append(candidate)
    return close[0] if len(close) == 1 else None


def find_occurrence_match(con, ev):
    event_day = start_date(ev)
    if not event_day:
        return None
    rows = con.execute(
        "SELECT * FROM events WHERE substr(start, 1, 10)=? ORDER BY start, id",
        (event_day.isoformat(),),
    ).fetchall()
    candidates = [row_to_event(row, include_dedup_key=True) for row in rows]
    matches = [candidate for candidate in candidates
               if same_event_occurrence(ev, candidate)]
    return matches[0] if len(matches) == 1 else None


def find_existing_event(con, ev):
    """Resolve an incoming event without changing its stable internal ID."""
    alias_match = find_alias_match(con, ev)
    if alias_match:
        return alias_match
    exact = get_event(con, ev["id"])
    if exact:
        return exact
    return find_occurrence_match(con, ev)


def merge_one(con, raw, seen):
    ev = normalise_incoming(raw, seen)
    if not ev["start"]:
        return ev, "rejected", "missing_start"
    cur = find_existing_event(con, ev)
    if cur is None:
        upsert_event(con, ev)
        return ev, "new", None

    changed = False
    for field in VOLATILE_FIELDS:
        new = ev.get(field)
        can_clear = (
            field in SAME_SOURCE_CLEARABLE_FIELDS
            and field in raw
            and cur.get("source_id") == ev.get("source_id")
        )
        if (new not in (None, "", [], False) or can_clear) \
                and cur.get(field) != new:
            cur[field] = new
            changed = True
    if len(ev["description"]) > len(cur.get("description") or ""):
        cur["description"] = ev["description"]
        changed = True
    if not is_event_url(canon_url(cur.get("url"))) \
            and is_event_url(canon_url(ev.get("url"))):
        cur["url"] = ev["url"]
        changed = True
    alias_added = upsert_event_alias(
        con, cur["id"], ev.get("url"), ev.get("source_id"), seen, seen,
    )
    alias_added = upsert_event_alias(
        con, cur["id"], ev.get("signup_url"), ev.get("source_id"), seen, seen,
    ) or alias_added
    changed = changed or alias_added
    cur["last_seen"] = seen
    upsert_event(con, cur)
    return cur, "updated" if changed else "unchanged", None


def validate_crawl_payload(payload):
    """Validate a legacy file-based crawl payload before importing it."""
    if not isinstance(payload, dict):
        return []  # Legacy event arrays remain supported.

    errors = []
    run = payload.get("run") or {}
    manifest = run.get("manifest_source_ids")
    if not isinstance(manifest, list) or not manifest:
        errors.append("run.manifest_source_ids is required; start with plan-run")
        manifest = []
    if len(manifest) != len(set(manifest)):
        errors.append("run.manifest_source_ids contains duplicates")

    rows = payload.get("sources")
    if not isinstance(rows, list):
        errors.append("sources must be a list")
        rows = []
    row_ids = [source_row_from_payload(row)["source_id"] for row in rows]
    if len(row_ids) != len(set(row_ids)):
        errors.append("sources contains duplicate source_id rows")
    missing = sorted(set(manifest) - set(row_ids))
    extra = sorted(set(row_ids) - set(manifest))
    if missing:
        errors.append("sources missing manifest rows: {}".format(", ".join(missing)))
    if extra:
        errors.append("sources not present in manifest: {}".format(", ".join(extra)))

    for row in rows:
        source_id = source_row_from_payload(row)["source_id"]
        state = row.get("state")
        if state == "pending":
            errors.append("{} is still pending".format(source_id))
        elif state not in LEGACY_SOURCE_STATES:
            errors.append("{} has invalid state {!r}".format(source_id, state))

    allowed = set(manifest)
    for section in ("events", "rejections"):
        values = payload.get(section) or []
        if not isinstance(values, list):
            errors.append("{} must be a list".format(section))
            continue
        for i, item in enumerate(values):
            if not isinstance(item, dict):
                errors.append("{}[{}] must be an object".format(section, i))
                continue
            raw = item.get("raw") or item.get("event") or item
            source_id = item.get("source_id") or raw.get("source_id")
            if source_id not in allowed:
                errors.append("{}[{}] has unplanned source_id {!r}".format(
                    section, i, source_id))
    return errors


def persist_run_plan(con, plan):
    """Store one run plan and its retrieval instructions in the crawl ledger."""
    run = plan["run"]
    run_id = run["id"]
    if con.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
        raise ValueError("run already exists: {}".format(run_id))
    ensure_run(con, run_id, run["seen_date"], run)
    work_by_source = {item["source_id"]: item for item in plan.get("work", [])}
    for source in plan.get("sources", []):
        stored = dict(source)
        stored["work"] = work_by_source.get(source["source_id"], {})
        upsert_source_run(con, run_id, stored)
    if not plan.get("work"):
        con.execute(
            "UPDATE runs SET state='completed', finished_at=? WHERE id=?",
            (now_iso(), run_id),
        )


def read_json_input(path):
    if path and path != "-":
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return json.load(sys.stdin)


def stage_source_result(con, run_id, source_id, payload):
    """Replace one source's staged candidates while its run is active."""
    if not isinstance(payload, dict):
        raise ValueError("source result must be one JSON object")
    source = payload.get("source") or {}
    events = payload.get("events") or []
    rejections = payload.get("rejections") or []
    if not isinstance(source, dict):
        raise ValueError("source must be one JSON object")
    if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
        raise ValueError("events must be a list of JSON objects")
    if not isinstance(rejections, list) or not all(
            isinstance(item, dict) for item in rejections):
        raise ValueError("rejections must be a list of JSON objects")
    state = source.get("state")
    if state not in SOURCE_STATES:
        raise ValueError("source.state must be a final retrieval state")

    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("unknown run id: {}".format(run_id))
    if run["state"] != "running":
        raise ValueError("run is not active: {}".format(run_id))
    planned = con.execute(
        "SELECT * FROM source_runs WHERE run_id=? AND source_id=?",
        (run_id, source_id),
    ).fetchone()
    if not planned:
        raise ValueError("source {} is not planned for run {}".format(
            source_id, run_id))
    staged_events = []
    for raw in events:
        item = dict(raw)
        item_source = item.get("source_id")
        if item_source and item_source != source_id:
            raise ValueError("event has a different source_id: {}".format(item_source))
        item["source_id"] = source_id
        staged_events.append(item)

    staged_rejections = []
    rejection_counts = {}
    for rejection in rejections:
        item = dict(rejection)
        raw = dict(item.get("raw") or item.get("event") or item)
        item_source = item.get("source_id") or raw.get("source_id")
        if item_source and item_source != source_id:
            raise ValueError("rejection has a different source_id: {}".format(
                item_source))
        raw["source_id"] = source_id
        reason = item.get("reason") or "rejected"
        staged_rejections.append((raw, reason))
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    con.execute(
        "DELETE FROM discoveries WHERE run_id=? AND source_id=?",
        (run_id, source_id),
    )
    for raw in staged_events:
        record_discovery(con, run_id, source_id, raw, "staged")
    for raw, reason in staged_rejections:
        record_discovery(
            con, run_id, source_id, raw, "rejected",
            rejection_reason=reason,
        )

    missing_start = sum(
        not (item.get("start") or item.get("start_datetime"))
        for item in staged_events
    )
    base = dict(planned)
    base.update(source)
    base.update({
        "source_id": source_id,
        "started_at": source.get("started_at") or planned["started_at"] or now_iso(),
        "finished_at": source.get("finished_at") or now_iso(),
        "found_count": len(staged_events) + len(staged_rejections),
        "parsed_count": len(staged_events) - missing_start,
        "qualified_count": len(staged_events) - missing_start,
        "rejected_count": len(staged_rejections) + missing_start,
        "new_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "duplicate_count": 0,
        "rejection_counts": rejection_counts,
    })
    upsert_source_run(con, run_id, base)
    return len(staged_events), len(staged_rejections)


def finalize_database_run(con, run_id):
    """Merge all staged candidates and complete one database-backed run."""
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("unknown run id: {}".format(run_id))
    if run["state"] != "running":
        raise ValueError("run is not active: {}".format(run_id))
    pending = [row[0] for row in con.execute(
        "SELECT source_id FROM source_runs WHERE run_id=? AND state='pending' "
        "ORDER BY source_id",
        (run_id,),
    ).fetchall()]
    if pending:
        raise ValueError("run still has pending sources: {}".format(
            ", ".join(pending)))

    computed = {}

    def metrics(source_id):
        return computed.setdefault(source_id or "unknown", {
            "found": 0, "parsed": 0, "rejected": 0, "new": 0,
            "updated": 0, "unchanged": 0, "duplicates": 0,
            "rejection_counts": {},
        })

    seen = run["seen_date"]
    seen_event_ids = set()
    totals = {
        "new": 0, "updated": 0, "unchanged": 0,
        "duplicates": 0, "rejected": 0,
    }
    discoveries = con.execute(
        "SELECT * FROM discoveries WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    for row in discoveries:
        source_id = row["source_id"]
        raw = json.loads(row["raw_json"])
        m = metrics(source_id)
        m["found"] += 1
        if row["outcome"] == "rejected":
            reason = row["rejection_reason"] or "rejected"
            m["rejected"] += 1
            m["rejection_counts"][reason] = \
                m["rejection_counts"].get(reason, 0) + 1
            totals["rejected"] += 1
            continue
        if row["outcome"] != "staged":
            raise ValueError(
                "run contains an unexpected discovery outcome: {}".format(
                    row["outcome"]))

        ev = normalise_incoming(raw, seen)
        if not ev["start"]:
            reason = "missing_start"
            m["rejected"] += 1
            m["rejection_counts"][reason] = \
                m["rejection_counts"].get(reason, 0) + 1
            totals["rejected"] += 1
            con.execute(
                "UPDATE discoveries SET outcome='rejected', "
                "rejection_reason=?, event_id=NULL WHERE id=?",
                (reason, row["id"]),
            )
            continue

        m["parsed"] += 1
        merged, outcome, reason = merge_one(con, raw, seen)
        event_id = merged.get("id")
        if event_id in seen_event_ids:
            outcome = "duplicate"
            reason = None
            m["duplicates"] += 1
            totals["duplicates"] += 1
        else:
            seen_event_ids.add(event_id)
            m[outcome] += 1
            totals[outcome] += 1
        con.execute(
            "UPDATE discoveries SET outcome=?, rejection_reason=?, event_id=? "
            "WHERE id=?",
            (outcome, reason, event_id, row["id"]),
        )

    source_rows = con.execute(
        "SELECT * FROM source_runs WHERE run_id=? ORDER BY source_id", (run_id,)
    ).fetchall()
    for source_row in source_rows:
        source_id = source_row["source_id"]
        base = dict(source_row)
        m = metrics(source_id)
        base.update({
            "found_count": m["found"],
            "parsed_count": m["parsed"],
            "qualified_count": (
                m["new"] + m["updated"] + m["unchanged"] + m["duplicates"]),
            "rejected_count": m["rejected"],
            "new_count": m["new"],
            "updated_count": m["updated"],
            "unchanged_count": m["unchanged"],
            "duplicate_count": m["duplicates"],
            "rejection_counts": m["rejection_counts"],
        })
        if base["state"] == "ok" and m["rejection_counts"].get("missing_start"):
            base["state"] = "validation_failed"
        elif base["state"] == "ok" and m["found"] == 0:
            base["state"] = "empty_suspicious"
        upsert_source_run(con, run_id, base)

    con.execute(
        "UPDATE runs SET finished_at=?, state='completed' WHERE id=?",
        (now_iso(), run_id),
    )
    totals["existing"] = totals["updated"] + totals["unchanged"]
    totals["store"] = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return totals


# ---------------------------------------------------------------- commands

def cmd_plan_run(args):
    seen = dt.date.fromisoformat(args.date) if args.date else today()
    try:
        plan = build_run_plan(
            load_catalog(), load_latest_source_health(), seen,
            mail_available=args.mail_available, run_id=args.run_id,
            only_source=args.only_source,
        )
    except ValueError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    if args.output:
        parent = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(parent, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("wrote {} ({} due, {} total sources)".format(
            args.output, len(plan["work"]), len(plan["sources"])))
    else:
        con = connect()
        try:
            with con:
                persist_run_plan(con, plan)
        except ValueError as error:
            print("ERROR: {}".format(error), file=sys.stderr)
            return 1
        finally:
            con.close()
        json.dump(plan, sys.stdout, indent=2, ensure_ascii=False)
        print()
    return 0


def cmd_run_work(args):
    con = connect()
    try:
        run = con.execute("SELECT * FROM runs WHERE id = ?", (args.run_id,)).fetchone()
        if not run:
            print("ERROR: unknown run id: {}".format(args.run_id), file=sys.stderr)
            return 1
        rows = con.execute(
            "SELECT source_id, work_json FROM source_runs "
            "WHERE run_id=? AND state='pending' ORDER BY source_id",
            (args.run_id,),
        ).fetchall()
        work = []
        for row in rows:
            item = json.loads(row["work_json"] or "{}")
            if item:
                work.append(item)
        json.dump({"run": dict(run), "work": work}, sys.stdout,
                  indent=2, ensure_ascii=False)
        print()
        return 0
    finally:
        con.close()


def cmd_record_source(args):
    try:
        payload = read_json_input(args.input)
    except (OSError, json.JSONDecodeError) as error:
        print("ERROR: cannot read source result: {}".format(error), file=sys.stderr)
        return 1
    con = connect()
    try:
        try:
            with con:
                event_count, rejection_count = stage_source_result(
                    con, args.run_id, args.source_id, payload)
        except ValueError as error:
            print("ERROR: {}".format(error), file=sys.stderr)
            return 1
        print("recorded {}: {} events, {} rejections".format(
            args.source_id, event_count, rejection_count))
        return 0
    finally:
        con.close()


def cmd_finalize_run(args):
    con = connect()
    try:
        try:
            con.execute("BEGIN")
            totals = finalize_database_run(con, args.run_id)
            if args.dry_run:
                con.rollback()
                print("DRY RUN — nothing written")
            else:
                con.commit()
        except ValueError as error:
            con.rollback()
            print("ERROR: {}".format(error), file=sys.stderr)
            return 1
        print("run id: {}".format(args.run_id))
        print("finalized: +{} new, {} existing ({} updated, {} unchanged), "
              "{} duplicates, {} rejected".format(
                  totals["new"], totals["existing"], totals["updated"],
                  totals["unchanged"], totals["duplicates"], totals["rejected"]))
        print("store now holds {} events".format(totals["store"]))
        unranked = con.execute(
            """SELECT COUNT(*) FROM events
               WHERE rank IS NULL
                 AND substr(start, 1, 10) >= date('now', 'localtime')"""
        ).fetchone()[0]
        if unranked:
            print("{} upcoming events need a rank — run: events_store.py needs-rank"
                  .format(unranked))
        return 0
    finally:
        con.close()

def cmd_migrate(args):
    con = connect()
    try:
        existing = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if existing:
            print("social.db already contains {} events; migration refused".format(existing),
                  file=sys.stderr)
            return 1
        with open(args.events_json, "r", encoding="utf-8") as fh:
            legacy = json.load(fh)
        if isinstance(legacy, dict):
            legacy = legacy.get("events", [])

        discoveries = []
        if args.discoveries:
            with open(args.discoveries, "r", encoding="utf-8") as fh:
                discoveries = json.load(fh)
            if isinstance(discoveries, dict):
                discoveries = discoveries.get("events", [])

        with con:
            for raw in legacy:
                ev = normalise_incoming(raw, raw.get("last_seen") or today().isoformat())
                upsert_event(con, ev)

            run_id = None
            if discoveries:
                date = today().isoformat()
                run_id = args.run_id or "legacy-{}".format(date)
                ensure_run(con, run_id, date, {"started_at": date + "T00:00:00-04:00"})
                con.execute(
                    """UPDATE runs SET finished_at=?, state='completed',
                       catalog_hash='legacy-json-import'
                       WHERE id=?""",
                    (date + "T23:59:59-04:00", run_id),
                )

                per_source = {}
                for raw in discoveries:
                    ev = normalise_incoming(raw, date)
                    eid = ev["id"] if ev["start"] and get_event(con, ev["id"]) else None
                    outcome = "new" if eid else "rejected"
                    reason = None if eid else "missing_start"
                    record_discovery(con, run_id, ev.get("source_id"), raw,
                                     outcome, eid, reason)
                    if eid:
                        per_source[ev.get("source_id") or "unknown"] = \
                            per_source.get(ev.get("source_id") or "unknown", 0) + 1
                for source_id, count in per_source.items():
                    upsert_source_run(con, run_id, {
                        "source_id": source_id, "state": "ok",
                        "method": "legacy_json",
                    })
                    con.execute(
                        """UPDATE source_runs
                           SET new_count=?, parsed_count=MAX(parsed_count, ?)
                           WHERE run_id=? AND source_id=?""",
                        (count, count, run_id, source_id),
                    )

        print("migrated {} events into social.db".format(len(legacy)))
        if discoveries:
            print("imported legacy run {} with {} discoveries"
                  .format(run_id, len(discoveries)))
        return 0
    finally:
        con.close()


def cmd_merge(args):
    with open(args.input, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        incoming = payload.get("events", [])
        rejections = payload.get("rejections", [])
        sources = payload.get("sources", [])
        run_meta = payload.get("run", {})
    else:
        incoming, rejections, sources, run_meta = payload, [], [], {}

    errors = validate_crawl_payload(payload)
    if errors:
        for error in errors:
            print("ERROR: {}".format(error), file=sys.stderr)
        return 1

    seen = args.seen or run_meta.get("seen_date") or today().isoformat()
    run_id = args.run_id or run_meta.get("id") or make_run_id(seen)
    con = connect()
    added = updated = unchanged = collapsed = rejected = 0
    seen_event_ids = set()
    computed = {}

    def metrics(source_id):
        return computed.setdefault(source_id or "unknown", {
            "found": 0, "parsed": 0, "rejected": 0, "new": 0,
            "updated": 0, "unchanged": 0, "duplicates": 0,
            "rejection_counts": {},
        })

    try:
        con.execute("BEGIN")
        ensure_run(con, run_id, seen, run_meta)
        for source in sources:
            upsert_source_run(con, run_id, source)

        for raw in incoming:
            source_id = raw.get("source_id") or "unknown"
            m = metrics(source_id)
            m["found"] += 1
            ev = normalise_incoming(raw, seen)
            if not ev["start"]:
                rejected += 1
                m["rejected"] += 1
                m["rejection_counts"]["missing_start"] = \
                    m["rejection_counts"].get("missing_start", 0) + 1
                record_discovery(con, run_id, source_id, raw, "rejected",
                                 rejection_reason="missing_start")
                continue
            m["parsed"] += 1
            merged, outcome, reason = merge_one(con, raw, seen)
            event_id = merged.get("id")
            if event_id in seen_event_ids:
                collapsed += 1
                m["duplicates"] += 1
                record_discovery(con, run_id, source_id, raw, "duplicate", event_id)
                continue
            seen_event_ids.add(event_id)
            record_discovery(con, run_id, source_id, raw, outcome,
                             event_id, reason)
            m[outcome] += 1
            added += outcome == "new"
            updated += outcome == "updated"
            unchanged += outcome == "unchanged"

        for rejection in rejections:
            raw = rejection.get("raw") or rejection.get("event") or rejection
            source_id = rejection.get("source_id") or raw.get("source_id") or "unknown"
            reason = rejection.get("reason") or "rejected"
            m = metrics(source_id)
            m["found"] += 1
            m["rejected"] += 1
            m["rejection_counts"][reason] = m["rejection_counts"].get(reason, 0) + 1
            rejected += 1
            record_discovery(con, run_id, source_id, raw, "rejected",
                             rejection_reason=reason)

        provided = {source_row_from_payload(s)["source_id"]: s for s in sources}
        for source_id in sorted(set(provided) | set(computed)):
            base = source_row_from_payload(provided.get(source_id, {
                "source_id": source_id, "state": "ok"
            }))
            m = metrics(source_id)
            base["found_count"] = m["found"]
            base["parsed_count"] = m["parsed"]
            base["qualified_count"] = (m["new"] + m["updated"]
                                       + m["unchanged"] + m["duplicates"])
            base["rejected_count"] = m["rejected"]
            base["new_count"] = m["new"]
            base["updated_count"] = m["updated"]
            base["unchanged_count"] = m["unchanged"]
            base["duplicate_count"] = m["duplicates"]
            base["rejection_counts"] = m["rejection_counts"]
            base["artifacts"] = json.loads(base.pop("artifacts_json") or "[]")
            if base["state"] == "ok" and m["rejection_counts"].get("missing_start"):
                base["state"] = "validation_failed"
            elif base["state"] == "ok" and m["found"] == 0:
                base["state"] = "empty_suspicious"
            upsert_source_run(con, run_id, base)

        con.execute(
            "UPDATE runs SET finished_at=?, state='completed' WHERE id=?",
            (run_meta.get("finished_at") or now_iso(), run_id),
        )
        store_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if args.dry_run:
            con.rollback()
            print("DRY RUN — nothing written")
        else:
            con.commit()

        print("run id: {}".format(run_id))
        existing = updated + unchanged
        print("merged {}: +{} new, {} existing ({} updated, {} unchanged), "
              "{} duplicates, {} rejected".format(
                  os.path.basename(args.input), added, existing, updated,
                  unchanged, collapsed, rejected))
        print("store now holds {} events".format(store_count))
        unranked = con.execute(
            """SELECT COUNT(*) FROM events
               WHERE rank IS NULL
                 AND substr(start, 1, 10) >= date('now', 'localtime')"""
        ).fetchone()[0]
        if unranked:
            print("{} upcoming events need a rank — run: events_store.py needs-rank"
                  .format(unranked))
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _needs_rank(ev, force=False):
    if start_date(ev) and start_date(ev) < today():
        return False
    if ev.get("status") == "cancelled":
        return False
    return True if force else ev.get("rank") not in RANKS


def cmd_needs_rank(args):
    store = load_store()
    horizon = today() + dt.timedelta(days=args.days) if args.days else None
    force = args.all or bool(args.host)
    out = []
    for ev in store:
        if not _needs_rank(ev, force=force):
            continue
        sd = start_date(ev)
        if horizon and sd and sd > horizon:
            continue
        if args.host and args.host.lower() not in (ev.get("host") or "").lower() \
                and args.host != ev.get("source_id"):
            continue
        out.append(ev)
    out.sort(key=lambda e: e.get("start") or "")
    if args.grouped:
        groups = grouped_rank_queue(out)
        if args.json:
            json.dump(groups, sys.stdout, indent=1, ensure_ascii=False)
            print()
            return 0
        if not groups:
            print("nothing needs ranking")
            return 0
        print("{} events in {} ranking groups".format(len(out), len(groups)))
        for group in groups:
            ev = group["representative"]
            print("  {}  {}  [{}]  {} ({} occurrence{})".format(
                ev["id"], (ev.get("start") or "")[:16],
                ev.get("source_id", ""), (ev.get("title") or "")[:60],
                len(group["occurrences"]),
                "s" if len(group["occurrences"]) != 1 else ""))
        return 0
    if args.json:
        json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return 0
    if not out:
        print("nothing needs ranking")
        return 0
    print("{} events need a rank".format(len(out)))
    for ev in out:
        print("  {}  {}  [{}]  {}".format(
            ev["id"], (ev.get("start") or "")[:16], ev.get("source_id", ""),
            (ev.get("title") or "")[:70]))
    return 0


def cmd_rank(args):
    con = connect()
    payload = {}
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and ("groups" in payload or "events" in payload):
            expanded = {}
            for group in payload.get("groups") or []:
                values = {key: value for key, value in group.items() if key != "ids"}
                for eid in group.get("ids") or []:
                    expanded[eid] = dict(values)
            for eid, values in (payload.get("events") or {}).items():
                expanded.setdefault(eid, {}).update(values)
            payload = expanded
    for pair in args.set or []:
        if "=" not in pair:
            print("bad --set (want ID=rank): {}".format(pair), file=sys.stderr)
            return 1
        eid, rank = pair.split("=", 1)
        payload[eid.strip()] = {"rank": rank.strip()}

    stamp = today().isoformat()
    applied = missing = bad = 0
    try:
        with con:
            for eid, vals in payload.items():
                ev = get_event(con, eid)
                if ev is None:
                    missing += 1
                    continue
                rank = (vals.get("rank") or "").strip().lower()
                if rank and rank not in RANKS:
                    print("bad rank {!r} for {}".format(rank, eid), file=sys.stderr)
                    bad += 1
                    continue
                if rank:
                    ev["rank"] = rank
                if vals.get("fit_note"):
                    ev["fit_note"] = vals["fit_note"]
                if vals.get("format_tags"):
                    ev["format_tags"] = vals["format_tags"]
                if vals.get("catch"):
                    ev["catch"] = vals["catch"]
                ev["ranked_on"] = stamp
                upsert_event(con, ev)
                applied += 1
        print("ranked {} events ({} ids not found, {} rejected)"
              .format(applied, missing, bad))
        return 1 if bad else 0
    finally:
        con.close()


def cmd_upcoming(args):
    lo = today()
    hi = lo + dt.timedelta(days=args.days)
    floor = RANK_ORDER.get(args.min_rank, 3) if args.min_rank else 3
    out = []
    for ev in load_store():
        if ev.get("status") == "cancelled":
            continue
        sd = start_date(ev)
        if sd is None or sd < lo or sd > hi:
            continue
        if RANK_ORDER.get(ev.get("rank"), 3) > floor:
            continue
        if args.source_id and ev.get("source_id") != args.source_id:
            continue
        out.append(ev)
    out.sort(key=lambda e: (e.get("start") or "", RANK_ORDER.get(e.get("rank"), 3)))
    if args.json:
        json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return 0
    if not out:
        print("no events in the next {} days matching that filter".format(args.days))
        return 0
    cur = None
    for ev in out:
        sd = start_date(ev)
        if sd != cur:
            cur = sd
            print("\n== {} {}".format(sd.isoformat(), sd.strftime("%a")))
        price = "free" if ev.get("is_free") else (ev.get("price") or "?")
        print("  [{}] {}  {}".format((ev.get("rank") or "unranked")[:6].ljust(6),
                                     (ev.get("start") or "")[11:16],
                                     (ev.get("title") or "")[:62]))
        print("         {} · {} · {} · {}".format(
            ev.get("neighborhood") or "?", price, ev.get("source_id") or "?", ev["id"]))
        if ev.get("fit_note"):
            print("         fit: {}".format(ev["fit_note"][:110]))
        if ev.get("catch"):
            print("         catch: {}".format(ev["catch"][:110]))
    print("\n{} events".format(len(out)))
    return 0


def cmd_source_stats(args):
    con = connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM latest_source_health ORDER BY source_id"
        ).fetchall()]
        for row in rows:
            row["rejection_counts"] = json.loads(row.pop("rejection_counts_json") or "{}")
            row["artifacts"] = json.loads(row.pop("artifacts_json") or "[]")
        if args.json:
            json.dump(rows, sys.stdout, indent=1, ensure_ascii=False)
            print()
            return 0
        if not rows:
            print("no source runs recorded")
            return 0
        for row in rows:
            print("{:<32} {:<18} {}  found {} parsed {} kept {} new {}"
                  .format(row["source_id"], row["state"], row["seen_date"],
                          row["found_count"], row["parsed_count"],
                          row["qualified_count"], row["new_count"]))
        return 0
    finally:
        con.close()


def validate_catalog(catalog):
    profiles = catalog.get("retrieval_profiles") or {}
    sources = catalog.get("sources") or []
    errors = []
    seen = set()
    for source in sources:
        source_id = source.get("id")
        if not source_id:
            errors.append("source without id")
            continue
        if source_id in seen:
            errors.append("duplicate source id: {}".format(source_id))
        seen.add(source_id)
        if "enabled" in source and not isinstance(source["enabled"], bool):
            errors.append("{} has non-boolean enabled flag".format(source_id))
        if source.get("enabled", True) is False and not source.get("disabled_reason"):
            errors.append("{} is disabled without disabled_reason".format(source_id))
        if "cadence" in source:
            errors.append("{} has obsolete cadence field".format(source_id))
        if "adapter" in source and (
                not isinstance(source["adapter"], str) or not source["adapter"].strip()):
            errors.append("{} has invalid adapter".format(source_id))
        profile_name = source.get("retrieval_profile")
        if profile_name not in profiles:
            errors.append("{} references missing profile {}"
                          .format(source_id, profile_name or "<none>"))
    for name, profile in profiles.items():
        if not profile.get("primary"):
            errors.append("profile {} has no primary recipe".format(name))
        if not profile.get("required_fields"):
            errors.append("profile {} has no required fields".format(name))
        if "empty_signal" not in profile:
            errors.append("profile {} has no explicit empty rule".format(name))
        if "audit_cadence" in profile:
            errors.append("profile {} has obsolete audit_cadence field".format(name))
    inbox = catalog.get("inbox_sources") or {}
    inbox_profile = inbox.get("retrieval_profile")
    if inbox.get("items") and inbox_profile not in profiles:
        errors.append("inbox sources reference missing profile {}".format(
            inbox_profile or "<none>"))
    for source in inbox.get("items") or []:
        source_id = source.get("id")
        if not source_id:
            errors.append("inbox source without id")
            continue
        if source_id in seen:
            errors.append("duplicate source id across catalog and inbox: {}".format(
                source_id))
        if "cadence" in source:
            errors.append("{} has obsolete cadence field".format(source_id))
        seen.add(source_id)
    return errors


def cmd_catalog_check(args):
    catalog = load_catalog()
    profiles = catalog.get("retrieval_profiles") or {}
    sources = catalog.get("sources") or []
    errors = validate_catalog(catalog)
    if errors:
        for error in errors:
            print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    disabled = sum(source.get("enabled", True) is False for source in sources)
    print("catalog ok: {} enabled sources, {} disabled, {} retrieval profiles".format(
        len(sources) - disabled, disabled, len(profiles)))
    return 0


def cmd_stats(args):
    store = load_store()
    up = [ev for ev in store if (start_date(ev) or today()) >= today()]
    past = len(store) - len(up)
    print("store: {} events ({} upcoming, {} past)".format(len(store), len(up), past))
    by_rank = {}
    for ev in up:
        key = ev.get("rank") or "unranked"
        by_rank[key] = by_rank.get(key, 0) + 1
    print("\nupcoming by rank:")
    for key in ("high", "medium", "low", "unranked"):
        if key in by_rank:
            print("  {:<9} {}".format(key, by_rank[key]))
    by_source = {}
    for ev in up:
        key = ev.get("source_id") or "?"
        by_source[key] = by_source.get(key, 0) + 1
    print("\nupcoming by source:")
    for key in sorted(by_source, key=lambda value: -by_source[value]):
        print("  {:<32} {}".format(key, by_source[key]))
    unranked = sum(1 for ev in up if _needs_rank(ev))
    if unranked:
        print("\n{} upcoming events have never been ranked".format(unranked))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="events_store.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")

    plan = sub.add_parser("plan-run")
    plan.add_argument("--date")
    plan.add_argument(
        "--output",
        help="legacy: write a crawl JSON file instead of creating a database run",
    )
    plan.add_argument("--run-id")
    plan.add_argument("--mail-available", action="store_true")
    plan.add_argument("--only-source")
    plan.set_defaults(fn=cmd_plan_run)

    run_work = sub.add_parser("run-work")
    run_work.add_argument("--run-id", required=True)
    run_work.set_defaults(fn=cmd_run_work)

    record_source = sub.add_parser("record-source")
    record_source.add_argument("--run-id", required=True)
    record_source.add_argument("--source-id", required=True)
    record_source.add_argument(
        "--input", default="-",
        help="source-result JSON file, or - to read from stdin (default)",
    )
    record_source.set_defaults(fn=cmd_record_source)

    finalize_run = sub.add_parser("finalize-run")
    finalize_run.add_argument("--run-id", required=True)
    finalize_run.add_argument("--dry-run", action="store_true")
    finalize_run.set_defaults(fn=cmd_finalize_run)

    migrate = sub.add_parser("migrate")
    migrate.add_argument("--events-json", required=True)
    migrate.add_argument("--discoveries")
    migrate.add_argument("--run-id")
    migrate.set_defaults(fn=cmd_migrate)

    merge = sub.add_parser("merge")
    merge.add_argument("--input", required=True)
    merge.add_argument("--seen")
    merge.add_argument("--run-id")
    merge.add_argument("--dry-run", action="store_true")
    merge.set_defaults(fn=cmd_merge)

    needs = sub.add_parser("needs-rank")
    needs.add_argument("--days", type=int, default=0)
    needs.add_argument("--all", action="store_true")
    needs.add_argument("--host")
    needs.add_argument("--grouped", action="store_true")
    needs.add_argument("--json", action="store_true")
    needs.set_defaults(fn=cmd_needs_rank)

    rank = sub.add_parser("rank")
    rank.add_argument("--set", action="append")
    rank.add_argument("--file")
    rank.set_defaults(fn=cmd_rank)

    upcoming = sub.add_parser("upcoming")
    upcoming.add_argument("--days", type=int, default=10)
    upcoming.add_argument("--min-rank", choices=RANKS)
    upcoming.add_argument("--source-id")
    upcoming.add_argument("--json", action="store_true")
    upcoming.set_defaults(fn=cmd_upcoming)

    catalog_check = sub.add_parser("catalog-check")
    catalog_check.set_defaults(fn=cmd_catalog_check)

    source_stats = sub.add_parser("source-stats")
    source_stats.add_argument("--json", action="store_true")
    source_stats.set_defaults(fn=cmd_source_stats)

    stats = sub.add_parser("stats")
    stats.set_defaults(fn=cmd_stats)

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 1
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
