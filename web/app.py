import json
import mimetypes
import calendar as calendar_lib
import datetime as dt
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .models import SourceEditorInput
from .repository import PROBLEM_STATES, ROOT, Repository


WEB_ROOT = Path(__file__).resolve().parent
CORE_SOURCE_FIELDS = {
    "id", "title", "url", "priority", "geo", "parse_hint",
    "retrieval_profile", "enabled", "disabled_reason", "health", "history",
    "events", "catalog_kind",
}


def create_app(root=ROOT, db_path=None):
    application = FastAPI(
        title="Social Crawler Control Center",
        description="Manage event discovery sources, runs, events, and crawler instructions.",
    )
    application.state.repo = Repository(root=root, db_path=db_path)
    application.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
    return application


app = create_app()
templates = Jinja2Templates(directory=WEB_ROOT / "templates")


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
    clean = {key: value for key, value in values.items() if value not in (None, "")}
    return path + ("?" + urlencode(clean) if clean else "")


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
    if state not in {"all", "enabled", "disabled", "issues"}:
        state = "enabled"
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
    catalog = repo(request).load_catalog()
    return page(
        request, "sources.html", title="Sources", sources=items, query=query,
        state=state, profiles=catalog.get("retrieval_profiles") or {},
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
    source_id = request.query_params.get("source_id", "")
    sort = request.query_params.get("sort", "start")
    default_direction = "asc" if period == "upcoming" else "desc"
    direction = request.query_params.get("dir", default_direction)
    if direction not in {"asc", "desc"}:
        direction = default_direction
    if sort not in {
            "title", "start", "first_seen", "last_seen", "place", "source", "rank"}:
        sort = "start"
    items = repo(request).list_events(
        query, rank, period, source_id, sort, direction)
    filters = {
        "q": query, "rank": rank, "period": period, "source_id": source_id,
    }
    sort_links = {}
    for field in (
            "title", "start", "first_seen", "last_seen", "place", "source", "rank"):
        next_direction = "desc" if sort == field and direction == "asc" else "asc"
        sort_links[field] = query_url(
            "/events", dict(filters, sort=field, dir=next_direction))
    calendar_url = query_url(
        "/calendar", {"rank": rank, "source_id": source_id})
    return page(
        request, "events.html", title="Events", events=items, query=query,
        rank=rank, period=period, source_id=source_id,
        source_options=source_options(request), sort=sort, direction=direction,
        sort_links=sort_links, calendar_url=calendar_url,
    )


@app.get("/calendar", response_class=HTMLResponse)
def event_calendar(request: Request):
    month = month_value(request.query_params.get("month"))
    rank = request.query_params.get("rank", "")
    source_id = request.query_params.get("source_id", "")
    weeks = calendar_lib.Calendar(firstweekday=0).monthdatescalendar(
        month.year, month.month)
    grid_start = weeks[0][0]
    grid_end = weeks[-1][-1]
    items = repo(request).calendar_events(
        grid_start.isoformat(), grid_end.isoformat(), rank, source_id)
    by_date = {}
    for event in items:
        by_date.setdefault((event.get("start") or "")[:10], []).append(event)
    today = dt.date.today()
    cells = [[{
        "date": day,
        "iso": day.isoformat(),
        "day": day.day,
        "in_month": day.month == month.month,
        "is_past": day < today,
        "is_today": day == today,
        "events": by_date.get(day.isoformat(), []),
    } for day in week] for week in weeks]
    previous_month = (month - dt.timedelta(days=1)).replace(day=1)
    next_month = (month + dt.timedelta(days=32)).replace(day=1)
    filters = {"rank": rank, "source_id": source_id}
    return page(
        request, "calendar.html", title="Event calendar", wide=True,
        month=month, month_label=month.strftime("%B %Y"), weeks=cells,
        event_count=sum(len(day["events"]) for week in cells for day in week
                        if day["in_month"]),
        rank=rank, source_id=source_id,
        source_options=source_options(request),
        previous_url=query_url(
            "/calendar", dict(filters, month=previous_month.strftime("%Y-%m"))),
        next_url=query_url(
            "/calendar", dict(filters, month=next_month.strftime("%Y-%m"))),
        today_url=query_url(
            "/calendar", dict(filters, month=today.strftime("%Y-%m"))),
        list_url=query_url(
            "/events", {"rank": rank, "source_id": source_id, "period": "all"}),
    )


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: str):
    event = repo(request).get_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    return page(request, "event_detail.html", title=event["title"], event=event)
