import datetime as dt
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler.adapters.iic_new_york import IICNewYorkEventsAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://iicnewyork.esteri.it/en/gli_eventi/"
FILM = "https://iicnewyork.esteri.it/en/gli_eventi/calendario/italian-film-night/"
BOOK = "https://iicnewyork.esteri.it/en/gli_eventi/calendario/book-talk/"


class Client:
    def __init__(self, empty=False):
        self.empty = empty
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        path = urlsplit(url).path
        if path.endswith("italian-film-night/"):
            fixture = FIXTURES / "iic_new_york_detail_film.html"
        elif path.endswith("book-talk/"):
            fixture = FIXTURES / "iic_new_york_detail_book.html"
        elif self.empty:
            fixture = FIXTURES / "iic_new_york_empty.html"
        else:
            page = parse_qs(urlsplit(url).query).get("pag", ["1"])[0]
            fixture = FIXTURES / "iic_new_york_listing_page_{}.html".format(page)
        return HttpResponse(url, 200, fixture.read_bytes(),
                            {"content-type": "text/html; charset=utf-8"},
                            "2026-08-15T12:00:00-04:00")


class IICNewYorkEventsAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "iic-new-york", "url": URL, "adapter": "iic_new_york_events"}

    def test_paginates_listing_and_reads_authoritative_details(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmp:
            result = IICNewYorkEventsAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 20), 4, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        film, book = result["events"]
        self.assertEqual(film["start"], "2026-08-21T18:30:00-04:00")
        self.assertEqual(film["venue"], "IIC NY")
        self.assertEqual(film["address"], "686 Park Avenue, New York")
        self.assertEqual(film["price"], "Free")
        self.assertTrue(film["is_free"])
        self.assertNotIn("email", film["description"])
        self.assertFalse(book["is_free"])
        self.assertEqual([parse_qs(urlsplit(url).query)["pag"] for url in client.requests[:2]],
                         [["1"], ["2"]])
        query = parse_qs(urlsplit(client.requests[0]).query)
        self.assertEqual(query["date-init"], ["2026-08-20"])
        self.assertEqual(query["date-end"], ["2026-08-24"])
        self.assertEqual(len(result["source"]["artifacts"]), 4)

    def test_explicit_empty_listing_is_verified(self):
        result = IICNewYorkEventsAdapter(Client(empty=True)).crawl(
            self.source(), dt.date(2026, 8, 20), 4, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
