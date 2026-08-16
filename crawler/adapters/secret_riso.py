"""Procedural adapter for Secret Riso Club's public Cargo calendar."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "secret_riso_calendar"
PARSER_VERSION = "secret-riso-calendar-html-v1"
METHOD = "python_adapter"
_UPCOMING_MARKER = "Upcoming Events and Workshops:"
_PAST_MARKER = "Past Events:"
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
_DATE = re.compile(
    r"(?P<word>\b(?P<month_name>January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+(?P<word_day>\d{1,2})"
    r"(?:st|nd|rd|th)?\b)|(?P<numeric>\b(?P<num_month>1[0-2]|0?[1-9])/"
    r"(?P<num_day>3[01]|[12]\d|0?[1-9])\b)", re.I)
_TIME = re.compile(
    r"\b(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?\s*"
    r"(?P<ampm>a\.?m\.?|p\.?m\.?)?\b", re.I)
_YEAR = re.compile(r"\b(20\d{2})\b")


class SecretRisoCalendarAdapter:
    """Read the complete current calendar from one official page response."""

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
                "calendar_html", "secret-riso-calendar.html", response)

        page_id, calendar_html, year = _calendar_content(response.text, source["url"])
        rows = _upcoming_rows(calendar_html)
        if not rows:
            return _result(started, seen_date, end_date, [], [], response, source,
                           self.artifacts, page_id, "explicit empty upcoming section")

        events, rejections, errors = [], [], []
        for row in rows:
            evidence = _row_evidence(row)
            try:
                row_events, unresolved = _row_events(
                    row, evidence, year, response, source, timezone, page_id)
            except (ParseError, ValueError) as error:
                evidence["error"] = str(error)
                rejections.append({"reason": "event_parse_failed", "raw": evidence})
                errors.append(str(error))
                continue
            for event in row_events:
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if seen_date <= event_date <= end_date:
                    events.append(event)
                else:
                    rejections.append({"reason": "outside_date_window", "raw": event})
            for unresolved_item in unresolved:
                reason = ("missing_explicit_time" if
                          seen_date <= unresolved_item["date"] <= end_date else
                          "outside_date_window")
                rejections.append({"reason": reason, "raw": unresolved_item["raw"]})

        has_missing_time = any(
            item["reason"] == "missing_explicit_time" for item in rejections)
        state = ("validation_failed" if errors and events else
                 "parse_failed" if errors else
                 "ok" if events else
                 "empty_suspicious" if has_missing_time else "empty_verified")
        return _result(
            started, seen_date, end_date, events, rejections, response, source,
            self.artifacts, page_id, "{} listing row(s), {} timed occurrence(s)".format(
                len(rows), len(events) + sum(
                    item["reason"] == "outside_date_window" and
                    isinstance(item["raw"], dict) and "start" in item["raw"]
                    for item in rejections)), state,
            "; ".join(errors[:5]) or None)


def _calendar_content(document, listing_url):
    soup = BeautifulSoup(document, "html.parser")
    node = soup.find("script", attrs={"data-set": "ScaffoldingData"})
    if node is None or not node.string:
        raise ParseError("Secret Riso calendar has no ScaffoldingData document")
    try:
        payload = json.loads(node.string)
    except json.JSONDecodeError as error:
        raise ParseError("Secret Riso ScaffoldingData is not valid JSON") from error
    page = _calendar_page(payload, listing_url)
    content = page.get("content")
    if not isinstance(content, str):
        raise ParseError("Secret Riso calendar page has no HTML content")
    upcoming = content.find(_UPCOMING_MARKER)
    past = content.find(_PAST_MARKER)
    if upcoming < 0 or past < 0 or upcoming >= past:
        raise ParseError("Secret Riso calendar markers are missing or reordered")
    year_matches = _YEAR.findall(content[:upcoming])
    if not year_matches:
        raise ParseError("Secret Riso calendar has no explicit calendar year")
    page_id = str(page.get("id") or "calendar")
    return page_id, content, int(year_matches[-1])


def _calendar_page(payload, listing_url):
    target = _normalized_url(listing_url)
    for page in _walk_pages(payload):
        direct_link = page.get("direct_link")
        if isinstance(direct_link, str) and _normalized_url(direct_link) == target:
            return page
        project_url = page.get("project_url")
        if isinstance(project_url, str) and _path_key(project_url) == _path_key(listing_url):
            return page
    raise ParseError("Secret Riso ScaffoldingData has no matching Calendar page")


def _walk_pages(value):
    if isinstance(value, dict):
        if "content" in value and ("direct_link" in value or "project_url" in value):
            yield value
        for child in value.get("pages", []) if isinstance(value.get("pages"), list) else []:
            yield from _walk_pages(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_pages(child)


def _upcoming_rows(calendar_html):
    upcoming = calendar_html[
        calendar_html.find(_UPCOMING_MARKER) + len(_UPCOMING_MARKER):
        calendar_html.find(_PAST_MARKER)]
    soup = BeautifulSoup(upcoming, "html.parser")
    rows = []
    for row in soup.find_all("div", attrs={"grid-row": True}):
        columns = row.find_all("div", attrs={"grid-col": True}, recursive=False)
        if not columns:
            continue
        if len(columns) != 5:
            raise ParseError("Secret Riso upcoming row does not have five columns")
        values = [_clean(column.get_text(" ", strip=True)) for column in columns]
        if not any(values):
            continue
        rows.append(row)
    return rows


def _row_events(row, evidence, year, response, source, timezone, page_id):
    columns = row.find_all("div", attrs={"grid-col": True}, recursive=False)
    title_node = columns[1].find("h1")
    title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
    date_text = _multiline_text(columns[3])
    if not title or not date_text:
        raise ParseError("Secret Riso listing is missing H1 title or date block")
    dates = list(_DATE.finditer(date_text))
    if not dates:
        raise ParseError("Secret Riso listing has no explicit date")
    ticket = columns[4].find("a", href=True)
    signup_url = urljoin(response.url, ticket.get("href")) if ticket else source["url"]
    ticket_label = _clean(ticket.get_text(" ", strip=True) if ticket else
                          columns[4].get_text(" ", strip=True))
    venue, address = _location(date_text)
    description = _clean(columns[2].get_text(" ", strip=True))
    category = _clean(columns[0].get_text(" ", strip=True))
    visible = " ".join(_clean(column.get_text(" ", strip=True)) for column in columns)
    events, unresolved = [], []
    for position, match in enumerate(dates):
        next_start = dates[position + 1].start() if position + 1 < len(dates) else len(date_text)
        time_text = date_text[match.end():next_start]
        event_date = _matched_date(match, year)
        start = _start_from_text(event_date, time_text, timezone)
        if start is None:
            continue
        event_id = hashlib.sha256("|".join((
            page_id, title, start.isoformat(), signup_url,
        )).encode("utf-8")).hexdigest()[:20]
        events.append({
            "title": title, "start": start.isoformat(), "end": None,
            "url": signup_url, "signup_url": signup_url,
            "host": "Secret Riso Club", "venue": venue,
            "neighborhood": "", "address": address,
            "price": _price(ticket_label), "is_free": _is_free(visible),
            "description": description, "capacity_flag": _capacity_flag(visible),
            "status": _status(visible), "source_id": source["id"],
            "source_listing_url": source["url"], "source_url": response.url,
            "source_event_id": event_id, "fetched_at": response.fetched_at,
            "parser_version": PARSER_VERSION, "content_hash": response.content_hash,
            "extracted_json": {
                "category": category, "date_text": date_text,
                "ticket_label": ticket_label, "page_id": page_id,
            },
        })
    if not events:
        unresolved.append({"date": _matched_date(dates[0], year), "raw": {
            "title": title, "date_text": date_text, "source_id": source["id"],
            "source_listing_url": source["url"], "source_url": response.url,
            "parser_version": PARSER_VERSION, "page_id": page_id,
        }})
    return events, unresolved


def _matched_date(match, year):
    if match.group("word"):
        return dt.date(year, _MONTHS[match.group("month_name").lower()],
                       int(match.group("word_day")))
    return dt.date(year, int(match.group("num_month")), int(match.group("num_day")))


def _start_from_text(event_date, text, timezone):
    times = list(_TIME.finditer(text))
    if not times:
        return None
    start_match = times[0]
    ampm = _ampm(start_match.group("ampm"))
    if ampm is None:
        for item in times[1:]:
            ampm = _ampm(item.group("ampm"))
            if ampm is not None:
                break
    if ampm is None:
        return None
    hour = int(start_match.group("hour"))
    minute = int(start_match.group("minute") or 0)
    if hour == 12:
        hour = 0
    if ampm == "pm":
        hour += 12
    return dt.datetime.combine(event_date, dt.time(hour, minute), ZoneInfo(timezone))


def _ampm(value):
    if not value:
        return None
    return "am" if value.lower().replace(".", "").startswith("a") else "pm"


def _location(text):
    lines = [_clean(line) for line in text.split("\n") if _clean(line)]
    for index, line in enumerate(lines):
        if line.casefold().startswith("location:"):
            first = _clean(line.split(":", 1)[1])
            values = ([first] if first else []) + lines[index + 1:]
            return (values[0] if values else "", ", ".join(values[1:]))
    return "", ""


def _row_evidence(row):
    columns = row.find_all("div", attrs={"grid-col": True}, recursive=False)
    return {"columns": [_multiline_text(column) for column in columns]}


def _normalized_url(url):
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/").casefold() or "/"
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))


def _path_key(url):
    return urlsplit(url).path.strip("/").casefold()


def _multiline_text(node):
    return "\n".join(_clean(item) for item in node.stripped_strings if _clean(item))


def _price(text):
    match = re.search(r"\$\s*\d+(?:\.\d{2})?(?:\s+[^\n]+)?", text)
    return _clean(match.group(0)) if match else ""


def _is_free(text):
    lowered = text.casefold()
    if re.search(r"\bnot\s+free\b", lowered):
        return False
    return True if re.search(r"\bfree\b", lowered) else None


def _capacity_flag(text):
    lowered = text.casefold()
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


def _result(started, seen_date, end_date, events, rejections, response, source, artifacts,
            page_id, evidence, state="empty_verified", error=None):
    return source_result(
        state=state, method=METHOD, recipe_version=PARSER_VERSION,
        started_at=started, finished_at=_now_iso(), events=events, rejections=rejections,
        artifacts=artifacts.items if artifacts is not None else [],
        detail=("Secret Riso Cargo calendar page {}: {}; {} event(s) in {} through {}.").format(
            page_id, evidence, len(events), seen_date, end_date), error=error)


def _clean(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
