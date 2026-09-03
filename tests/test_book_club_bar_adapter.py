import datetime as dt
from http import HTTPStatus
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from crawler.adapters.book_club_bar import (
    ADAPTER_ID, PARSER_VERSION, BookClubBarAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent
from crawler.registry import get_adapter


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = {"id": "book-club-bar", "title": "Book Club Bar",
          "url": "https://www.bookclubbar.com/events"}


class Client:
    def __init__(self, text):
        self.text = text
        self.requests = []

    def rendered_session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, ready_selector):
        self.requests.append((url, ready_selector))
        return HttpResponse(url, HTTPStatus.OK, self.text.encode(),
                            {"content-type": "text/html"},
                            "2026-09-08T12:00:00-04:00")


class BookClubBarAdapterTests(unittest.TestCase):
    def listing(self):
        return (FIXTURES / "book_club_bar_listing.html").read_text()

    def crawl(self, html=None, lookahead=4):
        return BookClubBarAdapter(Client(html if html is not None else self.listing())).crawl(
            SOURCE, dt.date(2026, 9, 8), lookahead, "America/New_York")

    def test_inclusive_window_locations_ticket_link_and_artifacts(self):
        client = Client(self.listing())
        with tempfile.TemporaryDirectory() as directory:
            result = BookClubBarAdapter(client, ArtifactRecorder(directory)).crawl(
                SOURCE, dt.date(2026, 9, 8), 4, "America/New_York")
            artifact = result["source"]["artifacts"][0]
            self.assertTrue(Path(artifact["path"]).exists())
            self.assertEqual(artifact["url"], SOURCE["url"])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(len(result["events"]), 3)
        first, second, last = result["events"]
        self.assertEqual(first["start"], "2026-09-08T18:00:00-04:00")
        self.assertEqual(last["start"], "2026-09-12T11:00:00-04:00")
        self.assertIn("boredofdatingapps.com", first["signup_url"])
        self.assertEqual(first["neighborhood"], "East Village")
        self.assertEqual(second["neighborhood"], "Bushwick")
        self.assertTrue(second["is_free"])
        self.assertNotEqual(first["source_event_id"], second["source_event_id"])
        self.assertEqual(result["rejections"][0]["reason"], "outside_date_window")
        self.assertEqual(len(client.requests), 1)
        for event in result["events"]:
            parsed = RawEvent.model_validate(event)
            self.assertEqual(parsed.parser_version, PARSER_VERSION)
            self.assertEqual(parsed.content_hash, artifact["sha256"])

    def test_empty_page_without_explicit_signal_is_suspicious(self):
        result = self.crawl((FIXTURES / "book_club_bar_empty.html").read_text())
        self.assertEqual(result["source"]["state"], "empty_suspicious")

    def test_explicit_empty_marker_is_verified(self):
        result = self.crawl('<main id="main-content"><div class="ant-empty-description">'
                            'No upcoming events</div></main>')
        self.assertEqual(result["source"]["state"], "empty_verified")

    def test_unknown_empty_marker_remains_suspicious(self):
        result = self.crawl('<main id="main-content"><div class="ant-empty-description">'
                            'Loading failed</div></main>')
        self.assertEqual(result["source"]["state"], "empty_suspicious")

    def test_truncated_window_is_not_success(self):
        result = self.crawl(lookahead=10)
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertIn("complete coverage", result["source"]["error"])

    def test_last_card_on_final_day_does_not_prove_coverage(self):
        self.assertEqual(self.crawl(lookahead=6)["source"]["state"], "validation_failed")

    def test_malformed_structure_raises(self):
        with self.assertRaisesRegex(ParseError, "main event container"):
            self.crawl("<div>Changed page</div>")

    def test_missing_time_fails_validation(self):
        result = self.crawl(self.listing().replace("6:00 PM", "Time TBD", 1))
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertIn("time range", result["source"]["error"])

    def test_compact_time_keeps_local_timezone(self):
        result = self.crawl(self.listing().replace("6:00 PM", "6:00PM", 1))
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["events"][0]["start"], "2026-09-08T18:00:00-04:00")

    def test_cancelled_event_is_rejected_and_private_event_kept(self):
        soup = BeautifulSoup(self.listing(), "html.parser")
        headings = soup.select("h4")
        headings[0].string = "Cancelled: Reading"
        headings[1].string = "Private Event"
        result = self.crawl(str(soup))
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual([r["reason"] for r in result["rejections"]],
                         ["cancelled", "outside_date_window"])
        self.assertEqual(result["events"][0]["capacity_flag"], "private event")

    def test_unsorted_cards_fail_validation(self):
        soup = BeautifulSoup(self.listing(), "html.parser")
        main = soup.select_one("main")
        main.insert(0, main.find_all("h4")[-1].parent.extract())
        self.assertEqual(self.crawl(str(soup))["source"]["state"], "validation_failed")

    def test_registry(self):
        self.assertIs(get_adapter(ADAPTER_ID), BookClubBarAdapter)


if __name__ == "__main__":
    unittest.main()
