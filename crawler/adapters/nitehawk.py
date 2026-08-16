"""Procedural adapter for Nitehawk Cinema coming-soon and trivia pages."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import FetchError, ParseError, source_result


ADAPTER_ID = "nitehawk"
PARSER_VERSION = "nitehawk-http-v1"
METHOD = "python_adapter"
_DATE_TIME = re.compile(
    r"\b(?:[A-Za-z]+),?\s+([A-Z][a-z]+)\s+(\d{1,2})\s+"
    r"(\d{1,2}):(\d{2})\s*(am|pm)\b", re.I)
_DATE_ONLY = re.compile(r"\b(?:[A-Za-z]+),?\s+([A-Z][a-z]+)\s+(\d{1,2})\b", re.I)
_TIME_ONLY = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)\b", re.I)
_SHOWTIME_SLUG = re.compile(
    r"-(\d{1,2})-(\d{1,2})-(\d{2}|20\d{2})-(\d{1,2})(\d{2})-(am|pm)(?:/|$)",
    re.I,
)
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December"), 1)}
_MONTHS.update({name[:3].lower(): number for name, number in list(_MONTHS.items())})
_LOCATIONS = (
    ("williamsburg", "https://nitehawkcinema.com/williamsburg/coming-soon/",
     "Nitehawk Cinema Williamsburg", "136 Metropolitan Ave., Brooklyn, NY"),
    ("prospectpark", "https://nitehawkcinema.com/prospectpark/coming-soon-2/",
     "Nitehawk Cinema Prospect Park", "188 Prospect Park West, Brooklyn, NY"),
)
_TRIVIA = "https://nitehawkcinema.com/prospectpark/movie-trivia-nite/"


class NitehawkAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        events, rejections, errors = [], [], []
        listing_count = 0
        for location, url, venue, address in _configured_pages(source):
            try:
                response = self.client.get(url)
                listing_count += 1
                if self.artifacts is not None:
                    self.artifacts.save_response("listing_html", location + "-coming-soon.html", response)
                candidates = _listing_events(response, source, venue, address, seen_date, timezone)
                for event in candidates:
                    if seen_date <= dt.datetime.fromisoformat(event["start"]).date() <= end_date:
                        events.append(event)
                    else:
                        rejections.append({"reason": "outside_date_window", "raw": event})
                if not candidates and not _explicit_empty(response.text):
                    raise ParseError("Nitehawk {} page contained no dated purchase or showtime links".format(location))
            except (FetchError, ParseError) as error:
                errors.append("{}: {}".format(url, error))
        try:
            response = self.client.get(_TRIVIA)
            if self.artifacts is not None:
                self.artifacts.save_response("detail_html", "prospectpark-trivia.html", response)
            candidates = _trivia_events(response, source, seen_date, timezone)
            for event in candidates:
                if seen_date <= dt.datetime.fromisoformat(event["start"]).date() <= end_date:
                    events.append(event)
                else:
                    rejections.append({"reason": "outside_date_window", "raw": event})
            if not candidates and not _explicit_empty(response.text):
                raise ParseError("Nitehawk trivia page contained no dated schedule entries")
        except (FetchError, ParseError) as error:
            errors.append("{}: {}".format(_TRIVIA, error))

        if errors and events:
            state = "validation_failed"
        elif errors:
            state = "parse_failed"
        elif events:
            state = "ok"
        else:
            state = "empty_verified"
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail="Parsed {} Nitehawk listing page(s) and the trivia schedule: {} event(s) in {} through {}."
                   .format(listing_count, len(events), seen_date, end_date),
            error="; ".join(errors[:5]) or None,
        )


def _configured_pages(source):
    # A catalog entry can point at either location, but the source represents both venues.
    return _LOCATIONS


def _listing_events(response, source, venue, address, seen_date, timezone):
    page_url = response.url
    soup = BeautifulSoup(response.text, "html.parser")
    result = []
    seen_occurrences = set()
    for card in soup.select(".show-details"):
        visible = " ".join(card.get_text(" ", strip=True).split())
        date_match = _DATE_TIME.search(visible)
        if not date_match:
            continue
        date = _date(date_match, seen_date)
        title = _title(card)
        if not title:
            continue
        movie_url = _first_url(card, ("/showtimes/", "/movies/")) or page_url
        purchase_links = card.select('a[href*="/purchase/"]')
        if not purchase_links:
            # Some special cards expose only a showtime URL.
            purchase_links = card.select('a[href*="/showtimes/"]')
        for index, link in enumerate(purchase_links):
            signup_url = urljoin(page_url, link.get("href") or movie_url)
            start = _link_start(link, signup_url, date, date_match, seen_date, timezone)
            # Purchase and showtime links identify one fixed screening. A
            # generic movie page can contain many dates and times, so using it
            # as the event URL would collapse distinct screenings in the store.
            event_url = signup_url
            occurrence = (event_url, start)
            if occurrence in seen_occurrences:
                continue
            seen_occurrences.add(occurrence)
            result.append(_event(title, start, event_url, signup_url, visible, source,
                                 venue, address,
                                 "{}-{}".format(_slug(signup_url), index), response))
    return result


def _trivia_events(response, source, seen_date, timezone):
    page_url = response.url
    soup = BeautifulSoup(response.text, "html.parser")
    clean = " ".join(soup.get_text(" ", strip=True).split())
    # The live page puts the location name in its first h1. This adapter uses
    # one fixed trivia endpoint, so its event title is also fixed.
    title = "Movie Trivia Nite"
    result = []
    for match in _DATE_ONLY.finditer(clean):
        date = _date(match, seen_date)
        start = dt.datetime.combine(date, dt.time(20, 0), ZoneInfo(timezone)).isoformat()
        result.append(_event(title, start, page_url, page_url, clean, source,
                             "Nitehawk Cinema Prospect Park", "188 Prospect Park West, Brooklyn, NY",
                             "movie-trivia-{}".format(date.isoformat()), response))
    return result


def _event(title, start, event_url, signup_url, description, source, venue, address,
           event_id, response):
    status = "cancelled" if re.search(r"\bcancel(?:led|ed)\b", description, re.I) else "active"
    return {"title": title, "start": start, "end": None, "url": event_url,
            "signup_url": signup_url, "host": "Nitehawk Cinema", "venue": venue,
            "neighborhood": "", "address": address, "price": "", "is_free": None,
            "description": description, "capacity_flag": None, "status": status,
            "source_id": source["id"], "source_listing_url": source["url"],
            "source_url": response.url, "source_event_id": event_id,
            "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
            "content_hash": response.content_hash, "extracted_json": {}}


def _title(card):
    for selector in ("h2", "h3", ".show-title", "a[href*='/movies/']", "a[href*='/showtimes/']"):
        node = card.select_one(selector)
        if node:
            value = " ".join(node.get_text(" ", strip=True).split())
            if value and value.lower() not in {"watch trailer", "see more"}:
                return value
    return ""


def _first_url(card, parts):
    for link in card.find_all("a", href=True):
        if any(part in link["href"] for part in parts):
            return link["href"]
    return ""


def _date(match, reference):
    month = _MONTHS[match.group(1).lower()]
    year = reference.year
    if month - reference.month > 6:
        year -= 1
    elif reference.month - month > 6:
        year += 1
    return dt.date(year, month, int(match.group(2)))


def _combine(date, match, timezone):
    hour = int(match.group(3) if len(match.groups()) >= 4 else match.group(1))
    minute = int(match.group(4) if len(match.groups()) >= 4 else match.group(2))
    meridiem = (match.group(5) if len(match.groups()) >= 5 else match.group(3)).lower()
    return _at(date, hour, minute, meridiem, timezone)


def _link_start(link, url, fallback_date, fallback_match, seen_date, timezone):
    text = link.get_text(" ", strip=True)
    time_match = _TIME_ONLY.search(text)
    parent = link.find_parent("li")
    raw_epoch = parent.get("data-date") if parent is not None else None
    if raw_epoch and str(raw_epoch).isdigit() and time_match:
        # Nitehawk uses this timestamp as a date selector. Its UTC date is the
        # calendar date, while the link text supplies the local showtime.
        date = dt.datetime.fromtimestamp(int(raw_epoch), dt.timezone.utc).date()
        return _combine(date, time_match, timezone)

    dated_match = _DATE_TIME.search(text)
    if dated_match:
        return _combine(_date(dated_match, seen_date), dated_match, timezone)

    slug_match = _SHOWTIME_SLUG.search(urlsplit(url).path)
    if slug_match:
        year = int(slug_match.group(3))
        if year < 100:
            year += 2000
        date = dt.date(year, int(slug_match.group(1)), int(slug_match.group(2)))
        return _at(date, int(slug_match.group(4)), int(slug_match.group(5)),
                   slug_match.group(6), timezone)

    return _combine(fallback_date, time_match or fallback_match, timezone)


def _at(date, hour, minute, meridiem, timezone):
    meridiem = meridiem.lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return dt.datetime.combine(date, dt.time(hour, minute), ZoneInfo(timezone)).isoformat()


def _explicit_empty(text):
    return bool(re.search(r"no\s+(?:upcoming\s+)?(?:events?|screenings?|showtimes?)", text, re.I))


def _slug(url):
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "event"


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
