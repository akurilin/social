"""Read dated fair listings from Art-Collecting's New York guide."""
from __future__ import annotations

import calendar
import datetime as dt
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.art_event_days import daily_events
from crawler.contracts import ParseError, source_result

ADAPTER_ID = 'art_collecting'
PARSER_VERSION = 'art-collecting-html-v2'
MONTHS = {name: number for number, name in enumerate(calendar.month_name) if name}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_RANGE = re.compile(
    rf'\b(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})'
    rf'(?:,?\s+(?P<year1>20\d{{2}}))?'
    rf'(?:\s*[-–—]\s*(?:(?P<month2>{MONTH_PATTERN})\s+)?(?P<day2>\d{{1,2}}))?'
    rf',?\s+(?P<year>20\d{{2}})\b')
NYC_LOCATIONS = ('New York, NY', 'Brooklyn, NY', 'Queens, NY', 'Bronx, NY', 'Staten Island, NY')


def date_ranges(text):
    """Read each printed range, including separate weeks and year boundaries."""
    result = []
    for match in DATE_RANGE.finditer(text):
        fields = match.groupdict()
        month = MONTHS[fields['month']]
        last_month = MONTHS.get(fields['month2'], month)
        year = int(fields['year'])
        first_year = int(fields['year1']) if fields['year1'] else year - (last_month < month)
        result.append((dt.date(first_year, month, int(fields['day'])),
                       dt.date(year, last_month, int(fields['day2'] or fields['day']))))
    return result


class ArtCollectingAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client, self.artifacts = client, artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        response = self.client.get(source['url'])
        if self.artifacts is not None:
            self.artifacts.save_response('listing_html', 'listing.html', response)
        soup = BeautifulSoup(response.text, 'html.parser')
        main = soup.select_one('main.content-main')
        if main is None or main.select_one('#main-top') is None:
            raise ParseError('Art-Collecting guide structure missing')
        events, rejections, errors, seen = [], [], [], set()
        blocks = list(main.select('#main-top > span, #other_nyc_art_fairs > span'))
        # Enhanced features can contain fairs absent from the short index.
        blocks.extend(main.select('[aria-label="Enhanced Feature"]'))
        if not blocks:
            raise ParseError('Art-Collecting guide has no fair blocks')
        dated_count = 0
        for block in blocks:
            text = block.get_text(' ', strip=True)
            enhanced = block.get('aria-label') == 'Enhanced Feature'
            links = block.select('a[href]')
            link = next((a for a in reversed(links) if a.get_text(strip=True).startswith('http')), None) if enhanced else block.select_one('a[href]')
            heading = block.select_one('strong') if enhanced else link
            if link is None or heading is None:
                errors.append('fair title or website missing')
                rejections.append({'reason': 'malformed_fair', 'raw': {'listing_text': text}})
                continue
            try:
                ranges = date_ranges(text)
            except ValueError as error:
                errors.append(str(error))
                rejections.append({'reason': 'invalid_date', 'raw': {'listing_text': text}})
                continue
            if not ranges:
                if re.search(r'\b(?:TBD|TBA)\b', text, re.I):
                    rejections.append({'reason': 'date_unannounced', 'raw': {'listing_text': text}})
                else:
                    errors.append('fair date missing')
                    rejections.append({'reason': 'invalid_date', 'raw': {'listing_text': text}})
                continue
            dated_count += 1
            if not any(city in text for city in NYC_LOCATIONS):
                rejections.append({'reason': 'outside_location', 'raw': {'listing_text': text}})
                continue
            url = urljoin(response.url, link['href'])
            record = {'id': url, 'title': heading.get_text(' ', strip=True), 'url': url,
                      'description': text if enhanced else '', 'evidence': {'listing_text': text}}
            for first, last in ranges:
                key = (url, first, last)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    days = daily_events(record, first, last, source, response, self.version,
                                        seen_date, lookahead_days, timezone)
                except ValueError as error:
                    errors.append(str(error))
                    rejections.append({'reason': 'invalid_date', 'raw': {'listing_text': text}})
                    continue
                events.extend(days)
                if not days:
                    rejections.append({'reason': 'outside_date_window', 'raw': {'url': url, 'dates': [first.isoformat(), last.isoformat()]}})
        state = 'validation_failed' if errors else 'ok' if events else 'empty_verified' if dated_count else 'empty_suspicious'
        return source_result(state=state, method='python_adapter', recipe_version=self.version,
                             started_at=started, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                             events=events, rejections=rejections,
                             artifacts=self.artifacts.items if self.artifacts is not None else [],
                             detail=f'{len(seen)} fair ranges; {len(events)} daily events. Hours are not published in this guide.',
                             error='; '.join(errors) or None)
