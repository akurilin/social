import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.metrograph import MetrographSpecialEventsAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE_URL = "https://metrograph.com/"
EVENTS_URL = "https://metrograph.com/events/"


class Client:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url, 200, self.fixture.read_bytes(),
                            {"content-type": "text/html; charset=utf-8"},
                            "2026-06-20T12:00:00-04:00")


class MetrographSpecialEventsAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "metrograph", "url": SOURCE_URL,
                "adapter": "metrograph_special_events"}

    def test_reads_only_special_event_cards_and_filters_window(self):
        client = Client(FIXTURES / "metrograph_special_events_listing.html")
        with tempfile.TemporaryDirectory() as tmp:
            result = MetrographSpecialEventsAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 6, 27), 0, "America/New_York")
        self.assertEqual(client.requests, [EVENTS_URL])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual({event["title"] for event in result["events"]},
                         {"Bouchra", "ACE Presents Breakfast of Champions"})
        self.assertNotIn("Ordinary Showtime", {event["title"] for event in result["events"]})
        self.assertEqual(result["events"][0]["start"], "2026-06-27T16:40:00-04:00")
        self.assertEqual(result["events"][0]["source_event_id"], "1001-30148")
        self.assertEqual(len(result["source"]["artifacts"]), 1)
        self.assertIn("outside_date_window", [item["reason"] for item in result["rejections"]])

    def test_empty_special_event_block_is_verified(self):
        result = MetrographSpecialEventsAdapter(
            Client(FIXTURES / "metrograph_special_events_empty.html")).crawl(
                self.source(), dt.date(2026, 6, 27), 0, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
