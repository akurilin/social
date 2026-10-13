"""Keep the September run failures reproducible without network access."""
import datetime as dt
import json
import unittest
from pathlib import Path
from bs4 import BeautifulSoup
from crawler.adapters import analog_film_nyc as analog, bored_dating as boda
from crawler.adapters import gather_community as gather, luma, othership, new_york_social_network as nysn
from crawler.contracts import HttpResponse, EventStub, ParseError

FIXTURES = Path(__file__).parent / 'fixtures'
DAY = dt.date(2026, 9, 27)
ZONE = 'America/New_York'

class Client:
    def __init__(self, body):
        self.body = body
    def get(self, url):
        return HttpResponse(url=url, status=200, body=self.body.encode(), headers={},
                            fetched_at='2026-09-27T12:00:00-04:00')

class RunRegressionTests(unittest.TestCase):
    def test_gather_uses_stable_endpoint(self):
        self.assertEqual(gather.DEFAULT_API_URL, 'https://www.57.nyc/api/getPosts')

    def test_analog_all_paragraphs_and_showtimes(self):
        body = '''<main><h2>Sunday, September 27th:</h2>
        <p><strong>1:00 PM – 4:00 PM</strong><br><em>First</em><br>Metrograph – <a href="https://example.com/first">Tickets</a></p>
        <hr><p><strong>7:00 PM</strong><br><em>Second</em><br>Metrograph – <a href="https://example.com/second">Tickets</a></p>
        <h2>Monday, September 28th:</h2><p>No screenings.</p></main>'''
        result = analog.AnalogFilmNYCAdapter(Client(body)).crawl(
            {'id':'film', 'url':'https://analogfilmnyc.org/upcoming-screenings/'}, DAY, 1, ZONE)
        self.assertEqual([e['start'][11:16] for e in result['events']], ['13:00','16:00','19:00'])
        self.assertEqual(len({e['source_event_id'] for e in result['events']}), 3)

    def test_analog_plain_title_with_explicit_film_metadata(self):
        body = '<main><h2>Sunday, September 27th:</h2><p>12:00 PM<br>Secret Hong Kong 35mm Feature (????) dir. ??? – 89 min.<br>Nitehawk – <a href="https://example.com/film">Tickets</a></p></main>'
        result = analog.AnalogFilmNYCAdapter(Client(body)).crawl(
            {'id':'film','url':'https://analogfilmnyc.org/upcoming-screenings/'},DAY,0,ZONE)
        self.assertEqual(result['events'][0]['title'],'Secret Hong Kong 35mm Feature')

    def test_boda_full_address_without_city(self):
        record = {'location':{'address':{'addressLocality':None,
                  'streetAddress':'380 Troutman St, Brooklyn, NY 11237, USA'}}}
        self.assertTrue(boda._is_nyc(record))
        record['location']['address']['streetAddress'] = '12 Brooklyn Road, London, UK'
        with self.assertRaises(ParseError):
            boda._is_nyc(record)

    def test_luma_complete_embedded_collection(self):
        body = (FIXTURES/'luma_calendar.html').read_text()
        soup = BeautifulSoup(body, 'html.parser')
        nodes = [n for v in luma.load_json_ld(luma.HtmlDocument(body)) for n in luma.find_typed_nodes(v,'Event')]
        entries = [{'event':{'url':n['url'].rstrip('/').split('/')[-1], 'start_at':n['startDate']}} for n in nodes]
        data = {'props':{'pageProps':{'initialData':{'data':{'upcoming':{'entries':entries,'has_more':False}}}}}}
        body += '<script id="__NEXT_DATA__" type="application/json">'+json.dumps(data)+'</script>'
        result = luma.LumaCalendarAdapter(Client(body)).crawl({'id':'luma','url':'https://lu.ma/test'},dt.date(2026,9,2),2,ZONE)
        self.assertEqual(result['source']['state'],'empty_verified')
        result = luma.LumaCalendarAdapter(Client(body.replace('"has_more": false','"has_more": true'))).crawl({'id':'luma','url':'https://lu.ma/test'},dt.date(2026,9,2),2,ZONE)
        self.assertEqual(result['source']['state'],'empty_suspicious')

    def test_othership_complete_toronto_collection(self):
        soup = BeautifulSoup((FIXTURES/'othership_listing.html').read_text(),'html.parser')
        cards = soup.select('.events-item')
        for card in cards:
            if card.select_one('.events-location-text').get_text(strip=True) != 'Adelaide': card.decompose()
        result = othership.OthershipEventsAdapter(Client(str(soup))).crawl({'id':'other','url':'https://www.othership.us/events'},DAY,14,ZONE)
        self.assertEqual(result['source']['state'],'empty_verified')

    def test_nysn_external_ticket_url_uses_visible_identity(self):
        from crawler.adapters.new_york_social_network import _parse_event
        url = 'https://newyorksocialnetwork.com/events/walk/'
        raw = {'@type':'Event','url':'https://tickets.example.com/walk','name':'Walk',
               'startDate':'2026-09-27T09:55:00-04:00','location':{'name':'Station'}}
        body = '<script type="application/ld+json">'+json.dumps(raw)+'</script><main><h2 class="text-white font-weight-bold mb-4 text-center">Walk</h2></main>'
        event = _parse_event(Client(body).get(url), EventStub(url=url,title='Walk',date_hint=DAY), {'id':'nysn','url':url},ZONE)
        self.assertEqual(event['url'], 'https://tickets.example.com/walk')

    def test_othership_next_page_prevents_verified_zero(self):
        soup = BeautifulSoup((FIXTURES/'othership_listing.html').read_text(),'html.parser')
        for card in soup.select('.events-item'):
            if card.select_one('.events-location-text').get_text(strip=True) != 'Adelaide':
                card.decompose()
        body = str(soup) + '<a class="w-pagination-next" href="?page=2">Next</a>'
        result = othership.OthershipEventsAdapter(Client(body)).crawl({'id':'other','url':'https://www.othership.us/events'},DAY,14,ZONE)
        self.assertEqual(result['source']['state'],'empty_suspicious')

    def test_nysn_visible_fields_without_structured_address(self):
        url = 'https://newyorksocialnetwork.com/events/walk/'
        raw = {'@type':'Event','url':url,'name':'Walk','startDate':'2026-09-27T09:55:00-04:00','location':{'name':'Station','address':{}}}
        body = '<script type="application/ld+json">'+json.dumps(raw)+'</script>'+'''<main><h1>Walk</h1><div class="event-datetimes"><li class="ee-event-datetimes-li"><span class="ee-event-datetimes-li-daterange">09/27/2026</span><span class="ee-event-datetimes-li-timerange">9:55 am</span></li></div><div class="espresso-venue-dv"><h4>Venue: <span itemprop="name">Station</span></h4></div></main>'''
        event = nysn._parse_event(Client(body).get(url),EventStub(url=url,title='Walk',date_hint=DAY),{'id':'nysn','url':url},ZONE)
        self.assertEqual(event['venue'],'Station')
        self.assertEqual(event['address'],'')
        body = body.replace('"startDate": "2026-09-27T09:55:00-04:00",','')
        event = nysn._parse_event(Client(body).get(url),EventStub(url=url,title='Walk',date_hint=DAY),{'id':'nysn','url':url},ZONE)
        self.assertEqual(event['start'],'2026-09-27T09:55:00-04:00')
