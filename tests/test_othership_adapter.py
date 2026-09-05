import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.othership import (
    ADAPTER_ID,
    PARSER_VERSION,
    OthershipEventsAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent
from crawler.registry import get_adapter


FIXTURES = Path(__file__).parent / "fixtures"
LISTING = "https://www.othership.us/events"
START_DETAIL = "https://www.othership.us/events/start-event"
END_DETAIL = "https://www.othership.us/events/end-event"


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        value = self.pages[url]
        body = value.read_bytes() if isinstance(value, Path) else value
        if isinstance(body, str):
            body = body.encode()
        return HttpResponse(
            url=url,
            status=200,
            body=body,
            headers={"content-type": "text/html; charset=utf-8"},
            fetched_at="2026-08-28T12:00:00-04:00",
        )


class OthershipEventsAdapterTests(unittest.TestCase):
    def source(self):
        return {
            "id": "othership-events-nyc",
            "title": "Othership — New York events",
            "url": LISTING,
            "adapter": ADAPTER_ID,
        }

    def pages(self):
        return {
            LISTING: FIXTURES / "othership_listing.html",
            START_DETAIL: FIXTURES / "othership_detail_start.html",
            END_DETAIL: FIXTURES / "othership_detail_end.html",
        }

    def test_filters_before_details_and_saves_provenance(self):
        client = Client(self.pages())
        with tempfile.TemporaryDirectory() as tmp:
            result = OthershipEventsAdapter(
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
            ["outside_geo", "outside_date_window"],
        )
        self.assertEqual(result["events"][0]["start"],
                         "2026-08-28T21:00:00-04:00")
        self.assertEqual(result["events"][1]["end"],
                         "2026-09-07T21:00:00-04:00")

    def test_emits_required_fields(self):
        result = OthershipEventsAdapter(Client(self.pages())).crawl(
            self.source(), dt.date(2026, 8, 28), 10,
            "America/New_York")
        event = RawEvent.model_validate(result["events"][0])
        self.assertEqual(event.source_event_id, "start-event")
        self.assertEqual(event.host, "Othership")
        self.assertEqual(event.venue, "Othership Williamsburg")
        self.assertEqual(event.neighborhood, "Williamsburg")
        self.assertEqual(event.address, "25 Kent Ave, Brooklyn, NY")
        self.assertEqual(event.signup_url,
                         "https://www.othership.us/schedule?start")
        self.assertEqual(event.description, "Full start description.")
        self.assertEqual(event.explicit_age_min, 18)
        self.assertEqual(event.explicit_age_label, "18+")

    def test_explicit_empty_state_is_verified(self):
        client = Client({
            LISTING: FIXTURES / "othership_listing_empty.html",
        })
        result = OthershipEventsAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 28), 10,
            "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])
        self.assertEqual(client.requests, [LISTING])

    def test_malformed_listing_raises_parse_error(self):
        client = Client({
            LISTING: FIXTURES / "othership_listing_malformed.html",
        })
        with self.assertRaisesRegex(ParseError, "missing"):
            OthershipEventsAdapter(client).crawl(
                self.source(), dt.date(2026, 8, 28), 10,
                "America/New_York")

    def test_malformed_detail_returns_validation_failure(self):
        pages = self.pages()
        body = (FIXTURES / "othership_detail_start.html").read_text()
        pages[START_DETAIL] = body.replace(
            "flex-horizontal is-events-custom",
            "flex-horizontal",
        )
        result = OthershipEventsAdapter(Client(pages)).crawl(
            self.source(), dt.date(2026, 8, 28), 10,
            "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(any(item["reason"] == "detail_parse_failed"
                            for item in result["rejections"]))

    def test_adapter_is_registered(self):
        self.assertIs(get_adapter(ADAPTER_ID), OthershipEventsAdapter)


if __name__ == "__main__":
    unittest.main()
