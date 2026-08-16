import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.pioneer_works import PARSER_VERSION, PioneerWorksAdapter
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = "https://pioneerworks.org/calendar"
PROGRAM = "https://pioneerworks.org/programs/opening-social"
CLASS = "https://pioneerworks.org/classes/print-workshop"


class Client:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url, 200, self.pages[url].read_bytes(),
                            {"content-type": "text/html"}, "2026-08-16T12:00:00-04:00")


class PioneerWorksAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "pioneer-works", "url": LISTING}

    def test_filters_calendar_before_detail_requests_and_excludes_exhibitions(self):
        client = Client({
            LISTING: FIXTURES / "pioneer_works_calendar.html",
            PROGRAM: FIXTURES / "pioneer_works_program_detail.html",
            CLASS: FIXTURES / "pioneer_works_class_detail.html",
        })
        with tempfile.TemporaryDirectory() as tmp:
            result = PioneerWorksAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(client.requests, [LISTING, PROGRAM, CLASS])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual([event["source_event_id"] for event in result["events"]],
                         ["program-1", "class-1"])
        self.assertEqual(result["events"][0]["signup_url"], "https://www.eventbrite.com/e/12345")
        self.assertEqual(result["events"][1]["price"], "$25")
        self.assertFalse(result["events"][1]["is_free"])
        self.assertEqual(result["events"][1]["start"], "2026-08-21T18:30:00-04:00")
        self.assertEqual(len(result["source"]["artifacts"]), 3)
        self.assertNotIn("https://pioneerworks.org/exhibitions/long-passive-exhibition", client.requests)

    def test_empty_event_collection_is_verified(self):
        empty = b'<script id="__NEXT_DATA__">{"props":{"pageProps":{"events":[]}}}</script>'
        client = Client({LISTING: FIXTURES / "pioneer_works_calendar.html"})
        client.pages[LISTING] = _BytesFixture(empty)
        result = PioneerWorksAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")

    def test_nonempty_calendar_without_supported_in_window_event_is_verified_empty(self):
        listing = b'''<script id="__NEXT_DATA__">{"props":{"pageProps":{"events":[
            {"_id":"old-class","_type":"class","title":"Old Class","slug":{"current":"old-class"},"calendar":{"startDate":"2026-07-20","startTime":"19:00"}},
            {"_id":"exhibition-1","_type":"exhibition","title":"Long Exhibition","slug":{"current":"long-exhibition"},"calendar":{"startDate":"2026-08-20","startTime":"12:00"}}
        ]}}}</script>'''
        client = Client({LISTING: _BytesFixture(listing)})
        result = PioneerWorksAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])
        self.assertEqual(client.requests, [LISTING])


class _BytesFixture:
    def __init__(self, body):
        self.body = body

    def read_bytes(self):
        return self.body


if __name__ == "__main__":
    unittest.main()
