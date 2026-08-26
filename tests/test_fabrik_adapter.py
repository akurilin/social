import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler.adapters.fabrik import (
    ADAPTER_ID,
    FabrikGatheringsAdapter,
    PARSER_VERSION,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent
from crawler.registry import get_adapter


FIXTURES = Path(__file__).parent / "fixtures"


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        fixture = self.pages[page]
        body = fixture.read_bytes() if isinstance(fixture, Path) else fixture
        return HttpResponse(
            url, 200, body, {"content-type": "application/json"},
            "2026-08-26T12:00:00-04:00",
        )


class FabrikGatheringsAdapterTests(unittest.TestCase):
    def source(self):
        return {
            "id": "fabrik-gatherings",
            "title": "Fabrik — Gatherings",
            "url": "https://www.joinfabrik.com/gatherings",
        }

    def pages(self):
        return {
            1: FIXTURES / "fabrik_gatherings_page_1.json",
            2: FIXTURES / "fabrik_gatherings_page_2.json",
        }

    def test_paginates_filters_inclusive_window_and_saves_provenance(self):
        client = Client(self.pages())
        with tempfile.TemporaryDirectory() as tmp:
            result = FabrikGatheringsAdapter(
                client, ArtifactRecorder(tmp)).crawl(
                    self.source(), dt.date(2026, 8, 26), 2,
                    "America/New_York")

        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["page"] for url in client.requests],
            [["1"], ["2"]],
        )
        self.assertEqual(len(result["source"]["artifacts"]), 2)
        self.assertEqual(
            result["source"]["artifacts"][0]["url"], client.requests[0])
        self.assertTrue(result["source"]["artifacts"][0]["sha256"])

        first, last = result["events"]
        self.assertEqual(first["start"], "2026-08-26T10:00:00-04:00")
        self.assertEqual(first["is_free"], True)
        self.assertEqual(last["start"], "2026-08-28T23:00:00-05:00")
        self.assertEqual(last["end"], "2026-08-29T01:00:00-05:00")
        self.assertIn("members only", last["capacity_flag"])
        reasons = [item["reason"] for item in result["rejections"]]
        self.assertEqual(reasons, ["cancelled", "outside_date_window"])

    def test_events_satisfy_required_raw_event_fields(self):
        result = FabrikGatheringsAdapter(Client(self.pages())).crawl(
            self.source(), dt.date(2026, 8, 26), 2, "America/New_York")
        event = RawEvent.model_validate(result["events"][0])
        self.assertEqual(event.source_event_id, "event-start")
        self.assertEqual(event.host, "Studio Table")
        self.assertEqual(event.venue, "Tribeca")
        self.assertEqual(
            event.address,
            "12 Vestry St, New York, NY 10013, United States",
        )
        self.assertEqual(event.url, "https://luma.com/open-studio-breakfast")

    def test_explicit_empty_pagination_is_verified(self):
        client = Client({1: FIXTURES / "fabrik_gatherings_empty.json"})
        result = FabrikGatheringsAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 26), 2, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_malformed_top_level_structure_raises_parse_error(self):
        client = Client({1: FIXTURES / "fabrik_gatherings_malformed.json"})
        with self.assertRaisesRegex(ParseError, "missing an items list"):
            FabrikGatheringsAdapter(client).crawl(
                self.source(), dt.date(2026, 8, 26), 2,
                "America/New_York")

    def test_missing_event_fields_returns_validation_failure(self):
        payload = json.loads(
            (FIXTURES / "fabrik_gatherings_page_1.json").read_text())
        payload["total_count"] = 1
        payload["total_pages"] = 1
        payload["items"] = [payload["items"][0]]
        payload["items"][0].pop("space")
        result = FabrikGatheringsAdapter(
            Client({1: json.dumps(payload).encode()})).crawl(
                self.source(), dt.date(2026, 8, 26), 2,
                "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["rejections"][0]["reason"], "event_parse_failed")
        self.assertIn(
            "missing its space", result["rejections"][0]["raw"]["error"])

    def test_adapter_is_registered(self):
        self.assertIs(get_adapter(ADAPTER_ID), FabrikGatheringsAdapter)


if __name__ == "__main__":
    unittest.main()
