"""Read dated NYC classes from Danznik's public Mindbody widget."""

import datetime as dt
import re
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "danznik"
PARSER_VERSION = "danznik-mindbody-v1"
WIDGET_ORIGIN = "https://widgets.mindbodyonline.com"
WEEK_DAYS = 7
NYC_LOCATION_ID = "2"
VENUE = "Danznik NYC"
ADDRESS = "240 W 37th St, 2nd Floor, New York, NY 10018"
EMPTY_TEXT = "We're sorry, there are no more classes today."
HTML_ASSIGNMENT = re.compile(r"\.html\('((?:\\.|[^'\\])*)'\)")


def schedule_url(widget_id, date):
    return WIDGET_ORIGIN + "/widgets/schedules/" + widget_id + ".js?" + urlencode({
        "options[start_date]": date.isoformat(),
    })


def _document(script):
    # Decode the widget's HTML string without executing remote JavaScript.
    match = HTML_ASSIGNMENT.search(script)
    if not match:
        raise ParseError("Mindbody response has no HTML assignment")
    html = re.sub(r"\\(.)", lambda m: {
        "n": "\n", "r": "\r", "t": "\t",
    }.get(m[1], m[1]), match[1])
    return BeautifulSoup(html, "html.parser")


def _text(node):
    return node.get_text(" ", strip=True) if node is not None else ""


def _labels(document, name):
    labels = {}
    for option in document.select("select[name='{}'] option[value]".format(name)):
        for value in option["value"].split(","):
            if value:
                labels[value] = _text(option)
    return labels


def _local_time(row, selector, timezone):
    node = row.select_one(selector)
    value = node.get("data-datetime", "").strip('"') if node else ""
    if "T" not in value:
        raise ValueError("class is missing its date and time")
    # Mindbody encodes studio wall time with +00:00; the visible clock agrees.
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=ZoneInfo(timezone))


class DanznikAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = dt.datetime.now().astimezone().isoformat()
        listing = self.client.get(source["url"])
        if self.artifacts is not None:
            self.artifacts.save_response("listing_html", "danznik-schedule.html", listing)
        document = BeautifulSoup(listing.text, "html.parser")
        widgets = document.select('healcode-widget[data-type="schedules"]')
        if len(widgets) != 1:
            raise ParseError("Danznik must contain one schedule widget")
        widget_id = widgets[0].get("data-widget-id", "")
        if not re.fullmatch(r"[a-zA-Z0-9]+", widget_id):
            raise ParseError("Danznik widget ID is invalid")

        window_end = seen_date + dt.timedelta(days=lookahead_days)
        cursor = seen_date
        events, rejections, failures = [], [], []
        ids = set()
        suspicious = False
        while cursor <= window_end:
            response = self.client.get(schedule_url(widget_id, cursor))
            if self.artifacts is not None:
                self.artifacts.save_response(
                    "schedule_js", "danznik-{}.js".format(cursor), response)
            page = _document(response.text)
            date_input = page.select_one('input[name="start_date"]')
            next_link = page.select_one('.next_week a[data-hc-startdate]')
            next_date = cursor + dt.timedelta(days=WEEK_DAYS)
            if (date_input is None or date_input.get("value") != cursor.isoformat()
                    or next_link is None
                    or next_link.get("data-hc-startdate") != next_date.isoformat()
                    or page.select_one("table.schedule") is None):
                raise ParseError("Mindbody did not confirm the requested week")
            rows = page.select("tr.hc_class")
            if not rows and EMPTY_TEXT not in page.get_text(" ", strip=True):
                suspicious = True
            titles = _labels(page, "mbo_class")
            styles = _labels(page, "visit_type")
            rooms = _labels(page, "resource")
            for row in rows:
                raw = dict(row.attrs)
                try:
                    start = _local_time(row, ".hc_starttime", timezone)
                    end = _local_time(row, ".hc_endtime", timezone)
                    if end <= start:
                        raise ValueError("class end must follow its start")
                    if not cursor <= start.date() < next_date:
                        raise ValueError("class is outside its returned week")
                    if not seen_date <= start.date() <= window_end:
                        rejections.append({"reason": "outside_date_window", "raw": raw})
                        continue
                    if row.get("data-bw-widget-location") != NYC_LOCATION_ID:
                        rejections.append({"reason": "outside_nyc", "raw": raw})
                        continue
                    event = self._event(row, page, titles, styles, rooms,
                                        source, response, start, end)
                    key = event["source_event_id"]
                    if key not in ids:
                        ids.add(key)
                        events.append(event)
                except (ValueError, TypeError, KeyError) as error:
                    failures.append(str(error))
                    rejections.append({"reason": "invalid_class", "raw": {
                        "row": raw, "error": str(error), "source_url": response.url,
                    }})
            cursor = next_date

        state = "validation_failed" if failures else (
            "empty_suspicious" if suspicious else ("ok" if events else "empty_verified"))
        return source_result(
            state=state, method="python_adapter", recipe_version=PARSER_VERSION,
            started_at=started_at,
            finished_at=dt.datetime.now().astimezone().isoformat(),
            events=events, rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail="Read NYC schedule weeks for {} through {}.".format(seen_date, window_end),
            error="; ".join(failures) or None,
        )

    @staticmethod
    def _event(row, page, titles, styles, rooms, source, response, start, end):
        class_id = row.get("data-bw-widget-mbo-class-id", "")
        title = titles.get(row.get("data-bw-widget-mbo-class"), "")
        button = row.select_one("[data-url*='add_booking']")
        signup = button.get("data-url", "") if button else ""
        if not title and signup:
            title = parse_qs(urlsplit(signup).query).get("item[name]", [""])[0]
        if not title or not class_id:
            raise ValueError("class is missing its title or ID")
        description = ["Dance class or social."]
        for label, value in (
            ("Style", styles.get(row.get("data-bw-widget-visit-type"), "")),
            ("Instructor", _text(row.select_one("span.trainer"))),
            ("Level", _text(row.select_one("span.class_level"))),
            ("Room", rooms.get(row.get("data-bw-widget-resource"), "")),
        ):
            if value:
                description.append("{}: {}.".format(label, value))
        availability = _text(row.select_one(".hc_class_availability"))
        if availability:
            description.append(availability)
        return {
            "title": title, "start": start.isoformat(), "end": end.isoformat(),
            "url": signup or source["url"], "signup_url": signup or source["url"],
            "host": "Danznik Studios", "venue": VENUE,
            "neighborhood": "Midtown Manhattan", "address": ADDRESS,
            "price": "", "is_free": None, "description": " ".join(description),
            "capacity_flag": "sold_out" if "full" in availability.lower() else None,
            "status": "cancelled" if "cancelled" in row.get("class", []) else "active",
            "source_id": source["id"], "source_listing_url": source["url"],
            "source_url": response.url,
            "source_event_id": "{}:{}".format(class_id, start.isoformat()),
            "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
            "content_hash": response.content_hash,
            "extracted_json": {"row": dict(row.attrs), "title": title,
                               "description": " ".join(description)},
        }
