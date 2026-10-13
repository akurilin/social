"""Read Big Apple Ballroom's public schedule and dated changes."""

from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, RawEvent, source_result


ADAPTER_ID = "big_apple_ballroom"
PARSER_VERSION = "big-apple-ballroom-schedule-v1"
HOST = "Big Apple Ballroom"
WEEK_DAYS = 7
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
FLIGHT_PREFIX = "self.__next_f.push("
SCHEDULE_FIELDS = {"classes", "events", "rotations", "overrides"}
OVERRIDE_FIELDS = {
    "danceChange": {"overrideDance": "dance"},
    "timeChange": {"overrideStartTime": "startTime",
                   "overrideDurationMinutes": "durationMinutes"},
    "instructorSub": {"overrideInstructor": "instructor"},
    "note": {}, "cancelled": {},
}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _schedule(document):
    # Decode data, without running page scripts. Next can split a JSON record.
    soup = BeautifulSoup(document, "html.parser")
    chunks = []
    decoder = json.JSONDecoder()
    for node in soup.find_all("script"):
        text = (node.string or "").strip()
        if not text.startswith(FLIGHT_PREFIX):
            continue
        try:
            chunk, _ = decoder.raw_decode(text[len(FLIGHT_PREFIX):])
            if chunk[0] == 1 and isinstance(chunk[1], str):
                chunks.append(chunk[1])
        except (ValueError, TypeError, IndexError) as error:
            raise ParseError("Invalid schedule data stream") from error
    stream = "".join(chunks)
    for match in re.finditer(r"(?:^|\n)[0-9a-f]+:(?=[\[{])", stream):
        try:
            record, _ = decoder.raw_decode(stream[match.end():])
        except ValueError:
            continue
        for value in _walk(record):
            if SCHEDULE_FIELDS <= value.keys():
                if not all(isinstance(value[key], list) for key in SCHEDULE_FIELDS):
                    raise ParseError("Schedule fields must be arrays")
                return soup, value
    raise ParseError("Public schedule data is missing")


def _address(soup):
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(node.string or "")
        except ValueError:
            continue
        for value in _walk(data):
            if value.get("name") == HOST and isinstance(value.get("address"), dict):
                address = value["address"]
                return ", ".join(str(address[key]) for key in (
                    "streetAddress", "addressLocality", "addressRegion", "postalCode")
                    if address.get(key))
    raise ParseError("Studio address is missing")


def _occurs(row, date):
    if row.get("dayOfWeek") not in DAY_NAMES:
        raise ValueError("Class weekday is missing or unknown")
    schedule_type = row.get("scheduleType")
    if schedule_type not in ("recurring", "monthly"):
        raise ValueError("Unknown class schedule type")
    if row["dayOfWeek"] != DAY_NAMES[date.weekday()]:
        return False
    if schedule_type == "monthly":
        week = row.get("weekOfMonth")
        if week == "last":
            return (date + dt.timedelta(days=WEEK_DAYS)).month != date.month
        if str(week) not in {"1", "2", "3", "4", "5"}:
            raise ValueError("Monthly class week is invalid")
        return (date.day - 1) // WEEK_DAYS + 1 == int(week)
    return True


def _changed(row, date, rotations, overrides):
    row = dict(row)
    if row.get("isRotating"):
        row["dance"] = rotations.get((date.strftime("%Y-%m-01"), row["_id"]))
    change = overrides.get((row["_id"], date.isoformat()))
    if change:
        kind = change.get("type")
        if kind not in OVERRIDE_FIELDS:
            raise ValueError("Unknown schedule change type")
        row["overrideNote"] = change.get("note")
        row["cancelled"] = kind == "cancelled" or bool(row.get("cancelled"))
        for old, new in OVERRIDE_FIELDS[kind].items():
            if change.get(old) is not None:
                row[new] = change[old]
    return row


def _event(row, date, source, response, timezone, address):
    if not row.get("_id") or not isinstance(row.get("title"), str) or not row["title"].strip():
        raise ValueError("Event ID or title is missing")
    # The site displays startDate in UTC as studio wall time, e.g. 19Z means 7 PM.
    clock = (dt.time.fromisoformat(row["startTime"]) if row.get("startTime") else
             dt.datetime.fromisoformat(row["startDate"].replace("Z", "+00:00"))
             .astimezone(dt.timezone.utc).time())
    start = dt.datetime.combine(date, clock.replace(tzinfo=None), ZoneInfo(timezone))
    duration = row.get("durationMinutes")
    if duration is not None and (type(duration) not in (int, float) or duration <= 0):
        raise ValueError("Event duration is invalid")
    end = start + dt.timedelta(minutes=duration) if duration is not None else None
    amount = row.get("dropInPrice")
    if amount is not None and (type(amount) not in (int, float) or amount < 0):
        raise ValueError("Event price is invalid")
    price = (row.get("priceNote") or ("${:g}".format(amount) if amount is not None else ""))
    price = price.replace("$$", "$")
    description = [row.get("description") or ""]
    for label, field in (("Category", "category"), ("Style", "danceStyle"),
                         ("Dance", "dance"), ("Instructor", "instructor"),
                         ("Level", "level"), ("Access", "calendarTag")):
        if row.get(field):
            description.append("{}: {}.".format(label, row[field]))
    if row.get("noPartnerRequired"):
        description.append("No partner required.")
    if row.get("overrideNote"):
        description.append(row["overrideNote"])
    signup = urljoin(source["url"], row.get("bookingUrl") or source["url"])
    return RawEvent.model_validate({
        "title": row["title"] + (" — " + row["dance"] if row.get("dance") else ""),
        "start": start.isoformat(), "end": end.isoformat() if end else None,
        "url": signup, "signup_url": signup, "host": HOST, "venue": HOST,
        "neighborhood": "Union Square", "address": address, "price": price,
        "is_free": amount == 0 if amount is not None else None,
        "description": " ".join(part for part in description if part),
        "capacity_flag": None, "status": "cancelled" if row.get("cancelled") else "active",
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": "{}:{}".format(row["_id"], date),
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash, "extracted_json": row,
    }).model_dump(mode="python")


class BigAppleBallroomAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now()
        response = self.client.get(source["url"])
        if self.artifacts is not None:
            self.artifacts.save_response("schedule_html", "big-apple-ballroom.html", response)
        soup, schedule = _schedule(response.text)
        address = _address(soup)
        try:
            rotations = {(rotation["month"], row["classId"]): row["dance"]
                         for rotation in schedule["rotations"] for row in rotation["entries"]}
            overrides = {(row["classId"], row["date"]): row for row in schedule["overrides"]}
        except (KeyError, TypeError) as error:
            raise ParseError("Schedule changes are malformed") from error
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        events, rejections, failures, ids = [], [], [], set()

        def reject(reason, row, error=None):
            rejections.append({"reason": reason, "raw": {"row": row, "error": error}})
            if error:
                failures.append(error)

        def accept(row, date):
            if not seen_date <= date <= end_date:
                reject("outside_date_window", row)
                return
            if (row.get("calendarTag") or "").casefold() == "private group":
                reject("private_group", row)
                return
            event = _event(row, date, source, response, timezone, address)
            if event["source_event_id"] not in ids:
                ids.add(event["source_event_id"])
                events.append(event)

        # Use the same recurrence and change rules as the site's week controls.
        for original in schedule["classes"]:
            try:
                date = seen_date
                while date <= end_date:
                    if _occurs(original, date):
                        accept(_changed(original, date, rotations, overrides), date)
                    date += dt.timedelta(days=1)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                reject("invalid_class", original, str(error))
        for row in schedule["events"]:
            try:
                date = dt.datetime.fromisoformat(row["startDate"].replace("Z", "+00:00"))
                date = date.astimezone(dt.timezone.utc).date()
                accept(row, date)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                reject("invalid_event", row, str(error))
        state = "validation_failed" if failures else "ok" if events else "empty_verified"
        return source_result(
            state=state, method="python_adapter", recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now(), events=events, rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail="Read the complete public schedule for {} through {}.".format(seen_date, end_date),
            error="; ".join(failures[:5]) or None,
        )
