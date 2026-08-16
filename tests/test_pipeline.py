import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

import events_store


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "primary": {"method": "http_json", "recipe": "test-v1"},
            "fallbacks": [],
            "required_fields": ["title", "start_datetime", "url"],
            "empty_signal": "an explicit empty list",
        }
        self.catalog = {
            "retrieval_profiles": {
                "test": self.profile,
                "gmail-api-v1": dict(self.profile),
            },
            "sources": [{
                "id": "source-one", "title": "One", "url": "https://example.com",
                "priority": 1, "geo": "NYC",
                "parse_hint": "test", "retrieval_profile": "test",
            }],
            "inbox_sources": {
                "priority": 1, "retrieval_profile": "gmail-api-v1",
                "query_template": "from:({senders})",
                "items": [{
                    "id": "mail-one", "title": "Mail", "sender_hint": "mail",
                }],
            },
        }
        self.seen = dt.date(2026, 8, 1)

    def test_full_run_selects_recent_healthy_source(self):
        latest = [{
            "source_id": "source-one", "state": "ok",
            "seen_date": "2026-07-30", "last_audited_at": "2026-07-30",
        }]
        plan = events_store.build_run_plan(self.catalog, latest, self.seen)
        self.assertEqual(plan["sources"][0]["state"], "pending")
        self.assertEqual(plan["sources"][0]["detail"], "selected for this run")
        self.assertFalse(plan["sources"][0]["audit"])
        self.assertEqual(plan["sources"][1]["state"], "blocked")
        self.assertEqual([row["source_id"] for row in plan["work"]], ["source-one"])

    def test_failure_and_first_check_request_an_audit(self):
        failed = [{
            "source_id": "source-one", "state": "parse_failed",
            "seen_date": "2026-07-31", "last_audited_at": "2026-07-31",
        }]
        plan = events_store.build_run_plan(self.catalog, failed, self.seen)
        self.assertEqual(plan["sources"][0]["state"], "pending")
        self.assertIn("retry", plan["sources"][0]["detail"])
        self.assertTrue(plan["sources"][0]["audit"])

        plan = events_store.build_run_plan(self.catalog, [], self.seen)
        self.assertTrue(plan["sources"][0]["audit"])
        self.assertEqual(plan["sources"][0]["detail"], "never checked")

    def test_manifest_validation_catches_incomplete_work(self):
        plan = events_store.build_run_plan(self.catalog, [], self.seen)
        errors = events_store.validate_crawl_payload(plan)
        self.assertTrue(any("still pending" in error for error in errors))

        plan["sources"][0]["state"] = "ok"
        plan["sources"].pop()
        errors = events_store.validate_crawl_payload(plan)
        self.assertTrue(any("missing manifest rows" in error for error in errors))

    def test_targeted_plan_forces_only_selected_source(self):
        latest = [{
            "source_id": "source-one", "state": "ok",
            "seen_date": "2026-07-30", "last_audited_at": "2026-07-30",
        }]
        plan = events_store.build_run_plan(
            self.catalog, latest, self.seen, only_source="source-one",
        )

        self.assertEqual(plan["run"]["manifest_source_ids"], ["source-one"])
        self.assertEqual([row["source_id"] for row in plan["sources"]], ["source-one"])
        self.assertEqual([row["source_id"] for row in plan["work"]], ["source-one"])
        self.assertEqual(plan["sources"][0]["state"], "pending")
        self.assertEqual(plan["sources"][0]["detail"], "targeted source crawl")

    def test_targeted_plan_rejects_unknown_and_disabled_sources(self):
        self.catalog["sources"].append({
            "id": "disabled-source", "title": "Disabled",
            "url": "https://example.com/disabled", "priority": 1,
            "geo": "NYC", "parse_hint": "test",
            "retrieval_profile": "test", "enabled": False,
            "disabled_reason": "inactive until its calendar returns",
        })

        with self.assertRaisesRegex(ValueError, "unknown --only-source"):
            events_store.build_run_plan(
                self.catalog, [], self.seen, only_source="missing-source",
            )
        with self.assertRaisesRegex(ValueError, "disabled --only-source"):
            events_store.build_run_plan(
                self.catalog, [], self.seen, only_source="disabled-source",
            )
    def test_targeted_inbox_plan_respects_connector_availability(self):
        blocked = events_store.build_run_plan(
            self.catalog, [], self.seen, only_source="mail-one",
        )
        available = events_store.build_run_plan(
            self.catalog, [], self.seen, mail_available=True,
            only_source="mail-one",
        )

        self.assertEqual(blocked["run"]["manifest_source_ids"], ["mail-one"])
        self.assertEqual(blocked["sources"][0]["state"], "blocked")
        self.assertEqual(blocked["work"], [])
        self.assertEqual(available["sources"][0]["state"], "pending")
        self.assertEqual(available["work"][0]["source_id"], "mail-one")

    def test_disabled_sources_are_preserved_but_not_planned(self):
        self.catalog["sources"].append({
            "id": "disabled-source", "title": "Disabled",
            "url": "https://example.com/disabled", "priority": 1,
            "geo": "NYC", "parse_hint": "test",
            "retrieval_profile": "test", "enabled": False,
            "disabled_reason": "inactive until its calendar returns",
        })
        plan = events_store.build_run_plan(self.catalog, [], self.seen)
        self.assertNotIn("disabled-source", plan["run"]["manifest_source_ids"])
        self.assertNotIn("disabled-source", [row["source_id"] for row in plan["work"]])

    def test_catalog_rejects_source_schedule_fields(self):
        self.catalog["sources"][0]["cadence"] = "weekly"
        self.catalog["retrieval_profiles"]["test"]["audit_cadence"] = "monthly"
        self.catalog["inbox_sources"]["items"][0]["cadence"] = "weekly"

        errors = events_store.validate_catalog(self.catalog)

        self.assertIn("source-one has obsolete cadence field", errors)
        self.assertIn("profile test has obsolete audit_cadence field", errors)
        self.assertIn("mail-one has obsolete cadence field", errors)


class RankingTests(unittest.TestCase):
    def test_url_canonicalization_keeps_event_ids_and_drops_tracking(self):
        canonical = events_store.canon_url(
            "https://www.astorwines.com/event.aspx?utm_source=email&eid=ABC-123"
        )

        self.assertEqual(
            canonical, "astorwines.com/event.aspx?eid=abc-123",
        )
        self.assertTrue(events_store.is_event_url(canonical))
        self.assertFalse(events_store.is_event_url(
            events_store.canon_url(
                "https://example.com/calendar?utm_source=email&view=month"
            )
        ))

    def test_recurring_titles_share_a_compact_group(self):
        events = [
            {"id": "a", "source_id": "rr", "host": "Reading Rhythms",
             "title": "Reading Rhythms Harlem: July 29th", "start": "2026-07-29T18:45:00"},
            {"id": "b", "source_id": "rr", "host": "Reading Rhythms",
             "title": "Reading Rhythms Harlem: August 12th", "start": "2026-08-12T18:45:00"},
        ]
        groups = events_store.grouped_rank_queue(events)
        self.assertEqual(len(groups), 1)
        self.assertEqual([row["id"] for row in groups[0]["occurrences"]], ["a", "b"])


class PipelineIntegrationTests(unittest.TestCase):
    def test_same_host_does_not_merge_simultaneous_different_venues(self):
        williamsburg = {
            "title": "Spider-Man", "host": "Nitehawk Cinema",
            "start": "2026-08-18T15:30:00-04:00",
            "venue": "Nitehawk Cinema Williamsburg",
            "address": "136 Metropolitan Ave., Brooklyn, NY",
        }
        prospect_park = {
            "title": "Spider-Man", "host": "Nitehawk Cinema",
            "start": "2026-08-18T16:00:00-04:00",
            "venue": "Nitehawk Cinema Prospect Park",
            "address": "188 Prospect Park West, Brooklyn, NY",
        }

        self.assertFalse(events_store.same_event_occurrence(
            williamsburg, prospect_park))

    def test_event_repair_deletes_only_validated_source_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            con = events_store.connect(database)
            with con:
                first = events_store.normalise_incoming({
                    "source_id": "nitehawk", "title": "Film One",
                    "start": "2026-08-20T19:00:00-04:00",
                    "url": "https://nitehawk.example/purchase/111",
                }, "2026-08-15")
                second = events_store.normalise_incoming({
                    "source_id": "other-source", "title": "Film Two",
                    "start": "2026-08-20T20:00:00-04:00",
                    "url": "https://other.example/event/222",
                }, "2026-08-15")
                events_store.upsert_event(con, first)
                events_store.upsert_event(con, second)
                events_store.ensure_run(con, "repair-run", "2026-08-15")
                events_store.upsert_source_run(con, "repair-run", {
                    "source_id": "nitehawk", "state": "ok",
                })
                events_store.record_discovery(
                    con, "repair-run", "nitehawk", first, "new", first["id"])
                with self.assertRaisesRegex(ValueError, "do not belong"):
                    events_store.invalidate_source_events_by_id(
                        con, "nitehawk", [second["id"]], "test repair")
                deleted, run_ids = events_store.invalidate_source_events_by_id(
                    con, "nitehawk", [first["id"]], "test repair")
            remaining = con.execute("SELECT id FROM events").fetchall()
            discovery = con.execute(
                "SELECT outcome, rejection_reason, event_id FROM discoveries"
            ).fetchone()
            source_run = con.execute(
                "SELECT state, rejected_count FROM source_runs "
                "WHERE run_id='repair-run' AND source_id='nitehawk'"
            ).fetchone()
            con.close()

            self.assertEqual(deleted, 1)
            self.assertEqual(run_ids, ["repair-run"])
            self.assertEqual([row["id"] for row in remaining], [second["id"]])
            self.assertEqual(tuple(discovery), ("rejected", "test repair", None))
            self.assertEqual(tuple(source_run), ("validation_failed", 1))

    def test_alias_repair_deletes_only_one_source_and_exact_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            con = events_store.connect(database)
            with con:
                event = events_store.normalise_incoming({
                    "source_id": "nitehawk", "title": "Film",
                    "start": "2026-08-20T19:00:00-04:00",
                    "url": "https://nitehawk.example/showtimes/film-night-8-20-26",
                }, "2026-08-02")
                events_store.upsert_event(con, event)
                events_store.upsert_event_alias(
                    con, event["id"], "https://nitehawk.example/purchase/111",
                    "nitehawk", "2026-08-15", "2026-08-15",
                )
                events_store.upsert_event_alias(
                    con, event["id"], "https://other.example/event/222",
                    "other-source", "2026-08-15", "2026-08-15",
                )
                deleted = events_store.delete_source_aliases_added_on(
                    con, "nitehawk", "2026-08-15")
            aliases = con.execute(
                "SELECT source_id, first_seen FROM event_urls ORDER BY source_id, first_seen"
            ).fetchall()
            con.close()

            self.assertEqual(deleted, 1)
            self.assertEqual([tuple(row) for row in aliases], [
                ("nitehawk", "2026-08-02"),
                ("other-source", "2026-08-15"),
            ])

    def test_same_source_can_clear_an_incorrect_age_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            original = {
                "source_id": "out-there-nyc", "title": "Aug 21-23 Trip",
                "start": "2026-08-21T16:00:00-04:00",
                "url": "https://outthere.nyc/events/aug-21-23-trip-2026-08-21",
                "explicit_age_min": 21, "explicit_age_max": 23,
            }
            corrected = dict(original)
            corrected.update({"explicit_age_min": None, "explicit_age_max": None})
            con = events_store.connect(database)
            with con:
                stored = events_store.normalise_incoming(original, "2026-08-15")
                events_store.upsert_event(con, stored)
                merged, outcome, reason = events_store.merge_one(
                    con, corrected, "2026-08-16")
            con.close()

            self.assertEqual(outcome, "updated")
            self.assertIsNone(reason)
            self.assertIsNone(merged["explicit_age_min"])
            self.assertIsNone(merged["explicit_age_max"])

    def test_signup_url_alias_matches_an_existing_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            original = {
                "source_id": "eventbrite", "title": "Speakeasy Speed Dating",
                "host": "NY Singles", "start": "2026-08-20T19:00:00-04:00",
                "url": "https://eventbrite.com/e/speakeasy-speed-dating-123",
                "venue": "Blind Barber", "address": "339 E 10th St",
            }
            aggregator = {
                "source_id": "out-there-nyc", "title": "A different title",
                "host": "Aggregator", "start": "2026-08-20T19:00:00-04:00",
                "url": "https://outthere.nyc/events/speakeasy-2026-08-20",
                "signup_url": original["url"],
            }
            con = events_store.connect(database)
            with con:
                stored = events_store.normalise_incoming(original, "2026-08-01")
                events_store.upsert_event(con, stored)
                merged, outcome, reason = events_store.merge_one(
                    con, aggregator, "2026-08-15")
            aliases = events_store.get_event_urls(con, stored["id"])
            event_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            con.close()

            self.assertEqual(merged["id"], stored["id"])
            self.assertEqual(outcome, "updated")
            self.assertIsNone(reason)
            self.assertEqual(event_count, 1)
            self.assertEqual({item["url"] for item in aliases}, {
                original["url"], aggregator["url"],
            })

    def test_new_url_alias_matches_an_existing_event_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            original = {
                "source_id": "venue-calendar", "title": "A social dinner",
                "host": "Dinner Host", "start": "2026-08-20T19:00:00-04:00",
                "url": "https://venue.example/events/social-dinner",
                "venue": "The Room", "address": "1 Main Street",
            }
            aggregator = dict(original)
            aggregator.update({
                "source_id": "event-aggregator",
                "url": "https://aggregator.example/event/98765",
            })
            con = events_store.connect(database)
            with con:
                stored = events_store.normalise_incoming(original, "2026-08-01")
                events_store.upsert_event(con, stored)
                merged, outcome, reason = events_store.merge_one(
                    con, aggregator, "2026-08-15")

            aliases = events_store.get_event_urls(con, stored["id"])
            event_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            preferred_url = con.execute(
                "SELECT url FROM events WHERE id=?", (stored["id"],)
            ).fetchone()[0]
            dedup_key = con.execute(
                "SELECT dedup_key FROM events WHERE id=?", (stored["id"],)
            ).fetchone()[0]
            con.close()

            self.assertEqual(merged["id"], stored["id"])
            self.assertEqual(outcome, "updated")
            self.assertIsNone(reason)
            self.assertEqual(event_count, 1)
            self.assertEqual(preferred_url, original["url"])
            self.assertEqual(dedup_key, stored["dedup_key"])
            self.assertEqual({item["url"] for item in aliases}, {
                original["url"], aggregator["url"],
            })

    def test_same_title_and_time_do_not_merge_without_matching_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            first = {
                "source_id": "one", "title": "Community Mixer",
                "host": "North Club", "start": "2026-08-20T19:00:00-04:00",
                "url": "https://one.example/events/community-mixer",
                "venue": "North Hall", "address": "1 North Street",
            }
            second = {
                "source_id": "two", "title": "Community Mixer",
                "host": "South Club", "start": "2026-08-20T19:00:00-04:00",
                "url": "https://two.example/events/community-mixer",
                "venue": "South Hall", "address": "99 South Street",
            }
            con = events_store.connect(database)
            with con:
                events_store.upsert_event(
                    con, events_store.normalise_incoming(first, "2026-08-01"))
                merged, outcome, _ = events_store.merge_one(
                    con, second, "2026-08-15")
            event_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            con.close()

            self.assertEqual(outcome, "new")
            self.assertNotEqual(
                merged["id"], events_store.normalise_incoming(first, "2026-08-01")["id"])
            self.assertEqual(event_count, 2)

    def test_two_urls_for_one_occurrence_are_duplicates_in_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            con = events_store.connect(database)
            plan = {
                "run": {
                    "id": "alias-run", "seen_date": "2026-08-15",
                    "started_at": "2026-08-15T09:00:00-04:00",
                },
                "sources": [{
                    "source_id": "aggregator", "state": "pending",
                    "method": "browser_dom",
                }],
                "work": [{"source_id": "aggregator"}],
            }
            first = {
                "title": "Open Table Night", "host": "Common House",
                "start": "2026-08-20T19:00:00-04:00", "venue": "Common House",
                "url": "https://one.example/events/open-table-night",
            }
            second = dict(first, url="https://two.example/e/12345")
            with con:
                events_store.persist_run_plan(con, plan)
                events_store.stage_source_result(con, "alias-run", "aggregator", {
                    "source": {"state": "ok", "method": "browser_dom"},
                    "events": [first, second], "rejections": [],
                })
                totals = events_store.finalize_database_run(con, "alias-run")

            outcomes = con.execute(
                "SELECT outcome, event_id FROM discoveries ORDER BY id"
            ).fetchall()
            event_id = outcomes[0][1]
            aliases = events_store.get_event_urls(con, event_id)
            event_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            con.close()

            self.assertEqual([row[0] for row in outcomes], ["new", "duplicate"])
            self.assertEqual(outcomes[0][1], outcomes[1][1])
            self.assertEqual(totals["new"], 1)
            self.assertEqual(totals["duplicates"], 1)
            self.assertEqual(event_count, 1)
            self.assertEqual(len(aliases), 2)

    def test_database_backed_run_stages_and_finalizes_without_crawl_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            env = dict(os.environ, SOCIAL_DB=database)
            tool = os.path.join(TOOLS, "events_store.py")
            run_id = "database-run"

            planned = subprocess.run(
                [sys.executable, tool, "plan-run", "--date", "2026-08-15",
                 "--only-source", "57-nyc", "--run-id", run_id],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan = json.loads(planned.stdout)
            self.assertEqual(plan["run"]["id"], run_id)
            self.assertEqual([row["source_id"] for row in plan["work"]], ["57-nyc"])
            self.assertEqual(events_store.load_latest_source_health(database), [])

            pending_finalize = subprocess.run(
                [sys.executable, tool, "finalize-run", "--run-id", run_id],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(pending_finalize.returncode, 1)
            self.assertIn("pending sources: 57-nyc", pending_finalize.stderr)

            work = subprocess.run(
                [sys.executable, tool, "run-work", "--run-id", run_id],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(work.returncode, 0, work.stderr)
            self.assertEqual(
                [row["source_id"] for row in json.loads(work.stdout)["work"]],
                ["57-nyc"],
            )

            source_result = {
                "source": {
                    "state": "ok", "method": "browser_dom",
                    "recipe_version": "calendar-dom-v1",
                    "artifacts": [".cache/evidence.md"],
                    "detail": "targeted source crawl",
                },
                "events": [{
                    "title": "A new social workshop",
                    "start_datetime": "2026-08-20T19:00:00-04:00",
                    "url": "https://example.com/events/new-workshop",
                    "venue_name": "Room",
                }],
                "rejections": [{
                    "reason": "cancelled_event",
                    "raw": {
                        "title": "A cancelled show",
                        "start_datetime": "2026-08-21T19:00:00-04:00",
                        "url": "https://example.com/events/cancelled-show",
                    },
                }],
            }
            recorded = subprocess.run(
                [sys.executable, tool, "record-source", "--run-id", run_id,
                 "--source-id", "57-nyc"],
                cwd=ROOT, env=env, text=True, input=json.dumps(source_result),
                capture_output=True,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertIn("1 events, 1 rejections", recorded.stdout)

            con = sqlite3.connect(database)
            staged = con.execute(
                "SELECT outcome FROM discoveries WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            source_before = con.execute(
                "SELECT state, work_json FROM source_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            con.close()
            self.assertEqual(staged, [("staged",), ("rejected",)])
            self.assertEqual(source_before[0], "ok")
            self.assertEqual(json.loads(source_before[1])["source_id"], "57-nyc")

            finalized = subprocess.run(
                [sys.executable, tool, "finalize-run", "--run-id", run_id],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            self.assertIn("+1 new, 0 existing", finalized.stdout)
            self.assertIn("0 duplicates, 1 rejected", finalized.stdout)

            con = sqlite3.connect(database)
            run_state = con.execute(
                "SELECT state FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            outcomes = con.execute(
                "SELECT outcome FROM discoveries WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            counts = con.execute(
                "SELECT found_count, parsed_count, qualified_count, rejected_count, "
                "new_count FROM source_runs WHERE run_id=?", (run_id,),
            ).fetchone()
            event_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            con.close()
            self.assertEqual(run_state, "completed")
            self.assertEqual(outcomes, [("new",), ("rejected",)])
            self.assertEqual(counts, (2, 1, 1, 1, 1))
            self.assertEqual(event_count, 1)
            self.assertEqual(
                events_store.load_latest_source_health(database)[0]["source_id"],
                "57-nyc",
            )

    def test_merge_derives_counts_and_completes_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            crawl = os.path.join(tmp, "crawl.json")
            run_id = "test-run"
            event = {
                "source_id": "source-one", "title": "A social dinner",
                "host": "Host", "start": "2026-08-02T19:00:00-04:00",
                "url": "https://example.com/events/dinner", "venue": "Room",
            }
            with open(crawl, "w", encoding="utf-8") as fh:
                json.dump({
                    "run": {
                        "id": run_id, "seen_date": "2026-08-01",
                        "manifest_source_ids": ["source-one"],
                    },
                    "sources": [{"source_id": "source-one", "state": "ok",
                                 "method": "http_json"}],
                    "events": [event], "rejections": [],
                }, fh)

            env = dict(os.environ, SOCIAL_DB=database)
            merged = subprocess.run(
                [sys.executable, os.path.join(TOOLS, "events_store.py"),
                 "merge", "--input", crawl],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(merged.returncode, 0, merged.stderr)

            con = sqlite3.connect(database)
            journal_mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            source = con.execute(
                "SELECT found_count, parsed_count, qualified_count, new_count "
                "FROM source_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            self.assertEqual(journal_mode, "delete")
            self.assertEqual(source, (1, 1, 1, 1))
            event_id = con.execute("SELECT id FROM events").fetchone()[0]
            run_state = con.execute(
                "SELECT state FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            con.close()
            self.assertEqual(run_state, "completed")
            self.assertTrue(event_id)

    def test_merge_reports_existing_and_refreshes_last_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            crawl = os.path.join(tmp, "crawl.json")
            existing_event = {
                "source_id": "source-one", "title": "A social dinner",
                "host": "Host", "start": "2026-08-20T19:00:00-04:00",
                "url": "https://example.com/events/dinner", "venue": "Room",
            }
            new_event = {
                "source_id": "source-one", "title": "A new workshop",
                "host": "Host", "start": "2026-08-21T19:00:00-04:00",
                "url": "https://example.com/events/workshop", "venue": "Room",
            }
            con = events_store.connect(database)
            with con:
                events_store.upsert_event(
                    con, events_store.normalise_incoming(existing_event, "2026-08-01")
                )
            con.close()
            with open(crawl, "w", encoding="utf-8") as fh:
                json.dump({
                    "run": {
                        "id": "targeted-run", "seen_date": "2026-08-15",
                        "manifest_source_ids": ["source-one"],
                    },
                    "sources": [{
                        "source_id": "source-one", "state": "ok",
                        "method": "http_json",
                    }],
                    "events": [existing_event, existing_event, new_event],
                    "rejections": [],
                }, fh)

            env = dict(os.environ, SOCIAL_DB=database)
            merged = subprocess.run(
                [sys.executable, os.path.join(TOOLS, "events_store.py"),
                 "merge", "--input", crawl],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )

            self.assertEqual(merged.returncode, 0, merged.stderr)
            self.assertIn(
                "+1 new, 1 existing (0 updated, 1 unchanged), 1 duplicates",
                merged.stdout,
            )
            con = sqlite3.connect(database)
            last_seen = con.execute(
                "SELECT last_seen FROM events WHERE title='A social dinner'"
            ).fetchone()[0]
            counts = con.execute(
                "SELECT new_count, updated_count, unchanged_count, duplicate_count "
                "FROM source_runs WHERE run_id='targeted-run'"
            ).fetchone()
            event_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            con.close()
            self.assertEqual(last_seen, "2026-08-15")
            self.assertEqual(counts, (1, 0, 1, 1))
            self.assertEqual(event_count, 2)

    def test_schema_migration_adds_current_fields_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "social.db")
            con = sqlite3.connect(database)
            con.executescript("""
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, seen_date TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT,
                    state TEXT NOT NULL DEFAULT 'running', catalog_hash TEXT,
                    error TEXT
                );
                CREATE TABLE events (
                    id TEXT PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE,
                    url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
                    host TEXT NOT NULL DEFAULT '', start TEXT NOT NULL, end TEXT,
                    venue TEXT NOT NULL DEFAULT '', neighborhood TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '', price TEXT NOT NULL DEFAULT '',
                    is_free INTEGER NOT NULL DEFAULT 0, source_id TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', fit_note TEXT NOT NULL DEFAULT '',
                    rank TEXT, format_tags_json TEXT NOT NULL DEFAULT '[]',
                    capacity_flag TEXT, catch TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active', first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL, ranked_on TEXT
                );
                INSERT INTO runs VALUES
                    ('old-run', '2026-08-01', '2026-08-01T09:00:00', NULL,
                     'completed', NULL, NULL);
                INSERT INTO events VALUES
                    ('event-one', 'key-one', 'https://example.com/events/kept-event', 'Kept event', '',
                     '2026-08-02T19:00:00', NULL, '', '', '', '', 0, '', '', '',
                     NULL, '[]', NULL, '', 'active', '2026-08-01', '2026-08-01',
                     NULL);
            """)
            con.close()

            migrated = events_store.connect(database)
            event_columns = {row[1] for row in migrated.execute("PRAGMA table_info(events)")}
            source_run_columns = {
                row[1] for row in migrated.execute("PRAGMA table_info(source_runs)")
            }
            title = migrated.execute(
                "SELECT title FROM events WHERE id='event-one'"
            ).fetchone()[0]
            alias = migrated.execute(
                "SELECT url FROM event_urls WHERE event_id='event-one'"
            ).fetchone()[0]
            schema_version = migrated.execute("PRAGMA user_version").fetchone()[0]
            migrated.close()

            self.assertEqual(title, "Kept event")
            self.assertEqual(alias, "https://example.com/events/kept-event")
            self.assertEqual(schema_version, events_store.SCHEMA_VERSION)
            self.assertIn("work_json", source_run_columns)
            self.assertIn("signup_url", event_columns)
            self.assertIn("explicit_age_min", event_columns)
            self.assertIn("explicit_age_max", event_columns)


if __name__ == "__main__":
    unittest.main()
