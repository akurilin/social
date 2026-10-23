"""Check public API pagination without storing anonymous session data."""
import datetime as dt
import json
import unittest
from unittest.mock import patch
from http import HTTPStatus
from crawler.adapters.book_club_bar import BookClubBarAdapter
from crawler.contracts import HttpResponse, ParseError, RobotsDenied
from crawler.http import HttpClient

SOURCE = {'id':'book-club-bar','url':'https://www.bookclubbar.com/events'}
DAY = dt.date(2026,9,27)
ZONE = 'America/New_York'

def row(event_id=1):
    return {'id':event_id,'title':'Reading','date':'20260927','original_date':'20260927',
            'start_time':'18:00:00','end_time':'20:00:00','all_day':False,
            'location_text':'East Village, 197 E 3rd Street','description':'<p>This is a free event.</p>',
            'summary':'Reading','end_date':'','recurring_as_single_row':0}

class Client:
    def __init__(self,pages):
        self.pages=pages
        self.requests=[]
    def response(self,url,value):
        return HttpResponse(url,HTTPStatus.OK,json.dumps(value).encode(),{},'2026-09-27T12:00:00-04:00')
    def get(self,url):
        return HttpResponse(url,HTTPStatus.OK,b'<script>var san="9911545";</script>',{},'2026-09-27T12:00:00-04:00')
    def post_form(self,url,data):
        self.requests.append((url,dict(data)))
        if url.endswith('store/getSettings'): return self.response(url,{'store_info':{'id':123},'using_events_v2':True})
        if url.endswith('session/get'): return self.response(url,{'session_id':'test-private-session'})
        return self.response(url,self.pages[data['offset']])

class BookClubBarAPITests(unittest.TestCase):
    def test_form_post_is_guarded_and_encoded(self):
        client=HttpClient()
        with patch.object(client,'_ensure_allowed') as guard, patch.object(client,'_request_with_retry') as request:
            client.post_form('https://example.com/customer/',{'title':'A & B'})
            guard.assert_called_once()
            self.assertEqual(request.call_args.kwargs['body'],b'title=A+%26+B')
        with patch.object(client,'_can_fetch',return_value=False):
            with self.assertRaises(RobotsDenied): client.post_form('https://example.com/customer/',{})

    def test_api_reads_until_empty_page_and_keeps_no_session(self):
        client=Client({0:{'rows':[row()]},1:{'rows':[]}})
        result=BookClubBarAdapter(client).crawl(SOURCE,DAY,14,ZONE)
        self.assertEqual(result['source']['state'],'ok')
        event=result['events'][0]
        self.assertEqual(event['start'],'2026-09-27T18:00:00-04:00')
        self.assertEqual(event['url'],SOURCE['url']+'/1')
        self.assertTrue(event['is_free'])
        self.assertNotIn('test-private-session',json.dumps(result))
        self.assertEqual(client.requests[-1][1]['offset'],1)

    def test_repeated_page_fails(self):
        client=Client({0:{'rows':[row()]},1:{'rows':[row()]}})
        with self.assertRaisesRegex(ParseError,'duplicate'):
            BookClubBarAdapter(client).crawl(SOURCE,DAY,14,ZONE)

    def test_explicit_empty_rows(self):
        result=BookClubBarAdapter(Client({0:{'rows':[]}})).crawl(SOURCE,DAY,14,ZONE)
        self.assertEqual(result['source']['state'],'empty_verified')

    def test_ticket_link_is_url_and_room_has_full_address(self):
        event=row(3);event.update({'date':'20261108','location_text':'Bushwick, 380 Troutman Street',
            'description':'<p><a href="https://www.eventbrite.com/e/123">Get tickets</a></p>'})
        result=BookClubBarAdapter(Client({0:{'rows':[row(),event]},2:{'rows':[]}})).crawl(SOURCE,DAY,45,ZONE)
        plain,ticketed=result['events']
        self.assertEqual((plain['url'],plain['signup_url']),(SOURCE['url']+'/1',''))
        self.assertEqual(plain['address'],'197 East 3rd Street, New York, NY')
        self.assertEqual(ticketed['url'],'https://www.eventbrite.com/e/123')
        self.assertEqual(ticketed['signup_url'],ticketed['url'])
        self.assertEqual((ticketed['venue'],ticketed['neighborhood']),('Book Club Bar Bushwick','Bushwick'))
        self.assertEqual(ticketed['address'],'380 Troutman Street, Brooklyn, NY')
        # November 8 is after the DST change, so the offset moves to -05:00.
        self.assertEqual(ticketed['start'],'2026-11-08T18:00:00-05:00')

    def test_all_day_row_is_rejected_without_failing_the_source(self):
        event=row(2);event.update({'all_day':True,'start_time':'11:00:00','end_time':'00:00:00','location_text':''})
        result=BookClubBarAdapter(Client({0:{'rows':[row(),event]},2:{'rows':[]}})).crawl(SOURCE,DAY,14,ZONE)
        self.assertEqual(result['source']['state'],'ok')
        self.assertEqual(len(result['events']),1)
        self.assertEqual([r['reason'] for r in result['rejections']],['missing_explicit_time'])

    def test_missing_clock_is_not_midnight(self):
        event=row();event['start_time']=''
        result=BookClubBarAdapter(Client({0:{'rows':[event]},1:{'rows':[]}})).crawl(SOURCE,DAY,14,ZONE)
        self.assertEqual(result['source']['state'],'validation_failed')
