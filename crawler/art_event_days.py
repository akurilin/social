"""Expand art-show date ranges before the crawl window removes past starts."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ONE_DAY = dt.timedelta(days=1)
UNKNOWN_HOURS = 'Hours not published; confirm opening days and times with the organiser.'


def daily_slots(first, last, seen_date, lookahead_days, timezone, *, weekly=None, dated=None):
    """Return local visit slots; a published schedule excludes closed days."""
    if last < first:
        raise ValueError('event ends before it starts')
    zone = ZoneInfo(timezone)
    day = max(first, seen_date)
    final = min(last, seen_date + dt.timedelta(days=lookahead_days))
    result = []
    while day <= final:
        if dated is not None and day in dated:
            hours = dated[day]
        elif weekly is not None:
            hours = weekly.get(day.weekday(), [])
        elif dated is not None:
            hours = []
        else:
            hours = [(None, None)]
        for opening, closing in hours:
            start = dt.datetime.combine(day, dt.time.fromisoformat(opening) if opening else dt.time(), zone)
            end = dt.datetime.combine(day, dt.time.fromisoformat(closing), zone) if closing else None
            if end is not None and end <= start:
                end += ONE_DAY
            result.append((start.isoformat(), end.isoformat() if end else None))
        day += ONE_DAY
    return result


def daily_events(record, first, last, source, response, version, seen_date,
                 lookahead_days, timezone, *, weekly=None, dated=None):
    """Keep date-specific IDs and evidence so repeated crawls merge each day."""
    events = []
    for start, end in daily_slots(first, last, seen_date, lookahead_days, timezone,
                                 weekly=weekly, dated=dated):
        known_hours = end is not None
        evidence = dict(record.get('evidence', {}), date_range=[first.isoformat(), last.isoformat()],
                        occurrence_date=start[:10], time_precision='time' if known_hours else 'date')
        description = record.get('description', '')
        if not known_hours:
            description = (description + '\n' + UNKNOWN_HOURS).strip()
        events.append({
            'title': record['title'], 'start': start, 'end': end,
            'url': record['url'], 'signup_url': record.get('signup_url', record['url']),
            'host': record.get('host', ''), 'venue': record.get('venue', ''),
            'neighborhood': '', 'address': record.get('address', ''),
            'price': record.get('price', ''), 'is_free': record.get('is_free'),
            'description': description, 'capacity_flag': None, 'status': 'active',
            'source_id': source['id'], 'source_listing_url': source['url'],
            'source_url': response.url, 'source_event_id': record['id'] + ':' + start[:10] + ':' + start[11:16],
            'fetched_at': response.fetched_at, 'parser_version': version,
            'content_hash': response.content_hash, 'extracted_json': evidence,
        })
    return events
