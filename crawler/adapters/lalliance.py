"""Procedural adapter for L'Alliance New York's public event archive."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "lalliance_events"
PARSER_VERSION = "lalliance-events-html-v1"
METHOD = "python_adapter"
_DATE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(20\d{2})\b",
    re.I,
)
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b", re.I)
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}


class LAllianceEventsAdapter:
    """Read the fully rendered official archive in one HTTP request."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        response = self.client.get(source["url"])
        if self.artifacts is not None:
            self.artifacts.save_response("event_listing_html", "lalliance-events.html", response)

        soup = BeautifulSoup(response.text, "html.parser")
        archive = soup.select_one(".events-grid__main")
        if archive is None:
            raise ParseError("L'Alliance archive has no events-grid__main structure")
        cards = archive.select(".event-card")
        if not cards:
            if _explicit_empty(archive.get_text(" ", strip=True)):
                return _result(started, seen_date, end_date, [], [], response, source,
                               self.artifacts, "official empty event message")
            raise ParseError("L'Alliance archive has no event cards or explicit empty signal")

        candidates, unresolved, rejections, errors = [], [], [], []
        for card in cards:
            raw = _card_evidence(card, response.url)
            if _has_category(card, "past"):
                rejections.append({"reason": "past_event", "raw": raw})
                continue
            try:
                card_events, card_unresolved = _card_events(card, response, source, timezone)
                candidates.extend(card_events)
                unresolved.extend(card_unresolved)
            except (ParseError, ValueError) as error:
                raw["error"] = str(error)
                rejections.append({"reason": "event_parse_failed", "raw": raw})
                errors.append(str(error))

        events = []
        for event in candidates:
            event_date = dt.datetime.fromisoformat(event["start"]).date()
            if seen_date <= event_date <= end_date:
                events.append(event)
            else:
                rejections.append({"reason": "outside_date_window", "raw": event})

        unresolved_in_window = 0
        for item in unresolved:
            if seen_date <= item["date"] <= end_date:
                unresolved_in_window += 1
                rejections.append({"reason": "missing_explicit_time", "raw": item["raw"]})
            else:
                rejections.append({"reason": "outside_date_window", "raw": item["raw"]})

        if errors:
            state = "validation_failed" if events else "parse_failed"
        elif events:
            state = "ok"
        elif unresolved_in_window:
            state = "empty_suspicious"
        else:
            state = "empty_verified"
        return _result(started, seen_date, end_date, events, rejections, response, source,
                       self.artifacts, "{} active card(s), {} explicit occurrence(s)".format(
                           len(cards), len(candidates) + len(unresolved)), state,
                       "; ".join(errors[:5]) or None)


def _result(started, seen_date, end_date, events, rejections, response, source, artifacts,
            evidence, state="empty_verified", error=None):
    return source_result(
        state=state, method=METHOD, recipe_version=PARSER_VERSION,
        started_at=started, finished_at=_now_iso(), events=events, rejections=rejections,
        artifacts=artifacts.items if artifacts is not None else [],
        detail=("L'Alliance archive: {}; {} event(s) in {} through {}.").format(
            evidence, len(events), seen_date, end_date),
        error=error,
    )


def _card_events(card, response, source, timezone):
    title_node = card.select_one(".event-card__heading")
    title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
    event_url = _event_url(card, response.url)
    date_node = _date_node(card)
    date_text = _clean(date_node.get_text(" ", strip=True) if date_node else "")
    if not title or not event_url or not date_text:
        raise ParseError("L'Alliance event card is missing title, detail URL, or dated occurrence text")
    occurrences = _occurrences(date_text, timezone)
    if not occurrences:
        raise ParseError("L'Alliance dated event card has no parseable occurrence")
    location = card.select_one(".event-card__location-name")
    venue = _clean(location.get_text(" ", strip=True) if location else "")
    excerpt = card.select_one(".event-card__excerpt")
    description = _clean(excerpt.get_text(" ", strip=True) if excerpt else "")
    categories = _categories(card)
    ticket = card.select_one(".event-card__buy-tickets[href]")
    signup_url = urljoin(response.url, ticket.get("href")) if ticket else event_url
    ticket_label = _clean(ticket.get_text(" ", strip=True) if ticket else "")
    visible = _clean(card.get_text(" ", strip=True))
    slug = urlsplit(event_url).path.rstrip("/").rsplit("/", 1)[-1]
    events, unresolved = [], []
    for number, (start, occurrence_date) in enumerate(occurrences, 1):
        if start is None:
            unresolved.append({"date": occurrence_date, "raw": {
                "title": title, "url": event_url, "source_id": source["id"],
                "source_listing_url": source["url"], "source_url": response.url,
                "date": occurrence_date.isoformat(), "date_text": date_text,
                "parser_version": PARSER_VERSION,
            }})
            continue
        event_id = "{}-{}-{}".format(slug or "event", start.strftime("%Y%m%d%H%M"), number)
        events.append({
            "title": title, "start": start.isoformat(), "end": None,
            "url": event_url, "signup_url": signup_url, "host": "L'Alliance New York",
            "venue": venue, "neighborhood": "", "address": "", "price": "",
            "is_free": _is_free(visible), "description": description,
            "capacity_flag": _capacity_flag(ticket_label),
            "status": _status(visible), "source_id": source["id"],
            "source_listing_url": source["url"], "source_url": response.url,
            "source_event_id": event_id, "fetched_at": response.fetched_at,
            "parser_version": PARSER_VERSION, "content_hash": response.content_hash,
            "extracted_json": {"date_text": date_text, "categories": categories,
                               "ticket_label": ticket_label},
        })
    return events, unresolved


def _date_node(card):
    """Find a date-bearing descendant without using generated Bricks class names."""

    for node in card.find_all(["p", "div", "span"]):
        text = _clean(node.get_text(" ", strip=True))
        if _DATE.search(text):
            return node
    return None


def _occurrences(text, timezone):
    matches = list(_DATE.finditer(text))
    values = []
    for index, match in enumerate(matches):
        previous = matches[index - 1] if index else None
        between = text[previous.end():match.start()] if previous else ""
        # A second date joined with a dash is the end of a date range, not a
        # new occurrence. The source does not state individual dates between it.
        if previous is not None and re.fullmatch(r"\s*[-–—]\s*", between):
            continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        time_text = text[match.end():next_start]
        times = list(_TIME.finditer(time_text))
        date = _date(match)
        if times:
            for time in times:
                values.append((_datetime(date, time, timezone), date))
        else:
            # The source states a date but not a time. It is useful evidence,
            # but it is not a factual start datetime and must not enter events.
            values.append((None, date))
    return values


def _date(match):
    return dt.date(int(match.group(3)), _MONTHS[match.group(1).lower()], int(match.group(2)))


def _datetime(date, match, timezone):
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = match.group(3).lower()
    if hour == 12:
        hour = 0
    if meridiem == "pm":
        hour += 12
    return dt.datetime.combine(date, dt.time(hour, minute), ZoneInfo(timezone))


def _event_url(card, base_url):
    for link in card.select("a[href]"):
        url = urljoin(base_url, link.get("href"))
        if "/event/" in urlsplit(url).path:
            return url
    return ""


def _categories(card):
    values = []
    for node in card.select(".event-card__category-block"):
        value = _clean(node.get_text(" ", strip=True))
        if value and value not in values:
            values.append(value)
    return values


def _has_category(card, category):
    return category.casefold() in {value.casefold() for value in _categories(card)}


def _card_evidence(card, base_url):
    return {"title": _clean((card.select_one(".event-card__heading") or card).get_text(" ", strip=True)),
            "event_url": _event_url(card, base_url), "text": _clean(card.get_text(" ", strip=True)),
            "categories": _categories(card)}


def _is_free(text):
    lowered = text.casefold()
    if re.search(r"\bnot\s+free\b", lowered):
        return False
    return True if re.search(r"\bfree\b", lowered) else None


def _capacity_flag(label):
    lowered = label.casefold()
    if "sold out" in lowered:
        return "sold_out"
    if "waitlist" in lowered:
        return "waitlist"
    return None


def _status(text):
    lowered = text.casefold()
    if "cancelled" in lowered or "canceled" in lowered:
        return "cancelled"
    if "postponed" in lowered:
        return "postponed"
    return "active"


def _explicit_empty(text):
    return bool(re.search(r"\bno\s+(?:upcoming\s+)?events?\b", text, re.I))


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
