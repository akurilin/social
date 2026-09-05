import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.reading_rhythms import (
    ADAPTER_ID,
    PARSER_VERSION,
    ReadingRhythmsAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent
from crawler.registry import get_adapter


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = "https://readingrhythms.co/events"
SOURCE_URL = LISTING + "#new-york-city"
START_DETAIL = "https://readingrhythms.co/events/queens/books-and-blankets-aug-28-start000"
END_DETAIL = "https://readingrhythms.co/events/williamsburg/pages-in-the-park-sep-7-end00000"


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        value = self.pages[url]
        body = value.read_bytes() if isinstance(value, Path) else value
        return HttpResponse(
            url=url,
            status=200,
            body=body,
            headers={"content-type": "text/html; charset=utf-8"},
            fetched_at="2026-08-28T12:00:00-04:00",
        )


class ReadingRhythmsAdapterTests(unittest.TestCase):
    def source(self):
        return {
            "id": "reading-rhythms-nyc",
            "title": "Reading Rhythms — NYC events",
            "url": SOURCE_URL,
            "adapter": ADAPTER_ID,
        }

    def pages(self):
        return {
            LISTING: FIXTURES / "reading_rhythms_listing.html",
            START_DETAIL: FIXTURES / "reading_rhythms_detail_start.html",
            END_DETAIL: FIXTURES / "reading_rhythms_detail_end.html",
        }

    def test_filters_inclusive_window_before_details_and_saves_provenance(self):
        client = Client(self.pages())
        with tempfile.TemporaryDirectory() as tmp:
            result = ReadingRhythmsAdapter(
                client, ArtifactRecorder(tmp)).crawl(
                    self.source(), dt.date(2026, 8, 28), 10,
                    "America/New_York")

        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(client.requests, [LISTING, START_DETAIL, END_DETAIL])
        self.assertEqual(len(result["source"]["artifacts"]), 2)
        self.assertEqual(result["source"]["artifacts"][0]["url"], LISTING)
        self.assertTrue(result["source"]["artifacts"][1]["sha256"])
        self.assertEqual(
            [item["reason"] for item in result["rejections"]],
            ["outside_date_window", "outside_date_window"],
        )
        first, last = result["events"]
        self.assertEqual(first["start"], "2026-08-28T18:30:00-04:00")
        self.assertEqual(first["end"], "2026-08-28T21:00:00-04:00")
        self.assertEqual(last["start"], "2026-09-07T13:00:00-04:00")

    def test_emits_required_fields_from_detail_jsonld(self):
        result = ReadingRhythmsAdapter(Client(self.pages())).crawl(
            self.source(), dt.date(2026, 8, 28), 10,
            "America/New_York")
        event = RawEvent.model_validate(result["events"][0])
        self.assertEqual(event.source_event_id, "start0000-0000-4000-8000-000000000000")
        self.assertEqual(event.host, "Reading Rhythms Queens")
        self.assertEqual(event.venue, "Astoria Bookshop")
        self.assertEqual(event.neighborhood, "Queens")
        self.assertEqual(event.address, "31-29 31st St, Astoria, NY 11106")
        self.assertEqual(event.price, "$20–30")
        self.assertFalse(event.is_free)
        self.assertEqual(event.signup_url, "https://luma.com/example-start")
        self.assertEqual(event.capacity_flag, "sold_out")

    def test_explicit_zero_city_count_is_verified_empty(self):
        client = Client({
            LISTING: FIXTURES / "reading_rhythms_listing_empty.html",
        })
        result = ReadingRhythmsAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 28), 10,
            "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])
        self.assertEqual(client.requests, [LISTING])

    def test_malformed_listing_raises_parse_error(self):
        client = Client({
            LISTING: FIXTURES / "reading_rhythms_listing_malformed.html",
        })
        with self.assertRaisesRegex(ParseError, "count does not match"):
            ReadingRhythmsAdapter(client).crawl(
                self.source(), dt.date(2026, 8, 28), 10,
                "America/New_York")

    def test_malformed_detail_returns_validation_failure(self):
        pages = self.pages()
        body = (FIXTURES / "reading_rhythms_detail_start.html").read_text()
        payload = json.loads(body.split("<script type=\"application/ld+json\">", 1)[1]
                             .split("</script>", 1)[0])
        payload.pop("startDate")
        pages[START_DETAIL] = (
            "<script type=\"application/ld+json\">{}"
            "</script>".format(json.dumps(payload)).encode())
        result = ReadingRhythmsAdapter(Client(pages)).crawl(
            self.source(), dt.date(2026, 8, 28), 10,
            "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(any(item["reason"] == "detail_parse_failed"
                            for item in result["rejections"]))

    def test_adapter_is_registered(self):
        self.assertIs(get_adapter(ADAPTER_ID), ReadingRhythmsAdapter)


if __name__ == "__main__":
    unittest.main()
