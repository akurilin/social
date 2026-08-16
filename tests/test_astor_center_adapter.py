import datetime as dt
import unittest
from pathlib import Path
import tempfile

from crawler.adapters.astor_center import AstorCenterAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = "https://www.astorcenternyc.com/calendar.aspx"
DETAIL = "https://www.astorcenternyc.com/class-wines-of-portugal.ac"
DISALLOWED_DETAIL = "https://www.astorcenternyc.com/controls/class-wines-of-portugal.ac"


class Client:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url=url, status=200, body=self.pages[url].read_bytes(),
                            headers={"content-type": "text/html; charset=utf-8"},
                            fetched_at="2026-08-15T12:00:00-04:00")


class AstorCenterAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "astor-center", "url": LISTING, "adapter": "astor_center"}

    def test_parses_listing_detail_and_filters_window(self):
        client = Client({LISTING: FIXTURES / "astor_center_listing.html",
                         DETAIL: FIXTURES / "astor_center_detail.html",
                         "https://www.astorcenternyc.com/class-old.ac": FIXTURES / "astor_center_detail.html"})
        with tempfile.TemporaryDirectory() as tmp:
            result = AstorCenterAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(len(result["rejections"]), 1)
        event = result["events"][0]
        self.assertEqual(event["start"], "2026-08-21T18:30:00-04:00")
        self.assertEqual(event["host"], "Vitalii Dascaliuc")
        self.assertEqual(event["price"], "$89")
        self.assertEqual(event["address"], "399 Lafayette Street, New York, NY 10003")
        self.assertEqual(event["signup_url"], DETAIL)
        self.assertEqual(event["source_event_id"], "wines-of-portugal")
        self.assertEqual(len(result["source"]["artifacts"]), 2)
        self.assertEqual(client.requests, [LISTING, DETAIL])
        self.assertNotIn(DISALLOWED_DETAIL, client.requests)

    def test_no_active_links_is_verified_empty(self):
        client = Client({LISTING: FIXTURES / "astor_center_empty.html"})
        result = AstorCenterAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_no_links_without_empty_signal_is_parse_failure(self):
        client = Client({LISTING: FIXTURES / "astor_center_structure_changed.html"})
        result = AstorCenterAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "parse_failed")
        self.assertIn("no class links", result["source"]["error"])


if __name__ == "__main__":
    unittest.main()
