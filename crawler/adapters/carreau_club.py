"""Procedural adapter for Carreau Club's explicit Friday tournament schedule."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "carreau_club_friday_melee"
PARSER_VERSION = "carreau-club-friday-schedule-html-v1"
METHOD = "python_adapter"
_WEEKLY_SCHEDULE = re.compile(
    r"\bFree\s+Casual\s+Beginners\s+Tournament\s+EVERY\s+FRIDAY\s+at\s+7\s*p\.?m\.?\b",
    re.I,
)
_TOURNAMENT = re.compile(
    r"\bWe\s+host\s+a\s+(?P<title>FREE\s+open\s+melee\s+tournament)\.\s*"
    r"(?P<rsvp>No\s+RSVP\s+required!)\s*"
    r"(?P<description>.*?\bfinish\s+by\s+10\s*p\.?m\.?)",
    re.I,
)
_LOCATION = re.compile(
    r"\b(?P<neighborhood>INDUSTRY\s+CITY)\s+Located\s+in\s+Building\s+\d+\s+"
    r"(?P<address>\d+\s+\d+(?:st|nd|rd|th)\s+Street\s+Brooklyn,\s+NY\s+\d{5})\b",
    re.I,
)


class CarreauClubFridayMeleeAdapter:
    """Read one official home page and require its full current schedule grammar."""

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
            self.artifacts.save_response("home_html", "carreau-club-home.html", response)
        evidence = _schedule_evidence(response.text)
        events = [
            _event_for_date(event_date, source, response, timezone, evidence)
            for event_date in _fridays(seen_date, end_date)
        ]
        return source_result(
            state="ok" if events else "empty_verified",
            method=METHOD,
            recipe_version=PARSER_VERSION,
            started_at=started,
            finished_at=_now_iso(),
            events=events,
            rejections=[],
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=(
                "Official home page confirmed the weekly Friday 7 PM free tournament; "
                "{} occurrence(s) fall in {} through {}."
            ).format(len(events), seen_date, end_date),
        )


def _schedule_evidence(document):
    soup = BeautifulSoup(document, "html.parser")
    text = _clean(soup.get_text(" ", strip=True))
    page_title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    missing = []
    weekly = _WEEKLY_SCHEDULE.search(text)
    tournament = _TOURNAMENT.search(text)
    location = _LOCATION.search(text)
    if not weekly:
        missing.append("weekly Friday 7 PM schedule")
    if not tournament:
        missing.append("free tournament, no-RSVP, and 10 PM finish schedule")
    if not location:
        missing.append("Industry City street address")
    if not re.search(r"\bCarreau\s+Club\b", page_title, re.I):
        missing.append("Carreau Club page title")
    if missing:
        raise ParseError("Carreau Club required schedule evidence is missing: {}".format(
            "; ".join(missing)))
    return {
        "title": "Friday Night Pétanque — {}".format(
            _clean(tournament.group("title")).title()),
        "description": _clean(tournament.group(0)),
        "no_rsvp_text": _clean(tournament.group("rsvp")),
        "weekly_schedule_text": _clean(weekly.group(0)),
        "venue": "Carreau Club",
        "neighborhood": _clean(location.group("neighborhood")).title(),
        "address": _clean(location.group("address")).replace(
            " Street Brooklyn,", " Street, Brooklyn,"),
        "page_title": page_title,
    }


def _fridays(start_date, end_date):
    date = start_date + dt.timedelta(days=(4 - start_date.weekday()) % 7)
    while date <= end_date:
        yield date
        date += dt.timedelta(days=7)


def _event_for_date(event_date, source, response, timezone, evidence):
    zone = ZoneInfo(timezone)
    start = dt.datetime.combine(event_date, dt.time(19), zone)
    end = dt.datetime.combine(event_date, dt.time(22), zone)
    event_id = hashlib.sha256("|".join((
        source["id"], "friday-open-melee", event_date.isoformat(), response.url,
    )).encode("utf-8")).hexdigest()[:20]
    return {
        "title": evidence["title"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "url": response.url,
        "signup_url": response.url,
        "host": "Carreau Club",
        "venue": evidence["venue"],
        "neighborhood": evidence["neighborhood"],
        "address": evidence["address"],
        "price": "Free",
        "is_free": True,
        "description": evidence["description"],
        "capacity_flag": None,
        "status": "active",
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": event_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {
            "weekly_schedule_text": evidence["weekly_schedule_text"],
            "no_rsvp_text": evidence["no_rsvp_text"],
            "page_title": evidence["page_title"],
        },
    }


def _clean(value):
    return re.sub(r"\s+", " ", value).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
