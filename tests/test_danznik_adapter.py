import datetime as dt
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from crawler.adapters.danznik import (
    ADAPTER_ID, DanznikAdapter, _document, schedule_url,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent
from crawler.registry import get_adapter


FIXTURES = Path(__file__).parent / 'fixtures'
LISTING = 'https://www.danznik.com/studios/nyc/schedule/'
WIDGET_ID = 'e2178160965d'
START = dt.date(2026, 9, 3)
SOURCE = {'id': 'danznik-nyc', 'url': LISTING, 'adapter': ADAPTER_ID}
TIMEZONE = 'America/New_York'


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url, HTTPStatus.OK, self.pages[url].encode(),
                            {'content-type': 'text/html'},
                            '2026-09-03T12:00:00-04:00')


def fixture(name):
    return (FIXTURES / name).read_text()


def pages():
    return {
        LISTING: fixture('danznik_listing.html'),
        schedule_url(WIDGET_ID, START): fixture('danznik_week_start.js'),
        schedule_url(WIDGET_ID, START + dt.timedelta(days=7)):
            fixture('danznik_week_end.js'),
    }


class DanznikAdapterTests(unittest.TestCase):
    def test_week_coverage_fields_and_artifacts(self):
        client = Client(pages())
        with tempfile.TemporaryDirectory() as tmp:
            result = DanznikAdapter(client, ArtifactRecorder(tmp)).crawl(
                SOURCE, START, 10, TIMEZONE)
            artifacts = result['source']['artifacts']
            self.assertEqual(len(artifacts), 3)
            self.assertTrue(all(Path(a['path']).exists() for a in artifacts))
        self.assertEqual(result['source']['state'], 'ok')
        self.assertEqual(len(client.requests), 3)
        first = RawEvent.model_validate(result['events'][0])
        self.assertEqual(first.start, '2026-09-03T20:30:00-04:00')
        self.assertEqual(first.title, 'International Latin w/ Tatiana Keegan (Silver Level)')
        self.assertIn('Style: International Latin', first.description)
        self.assertEqual(first.venue, 'Danznik NYC')
        self.assertTrue(first.source_event_id)
        self.assertTrue(first.content_hash)
        self.assertIsNone(first.is_free)
        self.assertIn('outside_date_window', [r['reason'] for r in result['rejections']])

    def test_inclusive_last_day(self):
        result = DanznikAdapter(Client(pages())).crawl(SOURCE, START, 7, TIMEZONE)
        dates = [dt.datetime.fromisoformat(e['start']).date() for e in result['events']]
        self.assertIn(START, dates)
        self.assertIn(START + dt.timedelta(days=7), dates)
        self.assertTrue(all(START <= date <= START + dt.timedelta(days=7) for date in dates))

    def test_explicit_empty_week(self):
        start = dt.date(2027, 9, 3)
        client = Client({LISTING: fixture('danznik_listing.html'),
                         schedule_url(WIDGET_ID, start): fixture('danznik_empty.js')})
        result = DanznikAdapter(client).crawl(SOURCE, start, 6, TIMEZONE)
        self.assertEqual(result['source']['state'], 'empty_verified')
        self.assertFalse(result['events'])

    def test_unverified_empty_is_suspicious(self):
        start = dt.date(2027, 9, 3)
        body = fixture('danznik_empty.js').replace('no more classes today.', 'loading.')
        client = Client({LISTING: fixture('danznik_listing.html'),
                         schedule_url(WIDGET_ID, start): body})
        result = DanznikAdapter(client).crawl(SOURCE, start, 0, TIMEZONE)
        self.assertEqual(result['source']['state'], 'empty_suspicious')

    def test_malformed_structure_and_ignored_date_raise(self):
        with self.assertRaises(ParseError):
            _document('alert("missing schedule")')
        data = pages()
        data[schedule_url(WIDGET_ID, START + dt.timedelta(days=7))] = data[
            schedule_url(WIDGET_ID, START)]
        with self.assertRaisesRegex(ParseError, 'requested week'):
            DanznikAdapter(Client(data)).crawl(SOURCE, START, 10, TIMEZONE)

    def test_bad_time_retains_other_events_and_rejection(self):
        data = pages()
        url = schedule_url(WIDGET_ID, START)
        data[url] = data[url].replace('2026-09-03T20:30:00.000+00:00', 'invalid')
        result = DanznikAdapter(Client(data)).crawl(SOURCE, START, 6, TIMEZONE)
        self.assertEqual(result['source']['state'], 'validation_failed')
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['rejections'][0]['reason'], 'invalid_class')

    def test_registered(self):
        self.assertIs(get_adapter(ADAPTER_ID), DanznikAdapter)


if __name__ == '__main__':
    unittest.main()
