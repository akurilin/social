# Social Event Crawler

This repository collects factual event listings from a maintained source
catalog, deduplicates them in SQLite, and then ranks their fit against a local
set of personal criteria. Retrieval does not use the ranking criteria. A
low-fit event stays in the event store with a low rank.

The crawler framework is location-neutral. The project includes many event
sources and ready-to-use adapters by default. The template catalog and some
adapters currently target New York City. To use another city, replace or add
sources, set the catalog timezone, and write local ranking criteria for that
location.

## 1. Set up the local source catalog

Create your local source catalog from the included template before you first
use the application:

```sh
cp sources.example.json sources.json
```

`sources.example.json` contains the default sources and is a starting template
only. Keep the sources and adapters that you need, remove the others, and add
new sources for your location. The application reads your local `sources.json`
file. Git ignores this file so that your changes do not change the template.

## 2. Set up local ranking criteria

Create the local criteria file from the tracked example:

```sh
cp EVENT_RANKING_CRITERIA.example.md EVENT_RANKING_CRITERIA.md
```

Edit `EVENT_RANKING_CRITERIA.md` directly or through the web control center.
Git ignores this file because it contains personal preferences.

## 3. Add event sources

Use the repository source skill in Codex:

```text
$add-source Add https://example.com/events to the event source catalog.
```

The skill investigates the source, chooses a retrieval profile, writes the
entry through the repository layer, and validates `sources.json`.

For a stable source, ask Codex to create a procedural adapter with fixture
tests, register it in `crawler/registry.py`, and add its adapter ID to the source
entry. Keep the retrieval profile because it defines the independent audit and
fallback method. A source without an adapter can still run through its declared
Codex Desktop retrieval method.

## 4. Run a full sync

From a Codex task rooted in this repository, invoke the crawler skill:

```text
$social-crawler Run a full crawl for today. Complete all sources, finalize
the run, rank all new events, and report the final counts.
```

The skill performs the complete sync:

1. It creates a database-backed run for every enabled source.
2. It retrieves procedural sources in a bounded parallel batch. One coordinator
   writes their results to SQLite in stable source order. It uses Codex Desktop
   only for declared audits, fallbacks, or sources without adapters.
3. It validates, stores, and deduplicates the factual events.
4. It reads `EVENT_RANKING_CRITERIA.md` and ranks new events as high, medium, or
   low fit.
5. It leaves the completed run and events in `social.db`.

If a run stops before completion, invoke the same skill again:

```text
$social-crawler Resume the active crawl and complete finalization and ranking.
```

`python3 tools/events_store.py plan-run` only creates a run plan. It does not
perform a sync by itself.

The skill runs this command after it creates the plan:

```sh
python3 tools/crawl_run.py --run-id RUN_ID
```

The default is four concurrent retrievals. Use `--workers N` to change the
limit. Finalization remains separate so audits and non-procedural sources can
finish first.

## 5. View the results

Start the local web control center:

```sh
python3 social.py web
```

Use `python3 web/start.py` instead if you also want to open the browser
automatically. The web app shows source health, run history, discoveries,
deduplicated events, fit assessments, and the event calendar.

The web app reads `social.db`. It does not start a sync. Run the crawler skill,
then refresh the web app to see the completed results.

## Validate repository changes

```sh
python3 social.py test
```

This command validates the source catalog and runs the complete test suite.
