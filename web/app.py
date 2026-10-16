import json
import mimetypes
import calendar as calendar_lib
import datetime as dt
import hmac
import secrets
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .models import SourceEditorInput
from .calendar_coverage import refresh_coverage
from .calendar_logs import read_logs
from .calendar_write import (
    CalendarWriteError, calendar_event_status, calendar_readiness,
    calendar_target, remove_event, save_event,
)
from .repository import PROBLEM_STATES, ROOT, Repository


WEB_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = WEB_ROOT / "static"
VERSIONED_STATIC_ASSET_NAMES = ("app.js",)
STATIC_ASSET_VERSIONS = {
    name: (STATIC_ROOT / name).stat().st_mtime_ns
    for name in VERSIONED_STATIC_ASSET_NAMES
}
NO_STORE_CACHE_CONTROL = "no-store"
DEFAULT_EVENT_SORT = "start"
DEFAULT_EVENT_SORT_DIRECTION = "asc"
HOURS_PER_HALF_DAY = 12
MERIDIEM_LABELS = ("a.m.", "p.m.")
SORT_DIRECTIONS = {"asc", "desc"}
SOURCE_SORTS = {
    "title": "Source", "retrieval_profile": "Retrieval", "health": "Latest health",
    "found_count": "Found", "added_on": "Added", "seen_date": "Last checked",
}
CORE_SOURCE_FIELDS = {
    "id", "title", "url", "priority", "geo", "parse_hint",
    "retrieval_profile", "enabled", "disabled_reason", "health", "history",
    "events", "catalog_kind", "added_on",
}
DATABASE_MIGRATION_COMMAND = "python3 social.py migrate"
templates = Jinja2Templates(directory=WEB_ROOT / "templates")
templates.env.globals["static_asset_versions"] = STATIC_ASSET_VERSIONS


async def require_current_database_schema(request, call_next):
    if request.url.path.startswith("/static/"):
        return await call_next(request)
    schema = request.app.state.repo.database_schema_status()
    if schema["state"] in {"behind", "ahead", "error"}:
        # Block data routes because a mismatched schema can return wrong data.
        return templates.TemplateResponse(
            request=request,
            name="database_migration_required.html",
            context={
                "request": request,
                "schema": schema,
                "migration_command": DATABASE_MIGRATION_COMMAND,
                "title": "Database update required",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return await call_next(request)


async def disable_response_cache(request, call_next):
    # Each refresh must fetch current pages and assets after a code change.
    response = await call_next(request)
    response.headers["Cache-Control"] = NO_STORE_CACHE_CONTROL
    return response


def create_app(root=ROOT, db_path=None):
    application = FastAPI(
        title="Social Crawler Control Center",
        description="Manage event discovery sources, runs, events, and crawler instructions.",
    )
    application.state.repo = Repository(root=root, db_path=db_path)
    application.state.calendar_csrf_token = secrets.token_urlsafe(32)
    application.middleware("http")(require_current_database_schema)
    application.middleware("http")(disable_response_cache)
    application.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
    return application


app = create_app()


def repo(request):
    return request.app.state.repo


def page(request, template, **context):
    base = {
        "request": request,
        "problem_states": PROBLEM_STATES,
        "saved": request.query_params.get("saved") == "1",
    }
    base.update(context)
    return templates.TemplateResponse(request=request, name=template, context=base)


def event_list_item(item):
    # Add display fields so the grouped list does not parse dates in the template.
    start = item["start"]
    event_date = dt.date.fromisoformat(start[:10])
    event_time = dt.datetime.fromisoformat(start).time()
    display_hour = event_time.hour % HOURS_PER_HALF_DAY or HOURS_PER_HALF_DAY
    display_minutes = ":{:02d}".format(event_time.minute)
    meridiem = MERIDIEM_LABELS[event_time.hour // HOURS_PER_HALF_DAY]
    return dict(
        item,
        start_day=event_date.isoformat(),
        start_day_label="{} {}, {}".format(
            event_date.strftime("%A, %B"), event_date.day, event_date.year),
        start_time_label="{}{} {}".format(
            display_hour, display_minutes, meridiem),
    )


async def form_values(request):
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def validation_message(error):
    if isinstance(error, ValidationError):
        return " ".join(item["msg"].replace("Value error, ", "")
                        for item in error.errors())
    return str(error)


def source_form_value(source):
    source = source or {}
    extra = {key: value for key, value in source.items() if key not in CORE_SOURCE_FIELDS}
    return {
        "id": source.get("id", ""),
        "title": source.get("title", ""),
        "url": source.get("url", ""),
        "priority": source.get("priority", 2),
        "geo": source.get("geo", ""),
        "parse_hint": source.get("parse_hint", ""),
        "retrieval_profile": source.get("retrieval_profile", ""),
        "enabled": source.get("enabled", True),
        "disabled_reason": source.get("disabled_reason", ""),
        "extra_json": json.dumps(extra, indent=2, ensure_ascii=False),
    }


def source_options(request):
    return sorted(
        ((item.get("id"), item.get("title"))
         for item in repo(request).load_catalog().get("sources") or []),
        key=lambda item: (item[1] or item[0]).lower(),
    )


def query_url(path, values):
    clean = {
        key: value for key, value in values.items()
        if value not in (None, "", [])
    }
    return path + ("?" + urlencode(clean, doseq=True) if clean else "")


def source_filter(request):
    options = source_options(request)
    requested = request.query_params.getlist("source_id")
    active = "sources_filtered" in request.query_params or bool(requested)
    selected = requested if active else [value for value, _ in options]
    return {
        "source_options": options,
        "selected_source_ids": selected,
        "source_filter_active": active,
    }


def source_filter_query(selected, active):
    if not active:
        return {}
    return {"sources_filtered": "1", "source_id": selected}


def month_value(raw):
    try:
        return dt.datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError):
        return dt.date.today().replace(day=1)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return page(request, "dashboard.html", data=repo(request).dashboard(), title="Overview")


@app.get("/settings/event-ranking-criteria", response_class=HTMLResponse)
def event_ranking_criteria_editor(request: Request):
    return page(
        request, "text_editor.html", title="Event ranking criteria",
        setting="event_ranking_criteria",
        content=repo(request).read_text("event_ranking_criteria"),
        description=(
            "Control the fit rank that the crawler assigns after it retrieves, "
            "validates, and stores an event."
        ),
    )


@app.post("/settings/event-ranking-criteria", response_class=HTMLResponse)
async def save_event_ranking_criteria(request: Request):
    values = await form_values(request)
    repo(request).save_text("event_ranking_criteria", values.get("content", ""))
    return RedirectResponse(
        "/settings/event-ranking-criteria?saved=1", status_code=303)


@app.get("/settings/workflow", response_class=HTMLResponse)
def workflow_editor(request: Request):
    return page(
        request, "text_editor.html", title="Crawler skill", setting="workflow",
        content=repo(request).read_text("workflow"),
        description="Control how each run plans, crawls, validates, merges, and ranks data.",
    )


@app.post("/settings/workflow", response_class=HTMLResponse)
async def save_workflow(request: Request):
    values = await form_values(request)
    repo(request).save_text("workflow", values.get("content", ""))
    return RedirectResponse("/settings/workflow?saved=1", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    items = repo(request).list_sources()
    query = request.query_params.get("q", "").strip()
    normalized_query = query.lower()
    state = request.query_params.get("state", "enabled") or "enabled"
    sort = request.query_params.get("sort", "")
    direction = request.query_params.get("dir", "asc")
    if state not in {"all", "enabled", "disabled", "issues"}:
        state = "enabled"
    if sort not in SOURCE_SORTS:
        sort = ""
    if direction not in SORT_DIRECTIONS:
        direction = "asc"
    if normalized_query:
        items = [item for item in items if normalized_query in " ".join((
            item.get("id", ""), item.get("title", ""), item.get("url", ""),
            item.get("geo", ""), item.get("retrieval_profile", ""),
        )).lower()]
    if state == "enabled":
        items = [item for item in items if item.get("enabled", True)]
    elif state == "disabled":
        items = [item for item in items if not item.get("enabled", True)]
    elif state == "issues":
        items = [item for item in items if item.get("health")
                 and item["health"].get("state") in PROBLEM_STATES]
    if sort:
        # Sort displayed values and keep missing counts or dates last in either direction.
        def sort_value(item):
            health = item.get("health") or {}
            if sort == "health":
                return (health.get("state") or "never checked") if item.get("enabled", True) else "disabled"
            value = health.get(sort) if sort in {"found_count", "seen_date"} else item.get(sort)
            return value.casefold() if isinstance(value, str) else value

        known = [item for item in items if sort_value(item) not in (None, "")]
        missing = [item for item in items if sort_value(item) in (None, "")]
        items = sorted(known, key=sort_value, reverse=direction == "desc") + missing
    filters = {"q": query, "state": state}
    sort_links = {
        field: query_url("/sources", dict(
            filters, sort=field,
            dir="desc" if sort == field and direction == "asc" else "asc"))
        for field in SOURCE_SORTS
    }
    catalog = repo(request).load_catalog()
    return page(
        request, "sources.html", title="Sources", sources=items, query=query,
        state=state, sort=sort, direction=direction,
        sort_links=sort_links, sort_columns=SOURCE_SORTS,
        profiles=catalog.get("retrieval_profiles") or {},
        inbox_count=len((catalog.get("inbox_sources") or {}).get("items") or []),
        catalog_path=repo(request).catalog_path.name,
    )


@app.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, source_id: str):
    source = repo(request).get_source(source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    return page(request, "source_detail.html", title=source["title"], source=source)


@app.get("/source", response_class=HTMLResponse)
def source_detail_query(request: Request, source_id: str):
    source = repo(request).get_source(source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    return page(request, "source_detail.html", title=source["title"], source=source)


@app.get("/sources/{source_id}/edit", response_class=HTMLResponse)
def edit_source(request: Request, source_id: str):
    source = repo(request).get_source(source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    if source.get("catalog_kind") != "website":
        return RedirectResponse("/sources")
    catalog = repo(request).load_catalog()
    return page(
        request, "source_form.html", title="Edit source", source=source_form_value(source),
        profiles=sorted((catalog.get("retrieval_profiles") or {}).keys()),
        original_id=source_id, error=None,
    )


@app.post("/sources/{source_id}/edit", response_class=HTMLResponse)
async def update_source(request: Request, source_id: str):
    values = await form_values(request)
    values["enabled"] = values.get("enabled") == "on"
    catalog = repo(request).load_catalog()
    existing = repo(request).get_source(source_id)
    if not existing:
        raise HTTPException(404, "Source not found")
    if existing.get("catalog_kind") != "website":
        return RedirectResponse("/sources")
    try:
        model = SourceEditorInput.model_validate(values)
        extra = json.loads(model.extra_json)
        source = dict(extra)
        source.update(model.model_dump(exclude={"extra_json"}))
        repo(request).upsert_source(source, original_id=source_id)
    except (ValidationError, ValueError) as error:
        return page(
            request, "source_form.html", title="Edit source", source=values,
            profiles=sorted((catalog.get("retrieval_profiles") or {}).keys()),
            original_id=source_id, error=validation_message(error),
        )
    return RedirectResponse("/sources/{}?saved=1".format(quote(model.id)), status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    return page(request, "runs.html", title="Runs", runs=repo(request).list_runs())


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    data = repo(request).get_run(run_id)
    if not data:
        raise HTTPException(404, "Run not found")
    return page(request, "run_detail.html", title="Run {}".format(data["run"]["seen_date"]), data=data)


@app.get("/runs/{run_id}/artifacts/{source_id}/{index}")
def run_artifact(request: Request, run_id: str, source_id: str, index: int):
    return artifact_response(request, run_id, source_id, index)


@app.get("/artifact")
def run_artifact_query(request: Request, run_id: str, source_id: str, index: int):
    return artifact_response(request, run_id, source_id, index)


def artifact_response(request, run_id, source_id, index):
    path, artifact = repo(request).artifact_path(run_id, source_id, index)
    if not path or not path.is_file():
        raise HTTPException(404, "Artifact not found")
    size_limit = 2 * 1024 * 1024
    content_type = (artifact.get("content_type") if isinstance(artifact, dict) else None) \
        or mimetypes.guess_type(str(path))[0] or ""
    if content_type.startswith("text/") or content_type in {
            "application/json", "application/xml", "application/javascript", ""}:
        raw = path.read_bytes()
        truncated = len(raw) > size_limit
        text = raw[:size_limit].decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[Artifact truncated at 2 MB.]"
        return PlainTextResponse(text)
    return PlainTextResponse("Binary artifact: {} ({} bytes)".format(path.name, path.stat().st_size))


@app.get("/events", response_class=HTMLResponse)
def events(request: Request):
    query = request.query_params.get("q", "").strip()
    rank = request.query_params.get("rank", "")
    period = request.query_params.get("period", "upcoming")
    sources = source_filter(request)
    selected_source_ids = sources["selected_source_ids"]
    source_filter_active = sources["source_filter_active"]
    # Keep the next event first unless the user selects another order.
    sort = request.query_params.get("sort", DEFAULT_EVENT_SORT)
    direction = request.query_params.get("dir", DEFAULT_EVENT_SORT_DIRECTION)
    if direction not in SORT_DIRECTIONS:
        direction = DEFAULT_EVENT_SORT_DIRECTION
    if sort not in {
            "title", "start", "first_seen", "last_seen", "place", "source", "rank"}:
        sort = DEFAULT_EVENT_SORT
    items = [event_list_item(item) for item in repo(request).list_events(
        query, rank, period,
        selected_source_ids if source_filter_active else None,
        sort, direction)]
    filters = {
        "q": query, "rank": rank, "period": period,
    }
    filters.update(source_filter_query(
        selected_source_ids, source_filter_active))
    sort_links = {}
    for field in (
            "title", "start", "first_seen", "last_seen", "place", "source", "rank"):
        next_direction = "desc" if sort == field and direction == "asc" else "asc"
        sort_links[field] = query_url(
            "/events", dict(filters, sort=field, dir=next_direction))
    calendar_filters = {"rank": rank}
    calendar_filters.update(source_filter_query(
        selected_source_ids, source_filter_active))
    calendar_url = query_url("/calendar", calendar_filters)
    coverage = repo(request).calendar_coverage()
    target = calendar_target(repo(request).root)
    calendar_state = calendar_event_status(repo(request).cache_path, target, items)
    return page(
        request, "events.html", title="Events", events=items, query=query,
        rank=rank, period=period, sort=sort, direction=direction,
        **sources,
        sort_links=sort_links, calendar_url=calendar_url,
        calendar_nav_url=calendar_url,
        calendar_state=calendar_state,
        calendar_csrf_token=request.app.state.calendar_csrf_token,
        calendar_timezone=repo(request).load_catalog().get("timezone") or "event local time",
        coverage=coverage, coverage_by_date={day["date"]: day for day in coverage["days"]},
    )


@app.get("/calendar-coverage/logs", response_class=PlainTextResponse)
def calendar_coverage_logs(request: Request):
    # Show retained diagnostics without caching local operational data.
    return PlainTextResponse(read_logs(repo(request).cache_path),
                             headers={"Cache-Control": NO_STORE_CACHE_CONTROL})


@app.post("/calendar-coverage/refresh")
def refresh_calendar_coverage(request: Request):
    # Only a local page action may read private calendars and start a model check.
    origin = request.headers.get("origin")
    if (request.headers.get("sec-fetch-site") == "cross-site"
            or origin and origin != str(request.base_url).rstrip("/")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Open this app to refresh.")
    repository = repo(request)
    with repository.calendar_refresh_state_lock:
        if not repository.calendar_refresh_lock.acquire(blocking=False):
            raise HTTPException(status.HTTP_409_CONFLICT, "A calendar check is already running.")
    try:
        return refresh_coverage(repository, cancel=repository.calendar_refresh_cancel)
    finally:
        with repository.calendar_refresh_state_lock:
            repository.calendar_refresh_cancel.clear()
            repository.calendar_refresh_lock.release()


@app.post("/calendar-coverage/stop")
def stop_calendar_coverage(request: Request):
    # Apply the same origin check before stopping a local refresh.
    origin = request.headers.get("origin")
    if (request.headers.get("sec-fetch-site") == "cross-site"
            or origin and origin != str(request.base_url).rstrip("/")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Open this app to stop a refresh.")
    repository = repo(request)
    with repository.calendar_refresh_state_lock:
        if repository.calendar_refresh_lock.locked():
            repository.calendar_refresh_cancel.set()
    return {"ok": True}


@app.get("/calendar", response_class=HTMLResponse)
def event_calendar(request: Request):
    month = month_value(request.query_params.get("month"))
    rank = request.query_params.get("rank", "")
    sources = source_filter(request)
    selected_source_ids = sources["selected_source_ids"]
    source_filter_active = sources["source_filter_active"]
    weeks = calendar_lib.Calendar(firstweekday=0).monthdatescalendar(
        month.year, month.month)
    grid_start = weeks[0][0]
    grid_end = weeks[-1][-1]
    items = repo(request).calendar_events(
        grid_start.isoformat(), grid_end.isoformat(), rank,
        selected_source_ids if source_filter_active else None)
    by_date = {}
    for event in items:
        by_date.setdefault((event.get("start") or "")[:10], []).append(event)
    today = dt.date.today()
    coverage = repo(request).calendar_coverage(today)
    coverage_by_date = {day["date"]: day for day in coverage["days"]}
    cells = [[{
        "date": day,
        "iso": day.isoformat(),
        "day": day.day,
        "in_month": day.month == month.month,
        "is_past": day < today,
        "is_today": day == today,
        "events": by_date.get(day.isoformat(), []),
        "coverage": coverage_by_date.get(day.isoformat()),
    } for day in week] for week in weeks]
    previous_month = (month - dt.timedelta(days=1)).replace(day=1)
    next_month = (month + dt.timedelta(days=32)).replace(day=1)
    filters = {"rank": rank}
    filters.update(source_filter_query(
        selected_source_ids, source_filter_active))
    list_url = query_url("/events", dict(filters, period="all"))
    calendar_nav_url = query_url(
        "/calendar", dict(filters, month=month.strftime("%Y-%m")))
    return page(
        request, "calendar.html", title="Event calendar", wide=True,
        coverage=coverage,
        month=month, month_label=month.strftime("%B %Y"), weeks=cells,
        event_count=sum(len(day["events"]) for week in cells for day in week
                        if day["in_month"]),
        rank=rank, **sources,
        previous_url=query_url(
            "/calendar", dict(filters, month=previous_month.strftime("%Y-%m"))),
        next_url=query_url(
            "/calendar", dict(filters, month=next_month.strftime("%Y-%m"))),
        today_url=query_url(
            "/calendar", dict(filters, month=today.strftime("%Y-%m"))),
        list_url=list_url, events_nav_url=list_url,
        calendar_nav_url=calendar_nav_url,
    )


def event_detail_page(request, event, notice=None, error=None, target=None, readiness=None):
    target = calendar_target(repo(request).root) if target is None else target
    calendar_state = (calendar_event_status(repo(request).cache_path, target, [event])
                      if readiness is None else dict(
                          readiness, present_ids=set(), ambiguous_ids=set()))
    return page(
        request, "event_detail.html", title=event["title"], event=event,
        calendar_target=target, calendar_readiness=calendar_state,
        calendar_state=calendar_state,
        calendar_notice=notice, calendar_error=error,
        calendar_csrf_token=request.app.state.calendar_csrf_token,
        calendar_timezone=repo(request).load_catalog().get("timezone") or "event local time",
    )


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: str):
    event = repo(request).get_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    notices = {
        "saved": "Event saved in Apple Calendar. Check the linked account for sync.",
        "already_added": "This event is already in the selected calendar.",
        "removed": "Event removed from the selected Apple calendar.",
        "not_found": "This event is no longer in the selected Apple calendar.",
    }
    return event_detail_page(
        request, event, notice=notices.get(request.query_params.get("calendar")))


@app.post("/events/{event_id}/calendar", response_class=HTMLResponse)
async def add_event_to_calendar(request: Request, event_id: str):
    values = await form_values(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    if not hmac.compare_digest(
            values.get("csrf_token", ""), request.app.state.calendar_csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid calendar request")
    event = repo(request).get_event(event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if event["status"] != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only active events can be added to Calendar")
    target = calendar_target(repo(request).root)
    with repo(request).calendar_save_lock:
        readiness = calendar_readiness(repo(request).cache_path, target)
        if not readiness["ready"]:
            if wants_json:
                return JSONResponse(
                    {"detail": readiness["message"]},
                    status_code=status.HTTP_409_CONFLICT)
            response = event_detail_page(
                request, event, target=target, readiness=readiness)
            response.status_code = status.HTTP_409_CONFLICT
            return response
        try:
            start = dt.datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
            if event["end"]:
                end = dt.datetime.fromisoformat(event["end"].replace("Z", "+00:00"))
            else:
                raw_end = values.get("end", "")
                end = dt.datetime.fromisoformat(raw_end)
                if end.tzinfo is not None:
                    raise ValueError("End time must be local")
                zone_name = repo(request).load_catalog().get("timezone")
                zone = ZoneInfo(zone_name) if zone_name else start.tzinfo
                end = end.replace(tzinfo=zone)
            if end <= start:
                raise ValueError("End time must follow start time")
            state = save_event(repo(request).cache_path, target, event, end.isoformat())
        except (ValueError, ZoneInfoNotFoundError):
            message = "Enter an end time after the event start."
        except CalendarWriteError as error:
            message = str(error)
        else:
            coverage = repo(request).update_calendar_plan(event, added=True)
            if wants_json:
                return JSONResponse({"state": state, "coverage": coverage})
            return_to = values.get("return_to", "")
            if return_to == "/events" or return_to.startswith("/events?"):
                return RedirectResponse(return_to, status_code=status.HTTP_303_SEE_OTHER)
            return RedirectResponse(
                query_url("/events/{}".format(event_id), {"calendar": state}),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    if wants_json:
        return JSONResponse({"detail": message}, status_code=status.HTTP_400_BAD_REQUEST)
    response = event_detail_page(request, event, error=message)
    response.status_code = status.HTTP_400_BAD_REQUEST
    return response


@app.post("/events/{event_id}/calendar/remove", response_class=HTMLResponse)
async def remove_event_from_calendar(request: Request, event_id: str):
    values = await form_values(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    if not hmac.compare_digest(
            values.get("csrf_token", ""), request.app.state.calendar_csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid calendar request")
    event = repo(request).get_event(event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    target = calendar_target(repo(request).root)
    with repo(request).calendar_save_lock:
        readiness = calendar_readiness(repo(request).cache_path, target)
        if not readiness["ready"]:
            message = readiness["message"]
        else:
            try:
                state = remove_event(repo(request).cache_path, target, event)
            except CalendarWriteError as error:
                message = str(error)
            else:
                coverage = repo(request).update_calendar_plan(event, added=False)
                if wants_json:
                    return JSONResponse({"state": state, "coverage": coverage})
                return_to = values.get("return_to", "")
                if return_to == "/events" or return_to.startswith("/events?"):
                    return RedirectResponse(return_to, status_code=status.HTTP_303_SEE_OTHER)
                return RedirectResponse(
                    query_url("/events/{}".format(event_id), {"calendar": state}),
                    status_code=status.HTTP_303_SEE_OTHER,
                )
    if wants_json:
        return JSONResponse({"detail": message}, status_code=status.HTTP_409_CONFLICT)
    response = event_detail_page(request, event, error=message)
    response.status_code = status.HTTP_409_CONFLICT
    return response
