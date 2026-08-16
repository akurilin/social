import datetime as dt
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler.adapters.luma import (
    LUMA_CATEGORY_ADAPTER_ID,
    LUMA_CATEGORY_API,
    LUMA_CATEGORY_PARSER_VERSION,
    LumaCategoryAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://lu.ma/arts"


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        cursor = parse_qs(urlsplit(url).query).get("pagination_cursor", [None])[0]
        fixture = self.pages[cursor]
        body = fixture.read_bytes() if isinstance(fixture, Path) else fixture.encode("utf-8")
        return HttpResponse(
            url=url,
            status=200,
            body=body,
            headers={"content-type": "application/json; charset=utf-8"},
            fetched_at="2026-08-15T12:00:00-04:00",
        )


class LumaCategoryAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "luma-nyc-arts", "url": URL,
                "adapter": LUMA_CATEGORY_ADAPTER_ID}

    def test_paginates_and_strictly_filters_nyc_and_date_window(self):
        client = FakeClient({
            None: FIXTURES / "luma_category_page_1.json",
            "cursor-two": FIXTURES / "luma_category_page_2.json",
        })
        with tempfile.TemporaryDirectory() as tmp:
            result = LumaCategoryAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], LUMA_CATEGORY_PARSER_VERSION)
        self.assertEqual([event["source_event_id"] for event in result["events"]],
                         ["evt-nyc-one", "evt-nyc-two"])
        self.assertEqual({item["reason"] for item in result["rejections"]},
                         {"outside_nyc", "outside_date_window"})
        first = result["events"][0]
        self.assertEqual(first["start"], "2026-08-16T19:00:00-04:00")
        self.assertEqual(first["url"], "https://luma.com/brooklyn-sculpture-salon")
        self.assertEqual(first["price"], "$25")
        self.assertEqual(first["capacity_flag"], "limited")
        self.assertEqual(first["address"],
                         "Studio 17, 123 Atlantic Ave, Brooklyn, NY 11201, USA")
        self.assertEqual(len(result["source"]["artifacts"]), 2)
        self.assertEqual(len(client.requests), 2)
        self.assertTrue(client.requests[0].startswith(LUMA_CATEGORY_API + "?"))
        first_query = parse_qs(urlsplit(client.requests[0]).query)
        self.assertEqual(first_query["slug"], ["arts"])
        self.assertEqual(first_query["pagination_limit"], ["50"])
        second_query = parse_qs(urlsplit(client.requests[1]).query)
        self.assertEqual(second_query["pagination_cursor"], ["cursor-two"])

    def test_explicit_empty_page_is_verified(self):
        result = LumaCategoryAdapter(FakeClient({
            None: FIXTURES / "luma_category_empty.json",
        })).crawl(self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_terminal_filtered_zero_is_verified_after_complete_ordered_scan(self):
        result = LumaCategoryAdapter(FakeClient({
            None: FIXTURES / "luma_category_filtered_empty.json",
        })).crawl(self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["rejections"][0]["reason"], "outside_nyc")

    def test_unordered_terminal_zero_is_suspicious(self):
        body = (FIXTURES / "luma_category_page_2.json").read_text(encoding="utf-8")
        body = body.replace("2026-08-30T23:30:00.000Z", "2026-08-16T20:00:00.000Z")
        result = LumaCategoryAdapter(FakeClient({None: body})).crawl(
            self.source(), dt.date(2026, 9, 1), 2, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_suspicious")


if __name__ == "__main__":
    unittest.main()
