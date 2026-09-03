import datetime as dt
import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path

from bs4 import BeautifulSoup

from crawler.adapters.bored_dating import BoredDatingAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent


FIXTURES = Path(__file__).parent / 'fixtures'
SOURCE_URL = 'https://www.boredofdatingapps.com/events/'
EVENT_URL = 'https://www.boredofdatingapps.com/event/bookshop-meet-cute-new-york-boda-singles-social-2/'
TICKET_URL = 'https://www.fatsoma.com/e/wd8x6l2f/bookshop-meet-cute-new-york-boda-singles-social'
SOURCE = {'id': 'bored-of-dating-apps', 'url': SOURCE_URL, 'adapter': 'bored_dating'}
SEEN_DATE = dt.date(2026, 9, 3)
TIMEZONE = 'America/New_York'


class FakeClient:
    def __init__(self, listing=None, ticket=None):
        self.requests = []
        self.pages = {
            SOURCE_URL: listing if listing is not None else self.fixture('listing'),
            EVENT_URL: self.fixture('detail'),
            TICKET_URL: ticket if ticket is not None else self.fixture('ticket'),
        }

    @staticmethod
    def fixture(name):
        return (FIXTURES / ('bored_dating_' + name + '.html')).read_text()

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url=url, status=HTTPStatus.OK,
                            body=self.pages[url].encode(),
                            headers={'content-type': 'text/html'},
                            fetched_at='2026-09-03T12:00:00-04:00')


def replace_json(page, field, value):
    soup = BeautifulSoup(page, 'html.parser')
    script = soup.select_one('script[type="application/ld+json"]')
    item = json.loads(script.get_text())
    item[field] = value
    script.string = json.dumps(item)
    return str(soup)


class BoredDatingAdapterTests(unittest.TestCase):
    def test_normal_result_and_artifact_provenance(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            result = BoredDatingAdapter(client, ArtifactRecorder(tmp)).crawl(
                SOURCE, SEEN_DATE, 10, TIMEZONE)
            self.assertEqual(result['source']['state'], 'ok')
            self.assertEqual(len(result['events']), 1)
            event = result['events'][0]
            RawEvent.model_validate(event)
            self.assertEqual(event['start'], '2026-09-08T18:00:00-04:00')
            self.assertEqual(event['end'], '2026-09-08T22:30:00-04:00')
            self.assertEqual(event['explicit_age_min'], 21)
            self.assertEqual(event['price'], 'GBP 25.00 + booking fees')
            self.assertFalse(event['is_free'])
            self.assertEqual(event['capacity_flag'], 'Some ticket types sold out')
            self.assertEqual(event['signup_url'], TICKET_URL)
            self.assertEqual(event['source_event_id'], 'wd8x6l2f')
            self.assertEqual(event['parser_version'], PARSER_VERSION)
            self.assertEqual(event['source_url'], TICKET_URL)
            self.assertEqual(client.requests, [SOURCE_URL, EVENT_URL, TICKET_URL])
            artifacts = result['source']['artifacts']
            self.assertEqual(len(artifacts), 3)
            self.assertEqual(artifacts[-1]['sha256'], event['content_hash'])
            for artifact in artifacts:
                self.assertTrue(Path(artifact['path']).exists())
            self.assertEqual({r['reason'] for r in result['rejections']},
                             {'outside_date_window', 'outside_target_geography'})

    def test_inclusive_start_and_end_dates(self):
        for seen, lookahead in [(dt.date(2026, 9, 8), 0), (SEEN_DATE, 5)]:
            with self.subTest(seen=seen, lookahead=lookahead):
                result = BoredDatingAdapter(FakeClient()).crawl(SOURCE, seen, lookahead, TIMEZONE)
                self.assertEqual(len(result['events']), 1)

    def test_empty_window_has_ordered_coverage(self):
        client = FakeClient()
        result = BoredDatingAdapter(client).crawl(SOURCE, SEEN_DATE, 4, TIMEZONE)
        self.assertEqual(result['source']['state'], 'empty_verified')
        self.assertEqual(client.requests, [SOURCE_URL])

    def test_empty_array_without_proven_empty_signal_is_suspicious(self):
        result = BoredDatingAdapter(FakeClient(listing=FakeClient.fixture('empty'))).crawl(
            SOURCE, SEEN_DATE, 10, TIMEZONE)
        self.assertEqual(result['source']['state'], 'empty_suspicious')

    def test_missing_structure_raises(self):
        with self.assertRaises(ParseError):
            BoredDatingAdapter(FakeClient(listing='<h1>Events</h1>')).crawl(
                SOURCE, SEEN_DATE, 10, TIMEZONE)

    def test_malformed_detail_records_validation_failure(self):
        for field, value in [('startDate', '2026-09-08T22:00'), ('name', ''),
                             ('startDate', '2026-09-08T23:00:00Z'), ('eventStatus', '')]:
            with self.subTest(field=field, value=value):
                ticket = replace_json(FakeClient.fixture('ticket'), field, value)
                result = BoredDatingAdapter(FakeClient(ticket=ticket)).crawl(
                    SOURCE, SEEN_DATE, 10, TIMEZONE)
                self.assertEqual(result['source']['state'], 'validation_failed')
                self.assertEqual(result['events'], [])
                self.assertEqual(result['rejections'][-2]['reason'], 'event_parse_failed')

    def test_cancelled_events_are_rejected(self):
        ticket = replace_json(FakeClient.fixture('ticket'), 'eventStatus', 'EventCancelled')
        result = BoredDatingAdapter(FakeClient(ticket=ticket)).crawl(
            SOURCE, SEEN_DATE, 10, TIMEZONE)
        self.assertEqual(result['events'], [])
        self.assertIn('event_cancelled', [r['reason'] for r in result['rejections']])

    def test_card_clock_accepts_no_space_before_meridiem(self):
        listing = FakeClient.fixture('listing').replace('6:00 pm', '6:00pm')
        result = BoredDatingAdapter(FakeClient(listing=listing)).crawl(
            SOURCE, SEEN_DATE, 10, TIMEZONE)
        self.assertEqual(result['source']['state'], 'ok')
        self.assertEqual(len(result['events']), 1)

    def test_year_rollover_uses_structured_year(self):
        listing = FakeClient.fixture('listing').replace('2026-09-08T22:00', '2027-01-08T23:00')
        listing = listing.replace('Tue 8th Sep at 6:00 pm', 'Fri 8th Jan at 6:00 pm')
        ticket = replace_json(FakeClient.fixture('ticket'), 'startDate', '2027-01-08T23:00:00Z')
        ticket = replace_json(ticket, 'endDate', '2027-01-09T03:30:00Z')
        result = BoredDatingAdapter(FakeClient(listing=listing, ticket=ticket)).crawl(
            SOURCE, dt.date(2026, 12, 30), 10, TIMEZONE)
        self.assertEqual(result['events'][0]['start'], '2027-01-08T18:00:00-05:00')


if __name__ == '__main__':
    unittest.main()
