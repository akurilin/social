"""Reject facts from unrelated content and unconfirmed schedules."""
import datetime as dt
import json
import unittest
from http import HTTPStatus
from crawler.adapters import luma, new_york_social_network as nysn
from crawler.contracts import HttpResponse, EventStub, FetchError
import test_nyc_culture_calendar_adapter as culture

DAY = dt.date(2026, 9, 27)
ZONE = 'America/New_York'
URL = 'https://luma.com/art-night'


def response(url, body):
    return HttpResponse(url, HTTPStatus.OK, body.encode(), {}, '2026-09-27T12:00:00-04:00')


class QualityTests(unittest.TestCase):
    def test_nysn_sidebar_does_not_set_age(self):
        url = 'https://newyorksocialnetwork.com/events/walk/'
        node = {'@type':'Event', 'url':url, 'name':'Walk',
                'description':'A walk for adults.', 'startDate':'2026-09-27T12:00:00-04:00',
                'location':{'name':'Station'}}
        body = '<script type="application/ld+json">'+json.dumps(node)+'</script><aside>Speed Dating Ages 30–45</aside>'
        event = nysn._parse_event(response(url, body), EventStub(url, 'Walk', DAY), {'id':'nysn','url':url}, ZONE)
        self.assertIsNone(event['explicit_age_min'])
        self.assertIsNone(event['explicit_age_max'])

    def test_unconfirmed_culture_showtime_is_rejected(self):
        client = culture.Client()
        client.events['events'][0]['description'] = 'Check site for exact showtimes.'
        result = culture.CalendarTests().run_adapter(client)
        self.assertEqual(result['source']['state'], 'validation_failed')
        self.assertEqual(result['rejections'][0]['reason'], 'unverified_start_time')

    def test_luma_detail_enriches_only_matching_event(self):
        event = {'url':URL, 'title':'Art Night', 'start':'2026-09-27T19:00:00-04:00',
                 'description':'', 'venue':'', 'address':'', 'extracted_json':{}}
        node = {'@type':'Event','url':URL,'name':'Art Night','startDate':event['start'],
                'description':'Meet local artists.', 'location':{'name':'Art Studio','address':'10 Main St'}}
        class Client:
            def get(self, url):
                return response(url, '<script type="application/ld+json">'+json.dumps(node)+'</script>')
        luma.LumaCategoryAdapter(Client())._enrich_event(event, ZONE)
        self.assertEqual(event['description'], 'Meet local artists.')
        self.assertEqual(event['venue'], 'Art Studio')
        node['startDate'] = '2026-09-28T19:00:00-04:00'
        with self.assertRaises(luma.ParseError):
            luma.LumaCategoryAdapter(Client())._enrich_event(event, ZONE)

    def test_luma_failed_detail_keeps_one_candidate_and_warning(self):
        raw = {'api_id':'evt-one','name':'Art Night','start_at':'2026-09-27T23:00:00Z',
               'url':'art-night','geo_address_info':{'city':'New York','region':'New York',
               'country':'United States'}}
        class Client:
            def get(self, url):
                if url.startswith(luma.LUMA_CATEGORY_API):
                    return response(url, json.dumps({'entries':[{'event':raw}], 'has_more':False}))
                raise FetchError('Detail unavailable')
        result = luma.LumaCategoryAdapter(Client()).crawl({'id':'arts','url':'https://luma.com/arts'}, DAY, 0, ZONE)
        self.assertEqual(result['source']['state'], 'validation_failed')
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['rejections'], [])
        self.assertIn('Detail unavailable', result['events'][0]['extracted_json']['detail_error'])

    def test_luma_stops_detail_requests_after_rate_limit(self):
        calls = []
        entries = [{'event':{'api_id':str(i),'name':'Art Night','start_at':'2026-09-27T23:00:00Z',
                    'url':'art-'+str(i),'geo_address_info':{'city':'New York','region':'New York',
                    'country':'United States'}}} for i in range(3)]
        class Client:
            def get(self, url):
                if url.startswith(luma.LUMA_CATEGORY_API):
                    return response(url, json.dumps({'entries':entries, 'has_more':False}))
                calls.append(url)
                raise luma.RateLimited('HTTP 429')
        result = luma.LumaCategoryAdapter(Client()).crawl({'id':'arts','url':'https://luma.com/arts'}, DAY, 0, ZONE)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result['events']), 3)
        self.assertEqual(result['source']['state'], 'validation_failed')
