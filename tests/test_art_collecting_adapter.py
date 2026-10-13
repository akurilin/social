"""Fixture checks for fair ranges and factual exclusions."""
import datetime as dt
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from crawler.adapters.art_collecting import ArtCollectingAdapter, date_ranges
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError
from tools.events_store import event_key

FIXTURES = Path(__file__).parent / 'fixtures' / 'art_sources'
SOURCE = {'id': 'art-collecting-nyc', 'url': 'https://art-collecting.com/newyork_art_fairs.htm'}


class Client:
    def __init__(self, text):
        self.text = text

    def get(self, url):
        return HttpResponse(url, HTTPStatus.OK, self.text.encode(), {}, '2026-09-27T12:00:00-04:00')


class ArtCollectingTests(unittest.TestCase):
    def crawl(self, text, date=dt.date(2026, 9, 27), days=1):
        return ArtCollectingAdapter(Client(text)).crawl(SOURCE, date, days, 'America/New_York')

    def test_daily_events_survive_dedup_and_keep_evidence(self):
        text = (FIXTURES / 'art_collecting.html').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            result = ArtCollectingAdapter(Client(text), ArtifactRecorder(tmp)).crawl(
                SOURCE, dt.date(2026, 9, 27), 1, 'America/New_York')
            artifact = result['source']['artifacts'][0]
            self.assertTrue(Path(artifact['path']).exists())
        events = result['events']
        self.assertEqual(result['source']['state'], 'ok')
        self.assertEqual(len(events), 4)
        spring = [e for e in events if 'Spring/Break' in e['title']]
        self.assertEqual([e['start'][:10] for e in spring], ['2026-09-27', '2026-09-28'])
        self.assertNotEqual(event_key(spring[0]), event_key(spring[1]))
        self.assertNotEqual(spring[0]['source_event_id'], spring[1]['source_event_id'])
        self.assertEqual(spring[0]['content_hash'], artifact['sha256'])
        self.assertEqual(spring[0]['extracted_json']['time_precision'], 'date')
        self.assertIn('Hours not published', spring[0]['description'])
        self.assertEqual({r['reason'] for r in result['rejections']}, {'outside_location', 'date_unannounced'})

    def test_rejections_use_store_compatible_objects(self):
        result = self.crawl((FIXTURES / 'art_collecting.html').read_text())
        for rejection in result['rejections']:
            self.assertIsInstance(rejection['raw'], dict)

    def test_range_formats(self):
        for text, expected in [
            ('September 24 - 27, 2026', ('2026-09-24', '2026-09-27')),
            ('September 30 – October 3, 2026', ('2026-09-30', '2026-10-03')),
            ('December 30, 2026 - January 2, 2027', ('2026-12-30', '2027-01-02')),
            ('September 24, 2026', ('2026-09-24', '2026-09-24')),
        ]:
            with self.subTest(text=text):
                self.assertEqual(tuple(d.isoformat() for d in date_ranges(text)[0]), expected)
        self.assertEqual(len(date_ranges('September 17 - 20, 2026 September 24 - 27, 2026')), 2)

    def test_complete_guide_outside_window_is_empty(self):
        result = self.crawl((FIXTURES / 'art_collecting.html').read_text(), dt.date(2026, 10, 1))
        self.assertEqual(result['source']['state'], 'empty_verified')

    def test_changed_structure_fails(self):
        with self.assertRaises(ParseError):
            self.crawl('<h1>Unexpected page</h1>')

    def test_bad_date_is_validation_failure(self):
        text = (FIXTURES / 'art_collecting.html').read_text().replace('September 24 - 27', 'September 31 - 32')
        self.assertEqual(self.crawl(text)['source']['state'], 'validation_failed')

    def test_two_weeks_do_not_fill_gap(self):
        text = '<main class="content-main"><section id="main-top"><span><a href="https://example.org/fair">Fair</a> (New York, NY)<br>September 17 - 20, 2026<br>September 24 - 27, 2026</span></section></main>'
        result = self.crawl(text, dt.date(2026, 9, 20), 5)
        self.assertEqual([e['start'][:10] for e in result['events']], ['2026-09-20', '2026-09-24', '2026-09-25'])
