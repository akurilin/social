import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.nitehawk import NitehawkAdapter, PARSER_VERSION, _listing_events
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import FetchError, HttpResponse


FIXTURES = Path(__file__).parent / "fixtures"
PAGES = {
    "https://nitehawkcinema.com/williamsburg/coming-soon/": FIXTURES / "nitehawk_listing.html",
    "https://nitehawkcinema.com/prospectpark/coming-soon-2/": FIXTURES / "nitehawk_listing.html",
    "https://nitehawkcinema.com/prospectpark/movie-trivia-nite/": FIXTURES / "nitehawk_trivia.html",
}


class Client:
    def __init__(self, pages=PAGES):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        page = self.pages[url]
        body = page.read_bytes()
        return HttpResponse(url=url, status=200, body=body,
                            headers={"content-type": "text/html; charset=utf-8"},
                            fetched_at="2026-08-15T12:00:00-04:00")


class NitehawkAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "nitehawk", "url": "https://nitehawkcinema.com", "adapter": "nitehawk"}

    def test_parses_both_venues_and_trivia_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = NitehawkAdapter(Client(), ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 3)
        film = result["events"][0]
        self.assertEqual(film["start"], "2026-08-19T19:00:00-04:00")
        self.assertEqual(film["url"], "https://nitehawkcinema.com/purchase/12345/")
        self.assertEqual(film["signup_url"], "https://nitehawkcinema.com/purchase/12345/")
        self.assertEqual(film["venue"], "Nitehawk Cinema Williamsburg")
        self.assertEqual(film["source_listing_url"], "https://nitehawkcinema.com")
        self.assertEqual(film["source_url"], "https://nitehawkcinema.com/williamsburg/coming-soon/")
        self.assertTrue(film["content_hash"])
        self.assertEqual(film["fetched_at"], "2026-08-15T12:00:00-04:00")
        trivia = result["events"][-1]
        self.assertEqual(trivia["title"], "Movie Trivia Nite")
        self.assertEqual(trivia["start"], "2026-08-18T20:00:00-04:00")
        self.assertEqual(trivia["signup_url"], "https://nitehawkcinema.com/prospectpark/movie-trivia-nite/")
        self.assertEqual(trivia["source_listing_url"], "https://nitehawkcinema.com")
        self.assertEqual(trivia["source_url"], "https://nitehawkcinema.com/prospectpark/movie-trivia-nite/")
        self.assertEqual(len(result["source"]["artifacts"]), 3)

    def test_date_window_rejects_all_out_of_window_candidates(self):
        result = NitehawkAdapter(Client()).crawl(
            self.source(), dt.date(2026, 9, 1), 2, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])
        self.assertEqual(len(result["rejections"]), 3)

    def test_partial_page_failure_preserves_valid_events(self):
        class PartialClient(Client):
            def get(self, url):
                if "prospectpark/coming-soon" in url:
                    raise FetchError("temporary failure")
                return super().get(url)
        result = NitehawkAdapter(PartialClient()).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertTrue(result["events"])
        self.assertIn("temporary failure", result["source"]["error"])

    def test_all_page_failures_are_parse_failure(self):
        class FailingClient:
            def get(self, url):
                raise FetchError("offline")
        result = NitehawkAdapter(FailingClient()).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "parse_failed")
        self.assertEqual(result["events"], [])

    def test_explicit_empty_pages_are_verified_empty(self):
        empty = FIXTURES / "nitehawk_empty.html"
        pages = {url: empty for url in PAGES}
        result = NitehawkAdapter(Client(pages)).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_each_purchase_uses_its_date_and_repeated_showtime_links_collapse(self):
        body = (FIXTURES / "nitehawk_multi_date_listing.html").read_bytes()
        response = HttpResponse(
            url="https://nitehawkcinema.com/prospectpark/coming-soon-2/",
            status=200, body=body,
            headers={"content-type": "text/html; charset=utf-8"},
            fetched_at="2026-08-15T12:00:00-04:00",
        )
        events = _listing_events(
            response, self.source(), "Nitehawk Cinema Prospect Park",
            "188 Prospect Park West, Brooklyn, NY", dt.date(2026, 8, 15),
            "America/New_York",
        )
        self.assertEqual(
            [(event["start"], event["url"]) for event in events],
            [
                ("2026-08-18T21:30:00-04:00",
                 "https://nitehawkcinema.com/prospectpark/purchase/111/"),
                ("2026-08-19T21:30:00-04:00",
                 "https://nitehawkcinema.com/prospectpark/purchase/222/"),
                ("2026-08-20T19:00:00-04:00",
                 "https://nitehawkcinema.com/prospectpark/showtimes/special-8-20-26-700-pm/"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
