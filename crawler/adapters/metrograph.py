"""Procedural adapter for Metrograph's special-events page only."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "metrograph_special_events"
PARSER_VERSION = "metrograph-special-events-html-v1"
METHOD = "python_adapter"
EVENTS_PATH = "/events/"
_SHOWTIME = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{1,2}):(\d{2})\s*(am|pm)\b", re.I)
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})


class MetrographSpecialEventsAdapter:
    """Read the dedicated special-events cards, never the regular showtime feed."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        response = self.client.get(_events_url(source["url"]))
        if self.artifacts is not None:
            self.artifacts.save_response("special_events_html", "metrograph-special-events.html", response)
        cards, special_section = _special_cards(response.text)
        if not cards:
            if special_section or _explicit_empty(response.text):
                return source_result(
                    state="empty_verified", method=METHOD, recipe_version=PARSER_VERSION,
                    started_at=started, finished_at=_now_iso(),
                    artifacts=self.artifacts.items if self.artifacts is not None else [],
                    detail="Metrograph special-events page explicitly contained no special-event cards.")
            raise ParseError("Metrograph events page contained no special-events block")

        events, rejections, errors = [], [], []
        occurrences = set()
        for card in cards:
            try:
                for event in _card_events(card, response, source, seen_date, timezone):
                    occurrence = (event["source_event_id"], event["start"])
                    if occurrence in occurrences:
                        continue
                    occurrences.add(occurrence)
                    event_date = dt.datetime.fromisoformat(event["start"]).date()
                    if seen_date <= event_date <= end_date:
                        events.append(event)
                    else:
                        rejections.append({"reason": "outside_date_window", "raw": event})
            except (ParseError, ValueError, TypeError) as error:
                errors.append(str(error))
                rejections.append({"reason": "event_parse_failed", "raw": {
                    "source_id": source["id"], "source_listing_url": source["url"],
                    "source_url": response.url, "parser_version": PARSER_VERSION,
                    "error": str(error),
                }})
        state = "ok" if events and not errors else (
            "validation_failed" if errors else "empty_verified")
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Metrograph special-events page contained {} special card(s); {} "
                    "occurrence(s) were in {} through {}.").format(
                        len(cards), len(events), seen_date, end_date),
            error="; ".join(errors[:5]) or None,
        )


def _events_url(source_url):
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ParseError("source URL is not an absolute HTTP URL")
    return "{}://{}{}".format(parsed.scheme, parsed.netloc, EVENTS_PATH)


def _special_cards(text):
    soup = BeautifulSoup(text, "html.parser")
    sections = soup.select(".movies-grid.events-block")
    cards = []
    for section in sections:
        cards.extend(section.select(":scope > .item.film-thumbnail"))
    return cards, bool(sections)


def _card_events(card, response, source, seen_date, timezone):
    title_node = card.select_one("h4 a.title, h4")
    title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
    film_link = card.select_one("a.title[href], a.image[href]")
    film_url = urljoin(response.url, film_link.get("href")) if film_link else response.url
    if not title or not film_link:
        raise ParseError("special-event card is missing title or film URL")
    description = _clean(" ".join(
        node.get_text(" ", strip=True)
        for node in card.select(".film-metadata, .film-description")))
    showtimes = card.select(".showtimes a")
    if not showtimes:
        raise ParseError("special-event card has no showtime links")
    film_id = parse_qs(urlsplit(film_url).query).get("vista_film_id", [""])[0]
    film_id = film_id or urlsplit(film_url).path.rstrip("/").rsplit("/", 1)[-1]
    events = []
    for position, showtime in enumerate(showtimes):
        display = _clean(showtime.get_text(" ", strip=True))
        match = _SHOWTIME.search(display)
        if not match:
            raise ParseError("special-event showtime has no full date and time: {}".format(display))
        start = _local_datetime(match, seen_date, timezone)
        ticket_url = urljoin(response.url, showtime.get("href")) if showtime.get("href") else ""
        ticket_id = parse_qs(urlsplit(ticket_url).query).get("txtSessionId", [""])[0]
        event_id = "{}-{}".format(film_id, ticket_id or "{}-{}".format(
            dt.datetime.fromisoformat(start).strftime("%Y%m%d%H%M"), position))
        visible = " ".join((title, description, display))
        events.append({
            "title": title, "start": start, "end": None,
            "url": ticket_url or film_url, "signup_url": ticket_url or film_url,
            "host": "Metrograph", "venue": "Metrograph", "neighborhood": "",
            "address": "", "price": "", "is_free": None,
            "description": description, "capacity_flag": "sold_out" if "sold out" in display.lower() else None,
            "status": "cancelled" if re.search(r"\bcancel(?:led|ed)\b", visible, re.I) else "active",
            "source_id": source["id"], "source_listing_url": source["url"],
            "source_url": response.url, "source_event_id": event_id,
            "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
            "content_hash": response.content_hash,
            "extracted_json": {"film_url": film_url, "showtime": display,
                               "ticket_url": ticket_url, "description": description},
        })
    return events


def _local_datetime(match, reference, timezone):
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        raise ParseError("special-event showtime has an invalid month")
    year = reference.year
    if month - reference.month > 6:
        year -= 1
    elif reference.month - month > 6:
        year += 1
    hour, minute = int(match.group(3)), int(match.group(4))
    if match.group(5).lower() == "pm" and hour != 12:
        hour += 12
    if match.group(5).lower() == "am" and hour == 12:
        hour = 0
    return dt.datetime(year, month, int(match.group(2)), hour, minute,
                       tzinfo=ZoneInfo(timezone)).isoformat()


def _explicit_empty(text):
    return bool(re.search(r"no\s+(?:upcoming\s+)?special\s+events?", text, re.I))


def _clean(value):
    return re.sub(r"\s+", " ", value).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
