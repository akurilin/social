---
name: social-crawler
description: "Run event crawls from this repository: create a database-backed run for all enabled sources or one targeted source, retrieve with procedural adapters and Codex Desktop fallbacks, record each source result, finalize and deduplicate the run, and rank new events. Use for full crawls, one-source crawl tests, source retries, or crawl-result processing in any configured location."
---

# Social event crawler

Work from the repository root. Read `AGENTS.md` and the complete `sources.json` before each run. Stop if `sources.json` is missing or invalid.

Do not read or apply `EVENT_RANKING_CRITERIA.md` during planning, retrieval,
validation, or deduplication. Those phases collect factual, in-scope events and
operate independently of personal fit. Read the local criteria only in step 4.

Store run plans, source state, candidates, and final outcomes in `social.db`. Do not create a crawl JSON file. Keep only large raw evidence and screenshots in `.cache`.

## 1. Plan

For a full crawl, create the database run:

```sh
python3 tools/events_store.py plan-run --date YYYY-MM-DD
```

For a targeted crawl requested by the user, create a run for exactly one source:

```sh
python3 tools/events_store.py plan-run --date YYYY-MM-DD --only-source SOURCE_ID
```

`plan-run` creates the `runs` and `source_runs` records before retrieval and prints the run ID and `work` list. A full crawl puts every enabled website source in `work`. `--only-source` creates a run that contains only that enabled source. An inbox source enters `work` only when the mail connector is available; otherwise, its row is `blocked`. Stop if the ID is unknown or disabled.

These Python commands only store the plan. They do not retrieve events.

Run all pending procedural adapters as one bounded parallel batch:

```sh
python3 tools/crawl_run.py --run-id RUN_ID
```

The command retrieves up to four sources at the same time. It keeps database
writes on its main thread and stages results in stable source order. Each
adapter uses the same database contract as `record-source`. Use
`tools/crawl_source.py` only for a one-source adapter test or retry. Add
`--replace` when retrying a source result that is already staged:

```sh
python3 tools/crawl_source.py --run-id RUN_ID --source-id SOURCE_ID --replace
```

Do not also retrieve an adapter source with Codex Desktop unless its work row
requests an audit or the adapter fails. Codex Desktop must execute rows without
an adapter with the declared browser, Chrome, web, or connector tools.

To resume an active run, load its remaining retrieval instructions:

```sh
python3 tools/events_store.py run-work --run-id RUN_ID
```

Add `--mail-available` only when a mail connector is available.

Run `tools/crawl_run.py` again to process procedural adapters that are still
pending. Then use Codex Desktop for the remaining work rows.

A full run has one row for each enabled source. A targeted run has only its selected source. Work only through the returned `work` list. Do not record an unplanned source.

## 2. Retrieve and validate

Use the primary recipe first. Use a fallback when the primary fails or returns an unverified zero.

For a source with an `adapter`, the adapter is its primary execution path. Its
`recipe_version` is the adapter parser version. Use the retrieval profile's
browser or agent recipe as the independent audit and fallback path.

Treat zero events as valid only when the retrieval profile's `empty_signal` is present. Otherwise, record `empty_suspicious` and try a fallback.

When a work row has `audit: true`, compare the primary method with one independent fallback on a small sample. Required fields, dates, prices, venues, and availability must agree. New sources and retries request this check; elapsed time does not.

Write large evidence files under `.cache/`. Put every candidate in `events` or `rejections` in one source-result object. Include the final state, actual method, recipe version, timestamps, artifacts, detail, and error in `source`:

```json
{
  "source": {"state": "ok", "method": "browser_dom", "recipe_version": "calendar-dom-v1", "artifacts": []},
  "events": [],
  "rejections": []
}
```

Pass that object through standard input. Do not save it as a crawl file:

```sh
python3 tools/events_store.py record-source --run-id RUN_ID --source-id SOURCE_ID
```

The command replaces any earlier staged result for that source. The run page then shows the source state and staged candidates before finalization.

Put every factual event that matches the configured source scope and date window
in `events`. Do this even when the event will probably have a low fit rank. Use
`rejections` only for factual or ingestion reasons, such as an event outside the
configured window, an inactive or cancelled record, or missing required data.
Do not reject an event because of its topic, format, likely crowd, access rule,
price, travel time, or any other personal ranking criterion.

Valid source states are:

- `ok`
- `ok_via_fallback`
- `empty_verified`
- `empty_suspicious`
- `parse_failed`
- `validation_failed`
- `fetch_failed`
- `auth_required`
- `blocked`
## 3. Finalize

After all planned source rows have final states, finalize the stored run:

```sh
python3 tools/events_store.py finalize-run --run-id RUN_ID
```

The command refuses to continue while any source is `pending`. It validates staged candidates, deduplicates events, updates `last_seen` on existing events, computes source counts, and completes the run in one transaction. Finalization does not apply personal ranking criteria.

Finalization checks each candidate in this order:

1. Match an event-specific URL against the stored URL aliases.
2. Match the existing internal event ID when the current deduplication key gives the same ID.
3. For a new URL, compare events on the same date. The start times must be within 30 minutes. The titles must be very similar. The venue, address, host, or strong neighborhood evidence must also agree.

Finalization never merges events from title similarity alone. It merges only when one stored event is a strong match. If there is no strong match, or if the result is ambiguous, it creates a new event. When a new URL points to an existing event, finalization keeps the existing event ID and preferred URL, and adds the new URL as an alias.

Use normal finalization for a targeted crawl unless the user explicitly asks for a preview. Use `--dry-run` for a preview; it leaves the run active and makes no event changes.

Report these targeted-crawl results from the completed run:

- `new`: events added to the event store
- `existing`: `updated` plus `unchanged`
- `duplicates`: repeated candidates within this crawl
- `rejected`: candidates not added to the event store

Use the run page to show titles and individual outcomes. Do not describe the planning command as the crawler; Codex Desktop performed the retrieval.

## 4. Assess new events

Read all of `EVENT_RANKING_CRITERIA.md` now. If the local file is missing, leave
the events unranked and report that the ranking phase could not run. Do not fail
or undo the completed retrieval and finalization phases. Use
`EVENT_RANKING_CRITERIA.example.md` only as setup guidance; it is not the active
policy.

Get the ranking queue:

```sh
python3 tools/events_store.py needs-rank --grouped --json
```

Judge each event against `EVENT_RANKING_CRITERIA.md`. Write:

- `rank`: `high`, `medium`, or `low`
- `fit_note`: likely room, social mechanism, and main tradeoff
- `format_tags`: short event-format labels
- `catch`: practical warning, when applicable

Apply the assessments with `rank --file`. Do not treat promotional claims or attendance counts as evidence of good social structure.

Rank an event `low` when it conflicts with a hard rule. Keep it in the event
store, and explain the conflict in `fit_note` or `catch`. A fit decision must not
change retrieval results, ingestion counts, or rejection records.

## Safety

- Do not RSVP, buy, join, submit forms, or enter credentials for the operator.
- Do not bypass bot protection or send excessive requests. Record blocked or failed results.

Use the web control center as the user-facing result. Give a concise count summary in the task response, but do not create a separate report.
