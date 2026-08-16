import datetime as dt
import tempfile
import unittest
from unittest.mock import patch

from crawler.contracts import ParseError, source_result
from crawler.runner import execute_source


NOW = "2026-08-16T12:00:00-04:00"


def event():
    return {
        "title": "Conversation Night",
        "start": "2026-08-20T19:00:00-04:00",
        "end": None,
        "url": "https://example.com/events/conversation-night",
        "signup_url": "https://example.com/events/conversation-night/register",
        "host": "Example Host",
        "venue": "Example Room",
        "neighborhood": "East Village",
        "address": "100 Example St, New York, NY",
        "price": "$20",
        "is_free": False,
        "description": "A structured conversation event.",
        "capacity_flag": None,
        "status": "active",
        "source_id": "example-source",
        "source_listing_url": "https://example.com/events",
        "source_url": "https://example.com/events/conversation-night",
        "source_event_id": "event-123",
        "fetched_at": NOW,
        "parser_version": "example-v1",
        "content_hash": "a" * 64,
        "extracted_json": {"id": "event-123"},
    }


def result(**overrides):
    values = {
        "state": "ok",
        "method": "python_adapter",
        "recipe_version": "example-v1",
        "started_at": NOW,
        "finished_at": NOW,
        "events": [event()],
        "rejections": [],
        "artifacts": [{
            "kind": "events_api_json",
            "path": "/tmp/example.json",
            "url": "https://example.com/api/events",
            "sha256": "b" * 64,
            "fetched_at": NOW,
        }],
        "detail": "Parsed one event.",
        "error": None,
    }
    values.update(overrides)
    return source_result(**values)


class AdapterContractTests(unittest.TestCase):
    def test_validates_and_returns_the_existing_dictionary_shape(self):
        payload = result()

        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["source"]["state"], "ok")
        self.assertEqual(payload["events"][0]["source_event_id"], "event-123")
        self.assertNotIn("explicit_age_min", payload["events"][0])

    def test_rejects_a_missing_raw_event_field(self):
        invalid = event()
        invalid.pop("content_hash")

        with self.assertRaisesRegex(ParseError, r"events\.0\.content_hash"):
            result(events=[invalid])

    def test_rejects_a_wrong_raw_event_type_without_coercion(self):
        invalid = event()
        invalid["is_free"] = "false"

        with self.assertRaisesRegex(ParseError, r"events\.0\.is_free"):
            result(events=[invalid])

    def test_rejects_an_invalid_or_naive_datetime(self):
        invalid = event()
        invalid["start"] = "2026-08-20T19:00:00"

        with self.assertRaisesRegex(ParseError, "UTC offset"):
            result(events=[invalid])

    def test_rejects_an_unknown_raw_event_field(self):
        invalid = event()
        invalid["conten_hash"] = invalid["content_hash"]

        with self.assertRaisesRegex(ParseError, r"events\.0\.conten_hash"):
            result(events=[invalid])

    def test_keeps_rejection_evidence_flexible(self):
        payload = result(events=[], rejections=[{
            "reason": "event_parse_failed",
            "raw": ["source", {"value": 7}],
        }])

        self.assertEqual(payload["rejections"][0]["raw"], ["source", {"value": 7}])

    def test_runner_turns_a_bypassed_contract_error_into_a_visible_failure(self):
        class InvalidAdapter:
            version = "invalid-v1"

            def __init__(self, client, artifacts=None):
                pass

            def crawl(self, source, seen_date, lookahead_days, timezone):
                return {"source": {}, "events": [], "rejections": []}

        source = {
            "id": "invalid-source",
            "url": "https://example.com/events",
            "adapter": "invalid",
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
                "crawler.runner.get_adapter", return_value=InvalidAdapter):
            payload = execute_source(
                source,
                {"run_config": {}},
                dt.date(2026, 8, 16),
                client=object(),
                artifact_root=tmp,
            )

        self.assertEqual(payload["source"]["state"], "parse_failed")
        self.assertIn("runtime schema validation", payload["source"]["error"])


if __name__ == "__main__":
    unittest.main()
