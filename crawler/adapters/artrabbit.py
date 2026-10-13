"""Read ArtRabbit's current and upcoming New York calendars."""
from __future__ import annotations

import calendar
from contextlib import ExitStack
import datetime as dt
from http import HTTPStatus
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from crawler.art_event_days import daily_events
from crawler.contracts import FetchError, ParseError, RateLimited, source_result
from crawler.jsonld import HtmlDocument, find_typed_nodes, load_json_ld

ADAPTER_ID = 'artrabbit'
PARSER_VERSION = 'artrabbit-html-jsonld-v2'
MAX_LISTING_PAGES = 100
LEAP_REFERENCE_YEAR = 2000
VIEWS = ('just-opened', 'upcoming')
DATE = re.compile(r'\b\d{1,2} [A-Z][a-z]{2} 20\d{2}\b')
HOURS = re.compile(r'(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})')
NYC_LOCALITIES = {'new york', 'new york city', 'manhattan', 'brooklyn', 'queens', 'bronx', 'the bronx', 'staten island'}
WEEKDAYS = {name.lower(): day for day, name in enumerate(calendar.day_name)}
EMPTY = re.compile(r'\b(?:no (?:events|listings|results)(?: found)?|0 results)\b', re.I)


def listing_url(base, view, date, page=1):
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query))
    query.update({'view': view, 'start-date': date.isoformat(), 'page': str(page)})
    return urlunsplit(parts._replace(query=urlencode(query)))


def regular_hours(soup, first, last):
    """Read weekly or date-specific hours; never copy opening-night hours."""
    container = soup.select_one('.openingHours')
    if container is None:
        return None, None
    weekly, dated = {}, {}
    has_regular = any(h.get_text(strip=True) == 'Regular hours' for h in container.select('h4'))
    for label in container.select('dt'):
        value = label.find_next_sibling('dd')
        if value is None:
            raise ParseError('opening-hours row has no value')
        text = value.get_text(' ', strip=True)
        hours = HOURS.findall(text)
        if not hours and text.lower() != 'closed':
            raise ParseError('unrecognised opening-hours value: ' + text)
        key = label.get_text(' ', strip=True)
        weekday = WEEKDAYS.get(key.lower())
        if weekday is not None:
            weekly[weekday] = hours
            continue
        # Date labels omit the year; match only dates inside the event range.
        try:
            if re.fullmatch(r'\d{1,2}-[A-Za-z]{3}-\d{4}', key):
                day = dt.datetime.strptime(key, '%d-%b-%Y').date()
                if first <= day <= last:
                    dated[day] = hours
                continue
            parsed = dt.datetime.strptime(key.split(',')[-1].strip() + f' {LEAP_REFERENCE_YEAR}', '%d %b %Y')
            matched = False
            for year in range(first.year, last.year + 1):
                day = dt.date(year, parsed.month, parsed.day)
                if first <= day <= last:
                    dated[day] = hours
                    matched = True
            if not matched:
                raise ValueError('hours outside event dates')
        except ValueError as error:
            raise ParseError('unrecognised opening-hours date: ' + key) from error
    if not has_regular:
        # Special dates do not establish whether other dates are open or closed.
        weekly = {day: [(None, None)] for day in WEEKDAYS.values()}
    if not weekly and not dated:
        raise ParseError('empty opening-hours table')
    return weekly or None, dated or None


def visible_event(soup):
    """Use visible event facts when the page omits its JSON-LD block."""
    title = soup.select_one('h1 [property="name"]')
    date_node = soup.select_one('h3.mod--colour')
    place = soup.select_one('[property="location"]')
    if title is None or date_node is None or place is None:
        raise ParseError('ArtRabbit detail lacks structured or visible event facts')
    dates = [dt.datetime.strptime(value, '%d %b %Y').date().isoformat()
             for value in DATE.findall(date_node.get_text(' ', strip=True))]
    if len(dates) not in (1, 2):
        raise ParseError('ArtRabbit visible date range missing')
    name = place.select_one('[property="name"]')
    address = place.select_one('[property="address"] ul')
    description = soup.select_one('meta[property="og:description"]')
    return {'name': title.get_text(' ', strip=True), 'startDate': dates[0], 'endDate': dates[-1],
            'location': {'name': name.get('content', '') if name else '',
                         'address': address.get_text(' ', strip=True) if address else ''},
            'description': description.get('content', '') if description else '',
            'extraction_method': 'visible_html'}


def parse_detail(response, source, seen_date, lookahead_days, timezone):
    document = HtmlDocument(response.text)
    nodes = [node for block in load_json_ld(document) for node in find_typed_nodes(block, 'Event')]
    if not nodes and any(find_typed_nodes(block, 'EventSeries') for block in load_json_ld(document)):
        return [], 'event_collection'
    soup = BeautifulSoup(response.text, 'html.parser')
    if nodes:
        # Match the page heading so an embedded related event cannot supply its facts.
        heading = soup.select_one('h1 [property="name"]')
        if heading:
            title = heading.get_text(' ', strip=True).casefold()
            nodes = [node for node in nodes if str(node.get('name') or '').strip().casefold() == title]
        if len(nodes) != 1:
            raise ParseError('ArtRabbit detail must identify one matching Event')
    raw = nodes[0] if nodes else visible_event(soup)
    if raw.get('eventAttendanceMode', '').endswith('OnlineEventAttendanceMode'):
        return [], 'online_event'
    first = dt.date.fromisoformat(raw['startDate'][:10])
    last = dt.date.fromisoformat(raw.get('endDate', raw['startDate'])[:10])
    if not raw.get('name'):
        raise ParseError('ArtRabbit event title missing')
    weekly, dated = regular_hours(soup, first, last)
    place = raw.get('location') or {}
    address = place.get('address') or {}
    if isinstance(address, dict):
        city = str(address.get('addressLocality') or '').strip().lower()
        if city and city not in NYC_LOCALITIES:
            return [], 'outside_location'
    address_text = ', '.join(str(address[k]) for k in ('streetAddress', 'addressLocality', 'postalCode', 'addressCountry') if address.get(k)) if isinstance(address, dict) else str(address)
    link = next((a for a in soup.select('a[href]') if a.get_text(' ', strip=True) == 'Event website'), None)
    price_heading = next((h for h in soup.select('h4') if h.get_text(strip=True) == 'Cost of entry'), None)
    price = price_heading.parent.get_text(' ', strip=True).removeprefix('Cost of entry').strip() if price_heading else ''
    record = {'id': urlsplit(response.url).path, 'title': raw['name'], 'url': response.url,
              'signup_url': urljoin(response.url, link['href']) if link else response.url,
              'venue': place.get('name', ''), 'address': address_text,
              'price': price, 'is_free': True if price.lower() in ('free', 'free entry') else None,
              'description': BeautifulSoup(raw.get('description', ''), 'html.parser').get_text(' ', strip=True),
              'evidence': {'jsonld': raw, 'regular_hours': str(soup.select_one('.openingHours') or '')}}
    events = daily_events(record, first, last, source, response, PARSER_VERSION,
                          seen_date, lookahead_days, timezone, weekly=weekly, dated=dated)
    status = str(raw.get('eventStatus', ''))
    for event in events:
        event['status'] = 'cancelled' if status.endswith('EventCancelled') else 'postponed' if status.endswith('EventPostponed') else 'active'
    return events, 'outside_date_window_or_closed' if not events else None


class ArtRabbitAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client, self.artifacts = client, artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        final = seen_date + dt.timedelta(days=lookahead_days)
        events, rejections, errors, seen = [], [], [], set()
        with ExitStack() as stack:
            browser = None

            def fetch(url, selector):
                nonlocal browser
                if browser is not None:
                    return browser.get(url, selector)
                try:
                    return self.client.get(url)
                except RateLimited:
                    raise
                except FetchError as error:
                    if f'HTTP {HTTPStatus.FORBIDDEN.value} ' not in str(error):
                        raise
                    # Use the shared browser with the same robots and rate limits.
                    browser = stack.enter_context(self.client.rendered_session())
                    return browser.get(url, selector)

            for view in VIEWS:
                pages_seen = set()
                for page in range(1, MAX_LISTING_PAGES + 1):
                    url = listing_url(source['url'], view, seen_date, page)
                    response = fetch(url, '.m_result-text')
                    if self.artifacts is not None:
                        self.artifacts.save_response('listing_html', f'{view}-{page}.html', response)
                    soup = BeautifulSoup(response.text, 'html.parser')
                    cards = soup.select('article.m_listing-item')
                    if not cards:
                        result_text = soup.select_one('.m_result-text')
                        if result_text and EMPTY.search(result_text.get_text(' ', strip=True)):
                            break
                        raise ParseError('ArtRabbit listing has no event cards or empty signal')
                    fingerprint = tuple(card.get('data-ident') or str(card.select_one('a.m_listing-link')) for card in cards)
                    if fingerprint in pages_seen:
                        raise ParseError('ArtRabbit pagination repeated a page')
                    pages_seen.add(fingerprint)
                    beyond_window = True
                    for card in cards:
                        link = card.select_one('a.m_listing-link[href]')
                        dates = card.select_one('.b_small-heading.mod--colour')
                        try:
                            if link is None or dates is None:
                                raise ParseError('listing event link or dates missing')
                            parsed = [dt.datetime.strptime(value, '%d %b %Y').date() for value in DATE.findall(dates.get_text(' ', strip=True))]
                            if len(parsed) not in (1, 2):
                                raise ParseError('listing date range missing')
                            first, last = parsed[0], parsed[-1]
                            if last < first:
                                raise ParseError('listing range is reversed')
                            beyond_window = beyond_window and first > final
                            event_url = urljoin(response.url, link['href'])
                            if event_url in seen:
                                continue
                            seen.add(event_url)
                            if last < seen_date or first > final:
                                rejections.append({'reason': 'outside_date_window', 'raw': {'url': event_url, 'dates': [d.isoformat() for d in parsed]}})
                                continue
                            detail = fetch(event_url, 'h1')
                            if self.artifacts is not None:
                                self.artifacts.save_response('detail_html', f'detail-{len(seen)}.html', detail)
                            days, reason = parse_detail(detail, source, seen_date, lookahead_days, timezone)
                            events.extend(days)
                            if reason:
                                rejections.append({'reason': reason, 'raw': {'url': event_url}})
                        except RateLimited:
                            raise
                        except (ParseError, FetchError, ValueError, KeyError, TypeError) as error:
                            beyond_window = False
                            errors.append(str(error))
                            rejections.append({'reason': 'invalid_event', 'raw': {'listing': card.get_text(' ', strip=True), 'error': str(error)}})
                    next_link = soup.select_one('.m_pagination-control.mod--next:not(.disabled)')
                    if next_link is None or (view == 'upcoming' and beyond_window):
                        break
                    next_page = dict(parse_qsl(urlsplit(next_link.get('href', '')).query)).get('page')
                    if next_page != str(page + 1):
                        raise ParseError('ArtRabbit next-page link is invalid')
                else:
                    raise ParseError('ArtRabbit listing page limit reached')
        return source_result(state='validation_failed' if errors else 'ok' if events else 'empty_verified',
                             method='python_adapter', recipe_version=self.version, started_at=started,
                             finished_at=dt.datetime.now(dt.timezone.utc).isoformat(), events=events,
                             rejections=rejections, artifacts=self.artifacts.items if self.artifacts is not None else [],
                             detail=f'{len(seen)} listings; {len(events)} daily events. Current and upcoming pages checked.',
                             error='; '.join(errors[:5]) or None)
