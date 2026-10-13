"""Read Culture Calendar's public event and venue files."""

import datetime as dt
import json
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result

ADAPTER_ID = 'nyc_culture_calendar'
PARSER_VERSION = 'nyc-culture-calendar-v2'
EVENTS_PATH = 'data/events.json'
VENUES_PATH = 'data/venues.json'
METHOD = 'python_adapter'
UNCONFIRMED_TIME = re.compile(
    r'check.{0,60}(?:exact\s+)?show\s*times|show\s*times\s+(?:vary|TBD)|'
    r'(?:time|schedule)\s+(?:TBD|to be confirmed)', re.IGNORECASE)


class NYCCultureCalendarAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        responses = {}
        payloads = {}
        # Save both files so venue facts have the same audit trail as dates.
        for path in (EVENTS_PATH, VENUES_PATH):
            response = self.client.get(urljoin(source['url'], path))
            responses[path] = response
            if self.artifacts is not None:
                self.artifacts.save_response('calendar_json', path.split('/')[-1], response)
            try:
                payloads[path] = json.loads(response.text)
            except ValueError as error:
                raise ParseError('Calendar returned invalid JSON') from error
        feed = payloads[EVENTS_PATH]
        venue_feed = payloads[VENUES_PATH]
        if (not isinstance(feed, dict) or not isinstance(feed.get('events'), list)
                or type(feed.get('count')) is not int
                or feed['count'] != len(feed['events'])
                or not isinstance(feed.get('generated_at'), str)):
            raise ParseError('Calendar event count or structure is invalid')
        try:
            generated_day = _datetime(feed['generated_at'], timezone).date()
        except ValueError as error:
            raise ParseError('Calendar generation time is invalid') from error
        if not isinstance(venue_feed, dict) or not isinstance(venue_feed.get('venues'), list):
            raise ParseError('Calendar venue structure is invalid')
        venues = {}
        for venue in venue_feed['venues']:
            if not isinstance(venue, dict) or not venue.get('id') or not venue.get('name'):
                raise ParseError('Calendar venue is missing its ID or name')
            if venue['id'] in venues:
                raise ParseError('Calendar has duplicate venue IDs')
            venues[venue['id']] = venue
        events, rejections, dates = [], [], []
        failures = 0
        ids = set()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        response = responses[EVENTS_PATH]
        for raw in feed['events']:
            try:
                if not isinstance(raw, dict):
                    raise ValueError('Event is not an object')
                for key in ('id', 'title', 'start', 'url'):
                    if not isinstance(raw.get(key), str) or not raw[key].strip():
                        raise ValueError('Event is missing ' + key)
                if raw['id'] in ids:
                    raise ValueError('Duplicate event ID')
                ids.add(raw['id'])
                start = _datetime(raw['start'], timezone)
                dates.append(start.date())
                if not seen_date <= start.date() <= window_end:
                    rejections.append({'reason': 'outside_date_window', 'raw': raw})
                    continue
                # Do not turn a placeholder feed time into a confirmed screening.
                if UNCONFIRMED_TIME.search(str(raw.get('description') or '')):
                    failures += 1
                    rejections.append({'reason': 'unverified_start_time', 'raw': raw})
                    continue
                if raw.get('all_day'):
                    raise ValueError('All-day event has no confirmed start time')
                end = _datetime(raw['end'], timezone) if raw.get('end') else None
                if end and end < start:
                    raise ValueError('Event end precedes start')
                if urlsplit(raw['url']).scheme not in ('https', 'http') or not urlsplit(raw['url']).netloc:
                    raise ValueError('Invalid event URL')
                venue = venues.get(raw.get('venue_id'), {})
                name = raw.get('venue_name') or venue.get('name', '')
                # An explicit location can be offsite; do not attach the default address.
                default_location = not raw.get('venue_name') or raw['venue_name'] == venue.get('name')
                price = str(raw.get('price') or '')
                events.append(dict(
                    title=raw['title'], start=start.isoformat(), end=end.isoformat() if end else None,
                    url=raw['url'], signup_url=raw['url'], host='', venue=name,
                    neighborhood=venue.get('neighborhood', '') if default_location else '',
                    address=venue.get('address', '') if default_location else '',
                    price=price, is_free=True if price.lower() == 'free' else None,
                    description=raw.get('description') or '', capacity_flag=None, status='active',
                    source_id=source['id'], source_listing_url=source['url'], source_url=response.url,
                    source_event_id=raw['id'], fetched_at=response.fetched_at,
                    parser_version=PARSER_VERSION, content_hash=response.content_hash,
                    extracted_json={'event': raw, 'venue': venue, 'generated_at': feed['generated_at']},
                ))
            except (ValueError, TypeError) as error:
                failures += 1
                rejections.append({'reason': 'event_parse_failed', 'raw': {'event': raw, 'error': str(error)}})
        # Only a same-day empty feed can prove that the requested window is empty.
        covered = dates == sorted(dates) and bool(dates) and max(dates) > window_end
        state = 'validation_failed' if failures else 'ok' if events else (
            'empty_verified' if (feed['count'] == 0 and generated_day == seen_date) or covered
            else 'empty_suspicious')
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            events=events, rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail='Read all {} events; feed generated {}.'.format(feed['count'], feed['generated_at']),
            error='{} events have invalid or unconfirmed facts'.format(failures) if failures else None,
        )


def _datetime(value, timezone):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('Event requires a date and time')
    parsed = dt.datetime.fromisoformat(value)
    zone = ZoneInfo(timezone)
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)
