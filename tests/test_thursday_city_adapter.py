import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.thursday_city import (
    DETAIL_READY_SELECTOR,
    LISTING_READY_SELECTOR,
    PARSER_VERSION,
    ThursdayCityAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent


FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE_URL = "https://www.thursday.com/events/new-york"
ONE_URL = SOURCE_URL + "/event-one-id"
BOUNDARY_URL = SOURCE_URL + "/event-boundary-id"
LATER_URL = SOURCE_URL + "/event-later-id"


class RenderedClient:
    def __init__(self, listing="thursday_city_listing.html", bad_detail=False):
        self.listing = listing
        self.bad_detail = bad_detail
        self.requests = []

    def rendered_session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, _error_type, _error, _traceback):
        return None

    def get(self, url, ready_selector):
        self.requests.append((url, ready_selector))
        if url == SOURCE_URL:
            fixture = self.listing
            response_url = url
        elif url == ONE_URL:
            fixture = ("thursday_city_detail_bad.html" if self.bad_detail
                       else "thursday_city_detail_one.html")
            response_url = SOURCE_URL + "/first-mixer"
        elif url == BOUNDARY_URL:
            fixture = "thursday_city_detail_boundary.html"
            response_url = SOURCE_URL + "/boundary-mixer"
        else:
            raise AssertionError("unexpected rendered URL: {}".format(url))
        return HttpResponse(
            url=response_url, status=200,
            body=(FIXTURES / fixture).read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
            fetched_at="2026-08-15T12:00:00-04:00",
        )


class ThursdayCityAdapterTests(unittest.TestCase):
    def source(self):
        return {
            "id": "thursday-nyc", "url": SOURCE_URL,
            "adapter": "thursday_city_browser",
        }

    def test_parses_details_and_filters_before_detail_requests(self):
        client = RenderedClient()
        with tempfile.TemporaryDirectory() as temporary:
            result = ThursdayCityAdapter(
                client, ArtifactRecorder(temporary)).crawl(
                    self.source(), dt.date(2026, 8, 15), 10,
                    "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        first, boundary = result["events"]
        RawEvent.model_validate(first)
        RawEvent.model_validate(boundary)
        self.assertEqual(first["start"], "2026-08-15T19:00:00-04:00")
        self.assertEqual(first["end"], "2026-08-15T22:30:00-04:00")
        self.assertEqual(first["url"], SOURCE_URL + "/first-mixer")
        self.assertEqual(first["address"], "1 First Street, New York, NY 10001")
        self.assertEqual(first["explicit_age_min"], 25)
        self.assertEqual(first["explicit_age_max"], 35)
        self.assertEqual(first["explicit_age_label"], "25-35")
        self.assertIs(first["is_free"], False)
        self.assertIs(boundary["is_free"], True)
        self.assertEqual(boundary["capacity_flag"], "Limited availability")
        self.assertEqual(
            client.requests,
            [(SOURCE_URL, LISTING_READY_SELECTOR),
             (ONE_URL, DETAIL_READY_SELECTOR),
             (BOUNDARY_URL, DETAIL_READY_SELECTOR)],
        )
        self.assertNotIn(LATER_URL, [url for url, _selector in client.requests])
        self.assertIn(
            "outside_date_window",
            [item["reason"] for item in result["rejections"]],
        )
        self.assertEqual(len(result["source"]["artifacts"]), 3)
        self.assertEqual(
            {item["kind"] for item in result["source"]["artifacts"]},
            {"rendered_listing_html", "rendered_event_html"},
        )

    def test_explicit_empty_signal_is_verified(self):
        client = RenderedClient(listing="thursday_city_empty.html")
        result = ThursdayCityAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])
        self.assertEqual(client.requests, [(SOURCE_URL, LISTING_READY_SELECTOR)])

    def test_malformed_listing_raises_parse_error(self):
        client = RenderedClient(listing="thursday_city_malformed.html")
        with self.assertRaisesRegex(ParseError, "malformed event card"):
            ThursdayCityAdapter(client).crawl(
                self.source(), dt.date(2026, 8, 15), 10,
                "America/New_York")

    def test_malformed_detail_is_validation_failed(self):
        result = ThursdayCityAdapter(RenderedClient(bad_detail=True)).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("1 event(s) failed", result["source"]["error"])
        self.assertIn(
            "event_parse_failed",
            [item["reason"] for item in result["rejections"]],
        )


if __name__ == "__main__":
    unittest.main()
