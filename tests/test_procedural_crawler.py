import datetime as dt
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from crawler.adapters.out_there import OutThereAdapter, PARSER_VERSION, _age_range
from crawler.contracts import HttpResponse, source_result
from crawler.runner import run_planned_adapters, run_planned_source
from tools import events_store


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEED = "https://outthere.nyc/?orientation=straight"
PAGE_2 = "https://outthere.nyc/?orientation=straight&page=2"
EVENT_1 = "https://outthere.nyc/events/speakeasy-speed-dating-2026-08-15"
EVENT_2 = "https://outthere.nyc/events/free-singles-arcade-2026-08-20"
EVENT_3 = "https://outthere.nyc/events/late-boundary-event-2026-08-26"


class FakeHttpClient:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        body = self.pages[url].read_bytes() if isinstance(self.pages[url], Path) \
            else self.pages[url].encode("utf-8")
        return HttpResponse(
            url=url,
            status=200,
            body=body,
            headers={"content-type": "text/html; charset=utf-8"},
            fetched_at="2026-08-15T12:00:00-04:00",
        )


def fixture_client():
    return FakeHttpClient({
        SEED: FIXTURES / "out_there_listing_1.html",
        PAGE_2: FIXTURES / "out_there_listing_2.html",
        EVENT_1: FIXTURES / "out_there_event_speed_dating.html",
        EVENT_2: FIXTURES / "out_there_event_arcade.html",
        EVENT_3: FIXTURES / "out_there_event_boundary.html",
    })


class OutThereAdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "out-there-nyc",
            "url": SEED,
            "adapter": "out_there",
        }

    def test_adapter_paginates_filters_and_extracts_factual_fields(self):
        client = fixture_client()
        with tempfile.TemporaryDirectory() as tmp:
            from crawler.artifacts import ArtifactRecorder
            adapter = OutThereAdapter(client, ArtifactRecorder(tmp))
            result = adapter.crawl(
                self.source,
                seen_date=dt.date(2026, 8, 15),
                lookahead_days=10,
                timezone="America/New_York",
            )

            self.assertEqual(result["source"]["state"], "ok")
            self.assertEqual(result["source"]["method"], "python_adapter")
            self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
            self.assertEqual(len(result["events"]), 3)
            self.assertEqual(len(result["rejections"]), 1)
            self.assertEqual(result["rejections"][0]["reason"], "outside_date_window")
            self.assertEqual(client.requests, [SEED, PAGE_2, EVENT_1, EVENT_2, EVENT_3])
            self.assertNotIn(
                "https://outthere.nyc/events/future-mixer-2026-08-30",
                client.requests,
            )
            self.assertEqual(len(result["source"]["artifacts"]), 2)
            self.assertTrue(all(Path(item["path"]).exists()
                                for item in result["source"]["artifacts"]))

        speed = result["events"][0]
        self.assertEqual(speed["start"], "2026-08-15T17:30:00-04:00")
        self.assertEqual(speed["end"], "2026-08-15T19:30:00-04:00")
        self.assertEqual(speed["explicit_age_min"], 30)
        self.assertEqual(speed["explicit_age_max"], 49)
        self.assertEqual(speed["neighborhood"], "Alphabet City")
        self.assertEqual(speed["price"], "$14.99")
        self.assertEqual(speed["signup_url"], "https://tickets.example/speakeasy")
        self.assertEqual(
            speed["source_event_id"], "cf12ce8f-49a5-5bcd-aa96-a3227b7aace7")
        self.assertIn("Guests rotate through short conversations", speed["description"])
        self.assertEqual(len(speed["content_hash"]), 64)

        arcade = result["events"][1]
        self.assertEqual(arcade["start"], "2026-08-20T19:00:00-04:00")
        self.assertTrue(arcade["is_free"])
        self.assertEqual(arcade["explicit_age_label"], "All ages")

        boundary = result["events"][2]
        self.assertEqual(boundary["start"], "2026-08-25T23:00:00-04:00")
        self.assertEqual(boundary["explicit_age_min"], 21)
        self.assertIsNone(boundary["explicit_age_max"])
        self.assertEqual(boundary["price"], "$20")

    def test_missing_event_json_ld_is_a_loud_partial_failure(self):
        client = fixture_client()
        client.pages[EVENT_1] = "<html><body>No structured event</body></html>"
        with tempfile.TemporaryDirectory() as tmp:
            from crawler.artifacts import ArtifactRecorder
            result = OutThereAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source,
                seen_date=dt.date(2026, 8, 15),
                lookahead_days=10,
                timezone="America/New_York",
            )
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 2)
        self.assertTrue(any(item["reason"] == "detail_parse_failed"
                            for item in result["rejections"]))
        self.assertIn("no Event JSON-LD", result["source"]["error"])

    def test_all_ages_header_wins_over_a_date_range_in_the_title(self):
        self.assertEqual(
            _age_range(
                "Aug 21-23 Trip\nAll ages\nDate & Time\nFriday, August 21",
                "Aug 21-23 Trip",
            ),
            (None, None, "All ages"),
        )


class ProceduralPipelineTests(unittest.TestCase):
    def test_run_adapters_retrieves_in_parallel_and_stages_on_main_thread(self):
        catalog = {
            "run_config": {
                "lookahead_days": 10,
                "timezone": "America/New_York",
            },
            "retrieval_profiles": {
                "test": {
                    "primary": {"method": "http", "recipe": "test-v1"},
                    "fallbacks": [],
                    "required_fields": ["title", "start"],
                    "empty_signal": "No events",
                },
            },
            "sources": [
                {
                    "id": "alpha", "title": "Alpha", "url": "https://alpha.test",
                    "retrieval_profile": "test", "adapter": "fake",
                },
                {
                    "id": "beta", "title": "Beta", "url": "https://beta.test",
                    "retrieval_profile": "test", "adapter": "fake",
                },
                {
                    "id": "manual", "title": "Manual",
                    "url": "https://manual.test", "retrieval_profile": "test",
                },
            ],
        }
        plan = events_store.build_run_plan(
            catalog, latest_rows=[], seen=dt.date(2026, 8, 15),
            run_id="parallel-run",
        )
        barrier = threading.Barrier(2)
        beta_finished = threading.Event()
        completed = []
        worker_threads = []
        stage_threads = []
        stage_order = []
        main_thread = threading.get_ident()
        stamp = "2026-08-15T12:00:00-04:00"

        def fake_execute(source, _catalog, _seen_date, artifact_root=None):
            self.assertEqual(Path(artifact_root).name, source["id"])
            worker_threads.append(threading.get_ident())
            barrier.wait(timeout=2)
            if source["id"] == "beta":
                completed.append("beta")
                beta_finished.set()
            else:
                self.assertTrue(beta_finished.wait(timeout=2))
                completed.append("alpha")
            return source_result(
                state="empty_verified",
                method="python_adapter",
                recipe_version="fake-v1",
                started_at=stamp,
                finished_at=stamp,
                detail="No events.",
            )

        original_stage = events_store.stage_source_result

        def recording_stage(con, run_id, source_id, payload):
            stage_threads.append(threading.get_ident())
            stage_order.append(source_id)
            return original_stage(con, run_id, source_id, payload)

        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            con = events_store.connect(database)
            with con:
                events_store.persist_run_plan(con, plan)
            con.close()

            with mock.patch.object(
                    events_store, "stage_source_result", side_effect=recording_stage):
                summary = run_planned_adapters(
                    "parallel-run",
                    database=database,
                    workers=2,
                    cache_root=os.path.join(tmp, "cache"),
                    catalog=catalog,
                    execute=fake_execute,
                )

            con = events_store.connect(database)
            states = dict(con.execute(
                "SELECT source_id, state FROM source_runs "
                "WHERE run_id='parallel-run'"
            ).fetchall())
            con.close()

        self.assertEqual(completed, ["beta", "alpha"], summary)
        self.assertEqual(stage_order, ["alpha", "beta"])
        self.assertTrue(all(thread != main_thread for thread in worker_threads))
        self.assertEqual(stage_threads, [main_thread, main_thread])
        self.assertEqual(
            [result["source_id"] for result in summary["results"]],
            ["alpha", "beta"],
        )
        self.assertEqual(summary["skipped"], ["manual"])
        self.assertEqual(summary["errors"], [])
        self.assertEqual(states, {
            "alpha": "empty_verified",
            "beta": "empty_verified",
            "manual": "pending",
        })

    def test_out_there_runs_through_existing_stage_and_finalize_contract(self):
        catalog = json.loads((ROOT / "sources.example.json").read_text(encoding="utf-8"))
        plan = events_store.build_run_plan(
            catalog,
            latest_rows=[],
            seen=dt.date(2026, 8, 15),
            run_id="procedural-run",
            only_source="out-there-nyc",
        )
        self.assertEqual(plan["work"][0]["adapter"], "out_there")

        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            con = events_store.connect(database)
            with con:
                events_store.persist_run_plan(con, plan)
            con.close()

            payload, event_count, rejection_count = run_planned_source(
                "procedural-run",
                "out-there-nyc",
                database=database,
                client=fixture_client(),
                cache_root=os.path.join(tmp, "cache"),
            )
            self.assertEqual(payload["source"]["state"], "ok")
            self.assertEqual((event_count, rejection_count), (3, 1))

            payload, event_count, rejection_count = run_planned_source(
                "procedural-run",
                "out-there-nyc",
                database=database,
                client=fixture_client(),
                cache_root=os.path.join(tmp, "cache"),
                replace=True,
            )
            self.assertEqual((event_count, rejection_count), (3, 1))

            con = events_store.connect(database)
            with con:
                totals = events_store.finalize_database_run(con, "procedural-run")
            stored = con.execute(
                "SELECT signup_url, explicit_age_min, explicit_age_max, rank "
                "FROM events WHERE title=?",
                ("30s & 40s Speakeasy Speed Dating",),
            ).fetchone()
            source_run = con.execute(
                "SELECT method, recipe_version, state, found_count, new_count, "
                "rejected_count FROM source_runs WHERE run_id='procedural-run'"
            ).fetchone()
            raw = json.loads(con.execute(
                "SELECT raw_json FROM discoveries WHERE outcome='new' ORDER BY id LIMIT 1"
            ).fetchone()[0])
            con.close()

        self.assertEqual(totals["new"], 3)
        self.assertEqual(totals["rejected"], 1)
        self.assertEqual(tuple(stored), (
            "https://tickets.example/speakeasy", 30, 49, None))
        self.assertEqual(tuple(source_run), (
            "python_adapter", PARSER_VERSION, "ok", 4, 3, 1))
        self.assertEqual(raw["parser_version"], PARSER_VERSION)
        self.assertEqual(len(raw["content_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
