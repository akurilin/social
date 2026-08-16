"""Procedural adapter for Analog Film NYC's public screening calendar."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "analog_film_nyc"
PARSER_VERSION = "analog-film-nyc-html-v1"
METHOD = "python_adapter"
_DAY_HEADING = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th):?$"
)
_TIME = re.compile(r"^\d{1,2}:\d{2}\s*(?:AM|PM)$", re.I)
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}


@dataclass(frozen=True)
class DaySection:
    """One dated day and its public screening list."""

    date: dt.date
    heading: str
    paragraph: object


class AnalogFilmNYCAdapter:
    """Read one official public page; never load linked ticket pages."""

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
            self.artifacts.save_response(
                "screenings_html", "analog-film-nyc-screenings.html", response)

        sections = _day_sections(response.text, seen_date)
        if not sections:
            raise ParseError("Analog Film NYC page has no dated screening sections")

        events, rejections, errors = [], [], []
        for section in sections:
            screenings, unresolved, section_errors = _section_screenings(
                section, response, source, timezone)
            errors.extend(section_errors)
            for raw in unresolved:
                reason = "missing_explicit_time"
                if seen_date <= section.date <= end_date:
                    rejections.append({"reason": reason, "raw": raw})
                else:
                    rejections.append({"reason": "outside_date_window", "raw": raw})
            for event in screenings:
                if seen_date <= section.date <= end_date:
                    events.append(event)
                else:
                    rejections.append({"reason": "outside_date_window", "raw": event})

        coverage_complete = _has_complete_day_coverage(sections, seen_date, end_date)
        missing_time_in_window = any(
            item["reason"] == "missing_explicit_time" for item in rejections)
        if errors:
            state = "validation_failed" if events else "parse_failed"
        elif events:
            state = "ok"
        elif coverage_complete and not missing_time_in_window:
            state = "empty_verified"
        else:
            state = "empty_suspicious"
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Analog Film NYC contained {} dated day section(s), {} complete "
                    "screening(s), and {} event(s) in {} through {}; day coverage {}.").format(
                        len(sections), len(events) + sum(
                            item["reason"] == "outside_date_window" and
                            isinstance(item["raw"], dict) and "start" in item["raw"]
                            for item in rejections), len(events), seen_date, end_date,
                        "is complete" if coverage_complete else "does not cover the full window"),
            error="; ".join(errors[:5]) or None,
        )


def _day_sections(text, reference_date):
    soup = BeautifulSoup(text, "html.parser")
    root = soup.select_one("main") or soup
    sections = []
    for heading in root.find_all("h2"):
        heading_text = _clean(heading.get_text(" ", strip=True))
        match = _DAY_HEADING.fullmatch(heading_text)
        if not match:
            continue
        paragraph = heading.find_next_sibling("p")
        if paragraph is None:
            raise ParseError("Analog Film NYC day section is missing its screening paragraph")
        sections.append(DaySection(
            date=_heading_date(match, reference_date),
            heading=heading_text,
            paragraph=paragraph,
        ))
    return sections


def _section_screenings(section, response, source, timezone):
    lines = _lines(section.paragraph)
    events, unresolved, errors = [], [], []
    index = 0
    unassigned_start = 0
    while index < len(lines):
        if not _TIME.fullmatch(_line_text(lines[index])):
            if _ticket_link(lines[index]):
                unresolved.append(_unresolved_item(
                    section, lines[unassigned_start:index + 1], response.url))
                unassigned_start = index + 1
            index += 1
            continue

        time_text = _line_text(lines[index])
        ticket_index = index + 1
        while ticket_index < len(lines):
            if _TIME.fullmatch(_line_text(lines[ticket_index])):
                break
            if _ticket_link(lines[ticket_index]):
                break
            ticket_index += 1
        if ticket_index >= len(lines) or _TIME.fullmatch(_line_text(lines[ticket_index])):
            errors.append("{} {} has a time but no ticket line".format(
                section.heading, time_text))
            index = ticket_index
            unassigned_start = index
            continue

        ticket_url = _ticket_link(lines[ticket_index])
        titles = _titles(lines[index + 1:ticket_index])
        venue_line = _line_text(lines[ticket_index])
        venue = _venue(venue_line)
        if not titles or not venue or not ticket_url:
            errors.append("{} {} is missing title, venue, or ticket URL".format(
                section.heading, time_text))
            index = ticket_index + 1
            unassigned_start = index
            continue
        intro_lines, next_index = _following_introductions(lines, ticket_index + 1)
        events.append(_event(
            section=section, time_text=time_text, titles=titles, venue=venue,
            ticket_url=urljoin(response.url, ticket_url),
            description_lines=lines[index + 1:ticket_index + 1] + intro_lines,
            response=response, source=source, timezone=timezone,
        ))
        index = next_index
        unassigned_start = index
    return events, unresolved, errors


def _lines(paragraph):
    """Split a WordPress paragraph at every BR, including nested BR nodes."""

    copied = BeautifulSoup(str(paragraph), "html.parser")
    for node in copied.find_all("br"):
        node.replace_with("\n")
    return [BeautifulSoup(part, "html.parser")
            for part in str(copied).split("\n")]


def _following_introductions(lines, index):
    """Keep a source note such as '*Introduced by …', but never guess an event."""

    introductions = []
    while index < len(lines):
        text = _line_text(lines[index])
        if not text:
            index += 1
            continue
        if text.startswith("*"):
            introductions.append(lines[index])
            index += 1
            continue
        break
    return introductions, index


def _event(section, time_text, titles, venue, ticket_url, description_lines, response,
           source, timezone):
    start = _start_datetime(section.date, time_text, timezone)
    title = " / ".join(titles)
    description = _clean(" ".join(_line_text(line) for line in description_lines))
    source_event_id = hashlib.sha256("|".join((
        section.date.isoformat(), time_text.upper(), ticket_url,
    )).encode("utf-8")).hexdigest()[:20]
    visible = " ".join((title, venue, description))
    return {
        "title": title, "start": start, "end": None,
        "url": ticket_url, "signup_url": ticket_url,
        "host": venue, "venue": venue, "neighborhood": "", "address": "",
        "price": "", "is_free": None, "description": description,
        "capacity_flag": None,
        "status": "cancelled" if re.search(r"\bcancel(?:led|ed)\b", visible, re.I) else "active",
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": source_event_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {
            "day_heading": section.heading,
            "time": time_text,
            "titles": titles,
            "venue_line": _line_text(description_lines[-1]) if description_lines else "",
            "ticket_url": ticket_url,
        },
    }


def _unresolved_item(section, lines, source_url):
    ticket_line = next((line for line in reversed(lines) if _ticket_link(line)), None)
    return {
        "day_heading": section.heading,
        "date": section.date.isoformat(),
        "text": _clean(" ".join(_line_text(line) for line in lines)),
        "ticket_url": _ticket_link(ticket_line) if ticket_line is not None else "",
        "source_url": source_url,
    }


def _titles(lines):
    titles = []
    for line in lines:
        for node in line.find_all("em"):
            value = _clean(node.get_text(" ", strip=True))
            if value and not value.startswith("*"):
                titles.append(value)
    return titles


def _ticket_link(line):
    for link in line.select("a[href]"):
        if _clean(link.get_text(" ", strip=True)).lower() == "tickets":
            return link.get("href") or ""
    return ""


def _venue(line):
    return _clean(re.split(r"\s+[–—]\s+", line, maxsplit=1)[0])


def _heading_date(match, reference_date):
    month = _MONTHS[match.group(1).lower()]
    year = reference_date.year
    if month - reference_date.month > 6:
        year -= 1
    elif reference_date.month - month > 6:
        year += 1
    try:
        return dt.date(year, month, int(match.group(2)))
    except ValueError as error:
        raise ParseError("Analog Film NYC day heading has an invalid date") from error


def _start_datetime(date, time_text, timezone):
    parsed = dt.datetime.strptime(time_text.upper(), "%I:%M %p")
    return dt.datetime.combine(date, parsed.time(), ZoneInfo(timezone)).isoformat()


def _has_complete_day_coverage(sections, start_date, end_date):
    covered = {section.date for section in sections}
    return all(start_date + dt.timedelta(days=offset) in covered
               for offset in range((end_date - start_date).days + 1))


def _line_text(line):
    return _clean(line.get_text(" ", strip=True))


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
