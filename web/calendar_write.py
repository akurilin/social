"""Change a crawler event in one configured Apple Calendar."""

import json
import os
import subprocess
import sys

from .calendar_coverage import CalendarCoverageError, build_calendar_reader


CALENDAR_ENV = "SOCIAL_APPLE_CALENDAR"
CALENDAR_TIMEOUT_SECONDS = 120


class CalendarWriteError(Exception):
    """A calendar save error that is safe to show in the web page."""


def calendar_target(root):
    # Read only this setting; other local .env values stay outside the web app.
    if CALENDAR_ENV in os.environ:
        return os.environ[CALENDAR_ENV].strip()
    path = root / ".env"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == CALENDAR_ENV:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def run_calendar_helper(cache_path, mode, payload=None):
    if sys.platform != "darwin":
        raise CalendarWriteError("Apple Calendar is available only on macOS.")
    try:
        binary = build_calendar_reader(cache_path)
        result = subprocess.run(
            [str(binary), mode], input=json.dumps(payload) if payload else None,
            capture_output=True, text=True, check=True,
            timeout=CALENDAR_TIMEOUT_SECONDS,
        )
        response = json.loads(result.stdout)
    except CalendarCoverageError as error:
        raise CalendarWriteError(str(error)) from error
    except subprocess.TimeoutExpired as error:
        raise CalendarWriteError("Apple Calendar did not respond. Try again.") from error
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise CalendarWriteError("Apple Calendar could not complete the request.") from error
    if not isinstance(response, dict):
        raise CalendarWriteError("Apple Calendar returned an invalid response.")
    return response


def list_calendars(cache_path):
    response = run_calendar_helper(cache_path, "list")
    if response.get("status") != "available":
        raise CalendarWriteError("Apple Calendar could not list writable calendars.")
    return response.get("calendars", [])


def readiness_response(response, target):
    if not target:
        return {"ready": False, "message": (
            "Set SOCIAL_APPLE_CALENDAR in your local .env; see sample.env. "
            "Run python3 social.py calendars to find the exact name.")}
    state = response.get("status")
    if state == "available" and response.get("selector") == target:
        return {"ready": True, "message": "Calendar ready: {}.".format(target)}
    messages = {
        "permission_needed": "Calendar access is not enabled. Run python3 social.py calendars to allow access.",
        "denied": "Allow calendar access in macOS System Settings, then reload this page.",
        "no_calendars": "No writable Apple calendars are available.",
        "calendar_not_found": "This calendar is missing or cannot be changed. Run python3 social.py calendars and update .env.",
        "calendar_ambiguous": "More than one calendar has this name. Choose another Account/Calendar value.",
    }
    return {"ready": False, "message": messages.get(
        state, "Apple Calendar could not verify this calendar. Reload this page.")}


def calendar_readiness(cache_path, target):
    # Check access and the exact writable calendar without changing an event.
    if not target:
        return readiness_response({}, target)
    try:
        response = run_calendar_helper(cache_path, "check", {"calendar": target})
    except CalendarWriteError as error:
        return {"ready": False, "message": str(error)}
    return readiness_response(response, target)


def calendar_event_status(cache_path, target, events):
    if not target:
        return dict(readiness_response({}, target), present_ids=set(), ambiguous_ids=set())
    candidates = [
        {key: event.get(key) or "" for key in ("id", "title", "start", "url")}
        for event in events
    ]
    try:
        response = run_calendar_helper(
            cache_path, "status", {"calendar": target, "events": candidates})
    except CalendarWriteError as error:
        return {"ready": False, "message": str(error),
                "present_ids": set(), "ambiguous_ids": set()}
    readiness = readiness_response(response, target)
    return dict(
        readiness,
        present_ids=set(response.get("present_ids") or []) if readiness["ready"] else set(),
        ambiguous_ids=set(response.get("ambiguous_ids") or []) if readiness["ready"] else set(),
    )


def save_event(cache_path, target, event, end):
    # Resolve the target in EventKit so a changed Mac account cannot redirect a save.
    payload = {
        "calendar": target, "id": event["id"], "title": event["title"],
        "start": event["start"], "end": end,
        "location": ", ".join(part for part in (event["venue"], event["address"]) if part),
        "description": event["description"], "url": event["url"],
    }
    response = run_calendar_helper(cache_path, "save", payload)
    state = response.get("status")
    if state in {"saved", "already_added"}:
        return state
    messages = {
        "denied": "Allow calendar access in macOS System Settings, then try again.",
        "no_calendars": "No Apple calendars are available. Add your account in Calendar.",
        "calendar_not_found": "The configured calendar was not found or cannot be changed. Run python3 social.py calendars.",
        "calendar_ambiguous": "More than one calendar has this Account/Calendar name.",
        "invalid_event": "This event needs a valid start and end time.",
    }
    raise CalendarWriteError(messages.get(state, "Apple Calendar could not save this event."))


def remove_event(cache_path, target, event):
    # Recheck the selected calendar at deletion time; never trust the page state.
    payload = {
        "calendar": target, "id": event["id"], "title": event["title"],
        "start": event["start"], "url": event["url"],
    }
    response = run_calendar_helper(cache_path, "remove", payload)
    state = response.get("status")
    if state in {"removed", "not_found"}:
        return state
    messages = {
        "denied": "Allow calendar access in macOS System Settings, then try again.",
        "no_calendars": "No writable Apple calendars are available.",
        "calendar_not_found": "The configured calendar was not found or cannot be changed.",
        "calendar_ambiguous": "More than one calendar has this Account/Calendar name.",
        "multiple_matches": "Several calendar events match this record. Remove one in Apple Calendar.",
        "invalid_event": "This event has no valid start time.",
    }
    raise CalendarWriteError(messages.get(state, "Apple Calendar could not remove this event."))
