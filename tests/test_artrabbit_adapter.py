"""Read all calendar pages and preserve each published visit slot."""
import datetime as dt
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from crawler.adapters.artrabbit import ArtRabbitAdapter, listing_url, parse_detail
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, FetchError

FIXTURES = Path(__file__).parent / 'fixtures' / 'art_sources'
SOURCE = {'id': 'artrabbit-nyc', 'url': 'https://www.artrabbit.com/all-listings/united-states/new-york'}
DETAIL = 'https://www.artrabbit.com/events/springbreak-art-show-2026'
DAY = dt.date(2026, 9, 27)
ZONE = 'America/New_York'
EMPTY = '<p class="m_result-text">No events found</p>'


def response(url, text):
    return HttpResponse(url, HTTPStatus.OK, text.encode(), {}, '2026-09-27T12:00:00-04:00')


def card(url=DETAIL, dates='23 Sep 2026 – 28 Sep 2026', ident='1'):
    return f'<article class="m_listing-item" data-ident="{ident}"><a class="m_listing-link" href="{url}">Show</a><p class="b_small-heading mod--colour">{dates}</p></article>'


class Client:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def get(self, url):
        self.requests.append(url)
        return response(url, self.pages[url])


class ArtRabbitTests(unittest.TestCase):
    def pages(self):
        return {listing_url(SOURCE['url'], 'just-opened', DAY): card(),
                listing_url(SOURCE['url'], 'upcoming', DAY): EMPTY,
                DETAIL: (FIXTURES / 'artrabbit_dated.html').read_text()}

    def test_ongoing_show_daily_hours_and_artifacts(self):
        client = Client(self.pages())
        with tempfile.TemporaryDirectory() as tmp:
            result = ArtRabbitAdapter(client, ArtifactRecorder(tmp)).crawl(SOURCE, DAY, 1, ZONE)
            self.assertEqual(len(result['source']['artifacts']), 3)
            self.assertTrue(all(Path(a['path']).exists() for a in result['source']['artifacts']))
        self.assertEqual(result['source']['state'], 'ok')
        self.assertEqual([(e['start'], e['end']) for e in result['events']],
                         [('2026-09-27T12:00:00-04:00', '2026-09-27T19:00:00-04:00'),
                          ('2026-09-28T12:00:00-04:00', '2026-09-28T19:00:00-04:00')])
        event = result['events'][0]
        self.assertEqual(event['signup_url'], 'https://www.springbreakartshow.com/')
        self.assertIn('555 West 57th Street', event['address'])
        self.assertEqual(event['source_url'], DETAIL)
        self.assertEqual(event['extracted_json']['time_precision'], 'time')

    def test_collection_is_not_a_daily_physical_event(self):
        detail = response(DETAIL, (FIXTURES / 'artrabbit_collection.html').read_text())
        events, reason = parse_detail(detail, SOURCE, DAY, 1, ZONE)
        self.assertEqual(events, [])
        self.assertEqual(reason, 'event_collection')

    def test_nearby_city_is_outside_nyc_scope(self):
        text = (FIXTURES / 'artrabbit_dated.html').read_text().replace('"addressLocality": "New York"', '"addressLocality": "Newark"')
        events, reason = parse_detail(response(DETAIL, text), SOURCE, DAY, 1, ZONE)
        self.assertEqual(events, [])
        self.assertEqual(reason, 'outside_location')

    def test_visible_detail_without_jsonld(self):
        detail = response(DETAIL, (FIXTURES / 'artrabbit_visible.html').read_text())
        events, _ = parse_detail(detail, SOURCE, DAY, 1, ZONE)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['title'], 'Analia Saban: Data Center')
        self.assertIn('535 West 24th Street', events[0]['address'])

    def test_special_hours_outside_range_do_not_break_regular_schedule(self):
        detail = response(DETAIL, (FIXTURES / 'artrabbit_special.html').read_text())
        events, _ = parse_detail(detail, SOURCE, DAY, 1, ZONE)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['start'][11:16] == '11:00' for e in events))

    def test_special_hours_alone_do_not_mark_other_days_closed(self):
        detail = response(DETAIL, (FIXTURES / 'artrabbit_special_only.html').read_text())
        events, _ = parse_detail(detail, SOURCE, DAY, 1, ZONE)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['extracted_json']['time_precision'] == 'date' for e in events))

    def test_weekly_schedule_omits_closed_monday(self):
        detail = response(DETAIL, (FIXTURES / 'artrabbit_weekly.html').read_text())
        events, _ = parse_detail(detail, SOURCE, DAY, 2, ZONE)
        self.assertEqual([e['start'][:10] for e in events], ['2026-09-27', '2026-09-29'])

    def test_pagination_retains_view_and_date_and_skips_old_detail(self):
        pages = self.pages()
        pages[listing_url(SOURCE['url'], 'just-opened', DAY)] = card('https://example.org/old', '1 Sep 2026 – 2 Sep 2026', 'old') + '<a class="m_pagination-control mod--next" href="?page=2">Next</a>'
        pages[listing_url(SOURCE['url'], 'just-opened', DAY, 2)] = card()
        client = Client(pages)
        result = ArtRabbitAdapter(client).crawl(SOURCE, DAY, 1, ZONE)
        self.assertEqual(len(result['events']), 2)
        self.assertNotIn('https://example.org/old', client.requests)
        self.assertIn(listing_url(SOURCE['url'], 'just-opened', DAY, 2), client.requests)

    def test_repeated_page_is_not_complete(self):
        pages = self.pages()
        pages[listing_url(SOURCE['url'], 'just-opened', DAY)] += '<a class="m_pagination-control mod--next" href="?page=2">Next</a>'
        pages[listing_url(SOURCE['url'], 'just-opened', DAY, 2)] = card()
        with self.assertRaisesRegex(ParseError, 'repeated'):
            ArtRabbitAdapter(Client(pages)).crawl(SOURCE, DAY, 1, ZONE)

    def test_explicit_empty_signal(self):
        pages = {listing_url(SOURCE['url'], v, DAY): EMPTY for v in ('just-opened', 'upcoming')}
        self.assertEqual(ArtRabbitAdapter(Client(pages)).crawl(SOURCE, DAY, 1, ZONE)['source']['state'], 'empty_verified')

    def test_changed_structure_fails(self):
        pages = self.pages()
        pages[listing_url(SOURCE['url'], 'just-opened', DAY)] = '<h1>Error</h1>'
        with self.assertRaises(ParseError):
            ArtRabbitAdapter(Client(pages)).crawl(SOURCE, DAY, 1, ZONE)

    def test_invalid_detail_is_not_silently_empty(self):
        pages = self.pages()
        pages[DETAIL] = '<h1>No structured event</h1>'
        self.assertEqual(ArtRabbitAdapter(Client(pages)).crawl(SOURCE, DAY, 1, ZONE)['source']['state'], 'validation_failed')

    def test_shared_browser_fallback_closes_session(self):
        pages = self.pages()
        class Browser(Client):
            closed = False
            def __enter__(self): return self
            def __exit__(self, *args): self.closed = True
            def get(self, url, selector): return super().get(url)
        browser = Browser(pages)
        class Blocked(Client):
            def get(self, url): raise FetchError(f'HTTP {HTTPStatus.FORBIDDEN.value} for {url}')
            def rendered_session(self): return browser
        result = ArtRabbitAdapter(Blocked(pages)).crawl(SOURCE, DAY, 1, ZONE)
        self.assertEqual(result['source']['state'], 'ok')
        self.assertTrue(browser.closed)
