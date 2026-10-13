import datetime as dt
from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import unittest

from crawler.adapters.nyc_culture_calendar import ADAPTER_ID, NYCCultureCalendarAdapter
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError, RawEvent
from crawler.registry import get_adapter

FIXTURES = Path(__file__).parent / 'fixtures' / 'nyc_culture_calendar'
SOURCE = {'id': 'nyc-culture-calendar', 'url': 'https://www.nyc-culture-calendar.com/'}
SEEN = dt.date(2026, 9, 7)


class Client:
    def __init__(self):
        self.events = json.loads((FIXTURES / 'events.json').read_text())
        self.venues = json.loads((FIXTURES / 'venues.json').read_text())

    def get(self, url):
        data = self.events if url.endswith('events.json') else self.venues
        return HttpResponse(url, HTTPStatus.OK, json.dumps(data).encode(),
                            {'content-type': 'application/json'}, '2026-09-07T12:00:00-04:00')


class CalendarTests(unittest.TestCase):
    def run_adapter(self, client, artifacts=None, days=0):
        return NYCCultureCalendarAdapter(client, artifacts).crawl(SOURCE, SEEN, days, 'America/New_York')

    def test_normal_fields_and_saved_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_adapter(Client(), ArtifactRecorder(tmp))
            self.assertEqual(result['source']['state'], 'ok')
            self.assertEqual(len(result['source']['artifacts']), 2)
            for artifact in result['source']['artifacts']:
                self.assertTrue(Path(artifact['path']).exists())
                self.assertTrue(artifact['sha256'])
        event = RawEvent.model_validate(result['events'][0])
        self.assertEqual(event.start, '2026-09-07T19:00:00-04:00')
        self.assertEqual(event.venue, 'Barbès')
        self.assertIn('376 9th St', event.address)
        self.assertEqual(event.signup_url, event.url)
        self.assertIsNone(event.is_free)
        self.assertIs(get_adapter(ADAPTER_ID), NYCCultureCalendarAdapter)

    def test_inclusive_window(self):
        client = Client()
        first = client.events['events'][0]
        client.events['events'] = [dict(first, id=str(day), start=f'2026-09-{day:02d}T19:00:00', end=None)
                                   for day in (6, 7, 8, 9)]
        client.events['count'] = 4
        result = self.run_adapter(client, days=1)
        self.assertEqual([e['source_event_id'] for e in result['events']], ['7', '8'])
        self.assertEqual(len(result['rejections']), 2)

    def test_explicit_empty(self):
        client = Client()
        client.events.update(count=0, events=[])
        self.assertEqual(self.run_adapter(client)['source']['state'], 'empty_verified')

    def test_short_feed_is_not_verified_empty(self):
        client = Client()
        for event in client.events['events']:
            event['start'] = '2026-09-06T19:00:00'
        self.assertEqual(self.run_adapter(client)['source']['state'], 'empty_suspicious')

    def test_malformed_structure(self):
        for payload in ({}, {'count': 0, 'events': [None]}, []):
            client = Client()
            client.events = payload
            with self.assertRaises(ParseError):
                self.run_adapter(client)

    def test_missing_required_event_field(self):
        client = Client()
        del client.events['events'][0]['title']
        self.assertEqual(self.run_adapter(client)['source']['state'], 'validation_failed')

    def test_offsite_location_does_not_inherit_address(self):
        client = Client()
        client.events['events'][0]['venue_name'] = 'Other room'
        event = self.run_adapter(client)['events'][0]
        self.assertEqual(event['venue'], 'Other room')
        self.assertEqual(event['address'], '')
        self.assertEqual(event['neighborhood'], '')
