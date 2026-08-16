"""Connect procedural adapters to the existing database-backed run pipeline."""

from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from crawler.artifacts import ArtifactRecorder
from crawler.contracts import (
    CrawlerError, ParseError, RobotsDenied, source_result,
    validate_adapter_result,
)
from crawler.http import HttpClient
from crawler.registry import get_adapter


ROOT = Path(__file__).resolve().parents[1]


def load_catalog(path=None):
    path = Path(path or ROOT / "sources.json")
    return json.loads(path.read_text(encoding="utf-8"))


def find_source(catalog, source_id):
    return next((source for source in catalog.get("sources") or []
                 if source.get("id") == source_id), None)


def execute_source(source, catalog, seen_date, client=None, artifact_root=None):
    adapter_id = source.get("adapter")
    if not adapter_id:
        raise ValueError("source {} has no procedural adapter".format(source["id"]))
    adapter_class = get_adapter(adapter_id)
    artifact_root = Path(artifact_root or os.environ.get(
        "SOCIAL_CACHE", ROOT / ".cache"))
    artifacts = ArtifactRecorder(artifact_root)
    adapter = adapter_class(client or HttpClient(), artifacts=artifacts)
    run_config = catalog.get("run_config") or {}
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        payload = adapter.crawl(
            source=source,
            seen_date=seen_date,
            lookahead_days=int(run_config.get("lookahead_days", 10)),
            timezone=run_config.get("timezone") or "America/New_York",
        )
        return validate_adapter_result(payload)
    except CrawlerError as error:
        finished_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        state = "blocked" if isinstance(error, RobotsDenied) else (
            "parse_failed" if isinstance(error, ParseError) else "fetch_failed")
        return source_result(
            state=state,
            method="python_adapter",
            recipe_version=getattr(adapter, "version", adapter_id),
            started_at=started_at,
            finished_at=finished_at,
            artifacts=artifacts.items,
            detail="Procedural adapter failed.",
            error=str(error),
        )


def run_planned_source(run_id, source_id, database, client=None, cache_root=None,
                       replace=False):
    # Imported here so the standalone crawler package does not own database state.
    from tools import events_store

    catalog = load_catalog()
    source = find_source(catalog, source_id)
    if source is None:
        raise ValueError("unknown catalog source: {}".format(source_id))
    con = events_store.connect(database)
    try:
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        planned = con.execute(
            "SELECT * FROM source_runs WHERE run_id=? AND source_id=?",
            (run_id, source_id),
        ).fetchone()
        if not run:
            raise ValueError("unknown run id: {}".format(run_id))
        if run["state"] != "running":
            raise ValueError("run is not active: {}".format(run_id))
        if not planned:
            raise ValueError("source {} is not planned for run {}".format(
                source_id, run_id))
        if planned["state"] != "pending" and not replace:
            raise ValueError("source {} is not pending in run {}".format(
                source_id, run_id))
        seen_date = dt.date.fromisoformat(run["seen_date"])
    finally:
        con.close()

    target_cache = Path(cache_root or os.environ.get("SOCIAL_CACHE", ROOT / ".cache")) \
        / run_id / source_id
    payload = execute_source(
        source, catalog, seen_date, client=client, artifact_root=target_cache)
    con = events_store.connect(database)
    try:
        with con:
            event_count, rejection_count = events_store.stage_source_result(
                con, run_id, source_id, payload)
    finally:
        con.close()
    return payload, event_count, rejection_count


def run_planned_adapters(run_id, database, workers=4, cache_root=None,
                         catalog=None, execute=None):
    """Retrieve pending adapters in parallel and stage them on this thread."""
    from tools import events_store

    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")

    catalog = catalog or load_catalog()
    execute = execute or execute_source
    sources_by_id = {
        source["id"]: source for source in catalog.get("sources") or []
    }

    con = events_store.connect(database)
    try:
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError("unknown run id: {}".format(run_id))
        if run["state"] != "running":
            raise ValueError("run is not active: {}".format(run_id))
        rows = con.execute(
            "SELECT source_id, work_json FROM source_runs "
            "WHERE run_id=? AND state='pending' ORDER BY source_id",
            (run_id,),
        ).fetchall()
        seen_date = dt.date.fromisoformat(run["seen_date"])
    finally:
        con.close()

    jobs = []
    skipped = []
    for row in rows:
        work = json.loads(row["work_json"] or "{}")
        source_id = row["source_id"]
        if not work.get("adapter"):
            skipped.append(source_id)
            continue
        source = sources_by_id.get(source_id)
        if source is None:
            raise ValueError("unknown catalog source: {}".format(source_id))
        if not source.get("adapter"):
            raise ValueError("source {} no longer has an adapter".format(source_id))
        jobs.append((source_id, dict(source)))

    summary = {"results": [], "skipped": skipped, "errors": []}
    if not jobs:
        return summary

    target_cache = Path(cache_root or os.environ.get(
        "SOCIAL_CACHE", ROOT / ".cache")) / run_id

    def retrieve(source_id, source):
        payload = execute(
            source,
            catalog,
            seen_date,
            artifact_root=target_cache / source_id,
        )
        return source_id, payload

    # Futures retrieve concurrently. Reading them in planned source order keeps
    # discovery insertion and later deduplication deterministic.
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        futures = [(source_id, pool.submit(retrieve, source_id, source))
                   for source_id, source in jobs]
        con = events_store.connect(database)
        try:
            for source_id, future in futures:
                try:
                    completed_source_id, payload = future.result()
                    with con:
                        event_count, rejection_count = \
                            events_store.stage_source_result(
                                con, run_id, completed_source_id, payload)
                except Exception as error:  # Keep other planned sources moving.
                    summary["errors"].append({
                        "source_id": source_id,
                        "error": str(error),
                    })
                    continue
                summary["results"].append({
                    "source_id": completed_source_id,
                    "state": payload["source"]["state"],
                    "event_count": event_count,
                    "rejection_count": rejection_count,
                    "error": payload["source"].get("error"),
                })
        finally:
            con.close()
    return summary
