import datetime as dt
from contextlib import closing
import json
import os
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"

import sys

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import events_store
from web.event_theme import event_theme_emoji
from web.calendar_coverage import coverage_view


PROBLEM_STATES = {
    "empty_suspicious", "parse_failed", "validation_failed", "fetch_failed",
    "auth_required", "blocked",
}

EVENT_SORTS = {
    "title": "title COLLATE NOCASE",
    "start": "start",
    "first_seen": "first_seen",
    "last_seen": "last_seen",
    "place": "COALESCE(NULLIF(neighborhood, ''), venue) COLLATE NOCASE",
    "source": "source_id COLLATE NOCASE",
    # Use the shared rank order so filters and sorting agree.
    "rank": ("CASE rank " + " ".join(
        "WHEN '{}' THEN {}".format(rank, index)
        for index, rank in enumerate(events_store.RANKS)
    ) + " ELSE {} END".format(events_store.UNRANKED_ORDER)),
}


def add_minimum_rank_filter(where, params, rank):
    if rank == "unranked":
        where.append("rank IS NULL")
    elif rank == events_store.INELIGIBLE_RANK:
        # Ineligible is an access state, so the UI filters it as an exact value.
        where.append("rank = ?")
        params.append(events_store.INELIGIBLE_RANK)
    elif rank in events_store.RANKS:
        accepted_ranks = events_store.RANKS[:events_store.RANKS.index(rank) + 1]
        placeholders = ", ".join("?" for _ in accepted_ranks)
        where.append("rank IN ({})".format(placeholders))
        params.extend(accepted_ranks)


def add_source_filter(where, params, source_ids):
    if source_ids is None:
        return
    selected = [source_id for source_id in source_ids if source_id]
    if not selected:
        where.append("0")
        return
    placeholders = ", ".join("?" for _ in selected)
    where.append("source_id IN ({})".format(placeholders))
    params.extend(selected)


def json_value(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


class Repository:
    def __init__(self, root=ROOT, db_path=None):
        self.root = Path(root).resolve()
        self.db_path = str(Path(db_path).resolve()) if db_path else str(self.root / "social.db")
        self.catalog_path = self.root / "sources.json"
        self.event_ranking_criteria_path = (
            self.root / "EVENT_RANKING_CRITERIA.md"
        )
        self.event_ranking_criteria_template_path = (
            self.root / "EVENT_RANKING_CRITERIA.example.md"
        )
        self.workflow_path = (
            self.root / ".agents" / "skills" / "social-crawler" / "SKILL.md"
        )
        self.cache_path = (self.root / ".cache").resolve()
        self.calendar_refresh_lock = threading.Lock()
        self.calendar_refresh_cancel = threading.Event()
        self.calendar_refresh_state_lock = threading.Lock()

    def load_calendar_coverage(self):
        with closing(self.connect()) as con:
            row = con.execute(
                "SELECT snapshot_json FROM calendar_coverage WHERE id = 1").fetchone()
        return json_value(row["snapshot_json"], None) if row else None

    def save_calendar_coverage(self, snapshot):
        # Keep only day states; personal calendar contents stay out of the event store.
        with closing(self.connect()) as con, con:
            con.execute(
                "INSERT INTO calendar_coverage (id, snapshot_json) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET snapshot_json = excluded.snapshot_json",
                (json.dumps(snapshot),))

    def calendar_coverage(self, today=None):
        return coverage_view(self.load_calendar_coverage(), today or dt.date.today())

    def connect(self):
        return events_store.connect(self.db_path)

    def database_schema_status(self):
        return events_store.database_schema_status(self.db_path)

    def read_text(self, name):
        path = self._settings_path(name)
        if name == "event_ranking_criteria" and not path.exists():
            return self.event_ranking_criteria_template_path.read_text(
                encoding="utf-8")
        return path.read_text(encoding="utf-8")

    def save_text(self, name, content):
        path = self._settings_path(name)
        self._atomic_write(path, content.rstrip() + "\n")

    def _settings_path(self, name):
        paths = {
            "event_ranking_criteria": self.event_ranking_criteria_path,
            "workflow": self.workflow_path,
        }
        if name not in paths:
            raise KeyError(name)
        return paths[name]

    def load_catalog(self):
        return json.loads(self.catalog_path.read_text(encoding="utf-8"))

    def save_catalog(self, catalog):
        errors = events_store.validate_catalog(catalog)
        if errors:
            raise ValueError("\n".join(errors))
        body = json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
        self._atomic_write(self.catalog_path, body)

    @staticmethod
    def _atomic_write(path, content):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(str(temporary), str(path))

    def dashboard(self):
        catalog = self.load_catalog()
        sources = catalog.get("sources") or []
        with self.connect() as con:
            today = dt.date.today().isoformat()
            counts = dict(con.execute(
                """SELECT COUNT(*) AS events,
                          SUM(CASE WHEN substr(start, 1, 10) >= date(?) AND status != 'cancelled'
                                   THEN 1 ELSE 0 END) AS upcoming,
                          SUM(CASE WHEN substr(start, 1, 10) >= date(?) AND rank IS NULL
                                        AND status != 'cancelled'
                                   THEN 1 ELSE 0 END) AS unranked
                   FROM events""", (today, today)
            ).fetchone())
            counts["runs"] = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            counts["sources"] = len(sources)
            counts["enabled_sources"] = sum(s.get("enabled", True) for s in sources)
            recent_runs = self._list_runs(con, 6)
            health_rows = [self._health_row(row) for row in con.execute(
                "SELECT * FROM latest_source_health ORDER BY run_started_at DESC"
            ).fetchall()]
            issues = [row for row in health_rows if row["state"] in PROBLEM_STATES][:8]
            unranked = [self._event_row(row) for row in con.execute(
                """SELECT * FROM events
                   WHERE substr(start, 1, 10) >= date(?) AND rank IS NULL
                     AND status != 'cancelled'
                   ORDER BY start LIMIT 8""", (today,)
            ).fetchall()]
        return {
            "counts": counts, "recent_runs": recent_runs,
            "source_issues": issues, "unranked": unranked,
        }

    def list_runs(self, limit=100):
        with self.connect() as con:
            return self._list_runs(con, limit)

    def _list_runs(self, con, limit):
        rows = con.execute(
            """SELECT r.*,
                      COUNT(DISTINCT sr.source_id) AS source_count,
                      COUNT(DISTINCT d.id) AS discovery_count,
                      COUNT(DISTINCT CASE WHEN d.outcome = 'new' THEN d.id END) AS new_count,
                      COUNT(DISTINCT CASE WHEN d.outcome = 'rejected' THEN d.id END) AS rejected_count,
                      COUNT(DISTINCT CASE WHEN sr.state IN
                        ('empty_suspicious','parse_failed','validation_failed',
                         'fetch_failed','auth_required','blocked')
                        THEN sr.source_id END) AS issue_count
               FROM runs r
               LEFT JOIN source_runs sr ON sr.run_id = r.id
               LEFT JOIN discoveries d ON d.run_id = r.id
               GROUP BY r.id
               ORDER BY r.started_at DESC, r.id DESC LIMIT ?""", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id):
        with self.connect() as con:
            run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                return None
            run = dict(run)
            source_rows = [self._source_run_row(row) for row in con.execute(
                "SELECT * FROM source_runs WHERE run_id = ? ORDER BY source_id", (run_id,)
            ).fetchall()]
            discovery_rows = [self._discovery_row(row) for row in con.execute(
                """SELECT d.*, e.title AS event_title, e.start AS event_start
                   FROM discoveries d LEFT JOIN events e ON e.id = d.event_id
                   WHERE d.run_id = ? ORDER BY d.id""", (run_id,)
            ).fetchall()]
        totals = {
            "sources": len(source_rows),
            # Old completed runs can contain not_due rows. Do not count those
            # historical skips as checked sources.
            "checked": sum(
                row["state"] not in {"not_due", "pending"} for row in source_rows),
            "issues": sum(row["state"] in PROBLEM_STATES for row in source_rows),
            "discoveries": len(discovery_rows),
            "new": sum(row["outcome"] == "new" for row in discovery_rows),
            "updated": sum(row["outcome"] == "updated" for row in discovery_rows),
            "unchanged": sum(row["outcome"] == "unchanged" for row in discovery_rows),
            "duplicates": sum(row["outcome"] == "duplicate" for row in discovery_rows),
            "rejected": sum(row["outcome"] == "rejected" for row in discovery_rows),
            "staged": sum(row["outcome"] == "staged" for row in discovery_rows),
        }
        totals["existing"] = totals["updated"] + totals["unchanged"]
        return {"run": run, "sources": source_rows, "discoveries": discovery_rows, "totals": totals}

    def list_sources(self):
        catalog = self.load_catalog()
        catalog_created_on = catalog.get("generated", "")
        health = {}
        with self.connect() as con:
            for row in con.execute("SELECT * FROM latest_source_health").fetchall():
                health[row["source_id"]] = self._health_row(row)
        out = []
        for source in catalog.get("sources") or []:
            item = dict(source)
            item.setdefault("added_on", catalog_created_on)
            item["catalog_kind"] = "website"
            item["health"] = health.get(source.get("id"))
            out.append(item)
        inbox = catalog.get("inbox_sources") or {}
        for source in inbox.get("items") or []:
            item = dict(source)
            item.setdefault("added_on", catalog_created_on)
            item.setdefault("url", item.get("signup_url", ""))
            item.setdefault("priority", inbox.get("priority", 3))
            item.setdefault("geo", "Inbox")
            item.setdefault("parse_hint", item.get("note", ""))
            item.setdefault("retrieval_profile", inbox.get("retrieval_profile", ""))
            item.setdefault("enabled", True)
            item["catalog_kind"] = "inbox"
            item["health"] = health.get(source.get("id"))
            out.append(item)
        return out

    def get_source(self, source_id):
        catalog = self.load_catalog()
        source = next((dict(item) for item in catalog.get("sources") or []
                       if item.get("id") == source_id), None)
        catalog_kind = "website" if source else None
        if not source:
            inbox = catalog.get("inbox_sources") or {}
            source = next((dict(item) for item in inbox.get("items") or []
                           if item.get("id") == source_id), None)
            if source:
                catalog_kind = "inbox"
                source.setdefault("url", source.get("signup_url", ""))
                source.setdefault("priority", inbox.get("priority", 3))
                source.setdefault("geo", "Inbox")
                source.setdefault("parse_hint", source.get("note", ""))
                source.setdefault("retrieval_profile", inbox.get("retrieval_profile", ""))
                source.setdefault("enabled", True)
        with self.connect() as con:
            history = [self._source_run_row(row) for row in con.execute(
                """SELECT sr.*, r.seen_date, r.started_at AS run_started_at
                   FROM source_runs sr JOIN runs r ON r.id = sr.run_id
                   WHERE sr.source_id = ? ORDER BY r.started_at DESC LIMIT 30""",
                (source_id,),
            ).fetchall()]
            events = [self._event_row(row) for row in con.execute(
                "SELECT * FROM events WHERE source_id = ? ORDER BY start DESC LIMIT 50",
                (source_id,),
            ).fetchall()]
        if not source and not history and not events:
            return None
        if not source:
            source = {
                "id": source_id, "title": source_id, "url": "", "priority": 3,
                "geo": "Not in current catalog",
                "parse_hint": "This source appears in stored history but is not in the current catalog.",
                "retrieval_profile": history[0].get("recipe_version") if history else "unknown",
                "enabled": False, "disabled_reason": "Not in current catalog",
            }
            catalog_kind = "historical"
        source["catalog_kind"] = catalog_kind
        source["history"] = history
        source["events"] = events
        source["health"] = history[0] if history else None
        return source

    def upsert_source(self, values, original_id=None):
        catalog = self.load_catalog()
        sources = catalog.setdefault("sources", [])
        values = dict(values)
        if original_id:
            index = next((i for i, item in enumerate(sources)
                          if item.get("id") == original_id), None)
            if index is None:
                raise KeyError(original_id)
            if values["id"] != original_id and any(
                    item.get("id") == values["id"] for item in sources):
                raise ValueError("A source with this ID already exists.")
            # Keep the original date when an edit omits catalog metadata.
            values.setdefault(
                "added_on",
                sources[index].get("added_on") or catalog.get("generated")
                or dt.date.today().isoformat(),
            )
            sources[index] = values
        else:
            if any(item.get("id") == values["id"] for item in sources):
                raise ValueError("A source with this ID already exists.")
            values.setdefault("added_on", dt.date.today().isoformat())
            sources.append(values)
        self.save_catalog(catalog)

    def remove_catalog_sources(self, source_ids=(), remove_inbox=False,
                               profile_ids=()):
        """Remove explicitly selected catalog records in one validated write."""
        catalog = self.load_catalog()
        requested_sources = set(source_ids)
        sources = catalog.get("sources") or []
        known_sources = {source.get("id") for source in sources}
        missing_sources = requested_sources - known_sources
        if missing_sources:
            raise KeyError(", ".join(sorted(missing_sources)))

        catalog["sources"] = [
            source for source in sources
            if source.get("id") not in requested_sources
        ]
        removed_inbox_ids = []
        if remove_inbox:
            inbox = catalog.pop("inbox_sources", None) or {}
            removed_inbox_ids = [
                source.get("id") for source in inbox.get("items") or []
                if source.get("id")
            ]

        profiles = catalog.get("retrieval_profiles") or {}
        requested_profiles = set(profile_ids)
        missing_profiles = requested_profiles - set(profiles)
        if missing_profiles:
            raise KeyError(", ".join(sorted(missing_profiles)))
        referenced_profiles = {
            source.get("retrieval_profile")
            for source in catalog.get("sources") or []
            if source.get("retrieval_profile")
        }
        inbox = catalog.get("inbox_sources") or {}
        if inbox.get("retrieval_profile"):
            referenced_profiles.add(inbox["retrieval_profile"])
        still_used = requested_profiles & referenced_profiles
        if still_used:
            raise ValueError(
                "Cannot remove referenced retrieval profiles: {}".format(
                    ", ".join(sorted(still_used))))
        for profile_id in requested_profiles:
            del profiles[profile_id]

        self.save_catalog(catalog)
        return {
            "sources": sorted(requested_sources),
            "inbox_sources": removed_inbox_ids,
            "profiles": sorted(requested_profiles),
        }

    def list_events(self, query="", rank="", period="all", source_ids=None,
                    sort="start", direction="desc"):
        where = ["status = 'active'"]
        params = []
        if query:
            where.append("(title LIKE ? OR host LIKE ? OR venue LIKE ? OR neighborhood LIKE ?)")
            term = "%{}%".format(query)
            params.extend([term, term, term, term])
        add_minimum_rank_filter(where, params, rank)
        add_source_filter(where, params, source_ids)
        if period == "upcoming":
            where.append("substr(start, 1, 10) >= date('now', 'localtime')")
        elif period == "past":
            where.append("substr(start, 1, 10) < date('now', 'localtime')")
        clause = " WHERE " + " AND ".join(where) if where else ""
        sort_expression = EVENT_SORTS.get(sort, EVENT_SORTS["start"])
        direction = "ASC" if direction == "asc" else "DESC"
        order = "{} {}, start DESC, title COLLATE NOCASE ASC".format(
            sort_expression, direction)
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM events{} ORDER BY {} LIMIT 500".format(clause, order), params
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def calendar_events(self, start_date, end_date, rank="", source_ids=None):
        where = ["substr(start, 1, 10) >= date(?)",
                 "substr(start, 1, 10) <= date(?)",
                 "status = 'active'"]
        params = [start_date, end_date]
        add_minimum_rank_filter(where, params, rank)
        add_source_filter(where, params, source_ids)
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM events WHERE {} ORDER BY start, title COLLATE NOCASE"
                .format(" AND ".join(where)), params,
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def get_event(self, event_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not row:
                return None
            event = self._event_row(row)
            urls = [dict(item) for item in con.execute(
                """SELECT url, canonical_url, source_id, first_seen, last_seen
                   FROM event_urls WHERE event_id=?
                   ORDER BY first_seen, canonical_url""",
                (event_id,),
            ).fetchall()]
            discoveries = [self._discovery_row(item) for item in con.execute(
                """SELECT d.*, e.title AS event_title, e.start AS event_start
                   FROM discoveries d LEFT JOIN events e ON e.id = d.event_id
                   WHERE d.event_id = ? ORDER BY d.id DESC""", (event_id,)
            ).fetchall()]
        preferred = events_store.canon_url(event.get("url"))
        for item in urls:
            item["is_preferred"] = item["canonical_url"] == preferred
        event["urls"] = urls
        event["discoveries"] = discoveries
        return event

    def artifact_path(self, run_id, source_id, index):
        with self.connect() as con:
            row = con.execute(
                "SELECT artifacts_json FROM source_runs WHERE run_id=? AND source_id=?",
                (run_id, source_id),
            ).fetchone()
        if not row:
            return None, None
        artifacts = json_value(row["artifacts_json"], [])
        if index < 0 or index >= len(artifacts):
            return None, None
        artifact = artifacts[index]
        raw_path = artifact.get("path") if isinstance(artifact, dict) else artifact
        if not raw_path:
            return None, None
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if path != self.cache_path and self.cache_path not in path.parents:
            return None, None
        return path, artifact

    @staticmethod
    def _source_run_row(row):
        item = dict(row)
        item["existing_count"] = (
            item.get("updated_count", 0) + item.get("unchanged_count", 0)
        )
        item["rejection_counts"] = json_value(item.pop("rejection_counts_json", "{}"), {})
        item["artifacts"] = json_value(item.pop("artifacts_json", "[]"), [])
        item["work"] = json_value(item.pop("work_json", "{}"), {})
        return item

    @classmethod
    def _health_row(cls, row):
        return cls._source_run_row(row)

    @staticmethod
    def _event_row(row):
        item = dict(row)
        item["is_free"] = bool(item.get("is_free"))
        item["format_tags"] = json_value(item.pop("format_tags_json", "[]"), [])
        item["theme_emoji"] = event_theme_emoji(item)
        return item

    @staticmethod
    def _discovery_row(row):
        item = dict(row)
        item["raw"] = json_value(item.pop("raw_json", "{}"), {})
        item["display_title"] = (
            item.get("event_title") or item["raw"].get("title") or "Untitled candidate"
        )
        item["display_start"] = item.get("event_start") or item["raw"].get("start") \
            or item["raw"].get("start_datetime")
        return item
