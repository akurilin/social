---
name: add-source
description: Add or update an event source in the social crawler's sources.json catalog. Use when Codex must investigate a new event source, choose its retrieval profile, add or change its catalog entry through the repository layer, disable it, or validate the source catalog for any configured location.
---

# Add a source to the event crawler catalog

Add or update an entry in `sources.json`, the source catalog for the social event crawler. The catalog is an agent contract: each entry declares where to look and how to fetch it. A bad entry can fail silently during future crawls, so investigate the source carefully.

Never hand-edit `sources.json` directly. Insert through the repository layer so validation, atomic writes, and catalog invariants are enforced.

## Workflow

### 1. Gather what the user knows

Ask for the URL and anything they know about the source, such as event type and login requirements. If they gave only a name, search for the official site and confirm the URL before you continue.

### 2. Investigate the site

Fetch the URL and its clear calendar or events pages before you write anything. Determine:

- The platform, such as Luma, Eventbrite, Meetup, Substack, Instagram, a venue site, or a directory that links to other calendars.
- Whether it has structured data, such as embedded JSON, RSS, or JSON-LD, or needs a real browser or logged-in session.
- Whether it lists dated events. A directory with no dates is not an event feed. See the `TRAP` notes in existing `parse_hint` fields for examples.

### 3. Check the catalog

- Search `sources.json` sources for the same URL or host. If it exists, update it instead of adding a duplicate.
- Check the `removed` section. If the source was rejected before, tell the user and ask before you add it again.
- Check for overlap. Aggregators can overlap venue sources, but record this in `parse_hint`.

### 4. Choose the retrieval profile

Pick a profile from `retrieval_profiles` in `sources.json`.

| Profile | Use when |
|---|---|
| `luma-calendar-api-v1` | Luma calendar |
| `eventbrite-organizer-v1` | Eventbrite organizer page |
| `meetup-group-v1` | Meetup group page |
| `http-calendar-v1` | Plain site with an HTTP-readable event calendar |
| `browser-calendar-v1` | JavaScript-rendered calendar that needs a browser |
| `rss-archive-v1` | Substack or newsletter with a public archive or RSS |
| `directory-http-v1`, `browser-directory-v1`, `link-hub-v1` | Directory or link hub |
| `authenticated-browser-v1`, `partiful-calendar-v1`, `instagram-chrome-v1` | Source that needs the operator's logged-in browser |

If authentication is required, add an `auth` block that follows an existing pattern. Tell the user that the source needs their session.

### 5. Build or select the procedural adapter

Every new enabled website source must have a procedural adapter. Assume that a
new source needs a source-specific adapter. Reuse an adapter only when it was
designed for the same platform contract and accepts source configuration. Keep
the retrieval profile as the independent audit and fallback path.

#### Create a new adapter

1. Read `crawler/contracts.py`, `crawler/runner.py`, and the adapter and test
   that most closely match the new site.
2. Add `crawler/adapters/<adapter_id>.py`. Define `ADAPTER_ID`,
   `PARSER_VERSION`, and a class with `id`, `version`, `__init__(client,
   artifacts=None)`, and `crawl(source, seen_date, lookahead_days, timezone)`.
3. In `crawl`:
   - Fetch through `self.client`; do not bypass robots rules or the shared rate
     limit.
   - Save the listing and useful detail samples with `ArtifactRecorder`.
   - Discover stable event URLs or IDs, then filter the inclusive date window
     before detail requests when the listing has reliable dates.
   - Prefer public structured data or APIs. Parse HTML only for facts that the
     structured data does not contain.
   - Emit each event with the required `RawEvent` fields and provenance from
     `crawler/contracts.py`.
   - Record factual exclusions as rejections. Never apply ranking criteria.
   - Accept zero events only with a proven empty signal or complete ordered
     coverage of the requested window. Otherwise use `empty_suspicious`.
   - Raise `ParseError` when the source structure no longer meets the contract,
     or return `validation_failed` when individual records are malformed.
   - Return the payload through `source_result(...)`.
4. Add minimal sanitized fixtures under `tests/fixtures/` and add
   `tests/test_<adapter_id>_adapter.py`. Test the normal result, inclusive window
   filtering, the explicit empty signal, malformed structure, required fields,
   and saved artifact provenance. Do not use the live site in unit tests.
5. Register the class in `crawler/registry.py`, then run:

   ```sh
   python3 -m unittest tests.test_adapter_registry tests.test_<adapter_id>_adapter
   python3 -m unittest discover -s tests
   ```

Do not save an enabled source until these tests pass. If safe procedural
retrieval is not possible, keep the source disabled with a precise reason and
tell the user what must change before it can be enabled.

### 6. Draft the entry

Follow the existing style. Required fields:

```json
{
  "id": "kebab-case-unique-id",
  "title": "Human-readable name",
  "url": "https://the-most-specific-events-or-calendar-url",
  "priority": 1,
  "geo": "City, area, or neighborhood",
  "parse_hint": "...",
  "retrieval_profile": "chosen-profile",
  "adapter": "adapter_registry_id"
}
```

Only use an adapter ID that exists in `crawler/registry.py` and has fixture
tests. A source with an adapter stays in the same catalog and keeps the same
source ID and run history.

- Use the deepest stable page that lists events.
- Use priority 1 for proven high-value sources, 2 for normal sources, and 3 for low-volume or unproven sources.
- Write `parse_hint` for a future agent. State the page structure, priorities, empty signal, overlap, and traps.
- Do not set `enabled: false` on a new source. Disabling an existing source requires a `disabled_reason` that says what would justify re-enabling it.

### 7. Get confirmation

Show the draft to the user and get confirmation before you write it. State any uncertainty.

### 8. Insert through the repository layer

From the repository root:

```sh
python3 - <<'PY'
from web.repository import Repository

source = {
    "id": "...",
    "title": "...",
    "url": "...",
    "priority": 1,
    "geo": "...",
    "parse_hint": "...",
    "retrieval_profile": "...",
    "adapter": "...",
}
repo = Repository(root=".")
repo.upsert_source(source)
PY
```

For updates, use `upsert_source(source, original_id=...)`. Warn the user before you rename an ID because the ID links to history.

### 9. Validate

Run:

```sh
python3 -m unittest tests.test_adapter_registry ADAPTER_TEST_MODULE
python3 tools/events_store.py catalog-check
```

Both commands must pass. Then confirm that the source appears correctly in the
web control center.

### 10. Report

Tell the user what changed, which retrieval profile you chose and why, that it will be included in the next full crawl, and any operational warning.

## Other source changes

- To disable a source, set `enabled: false` and add a `disabled_reason` that says what would justify re-enabling it.
- Do not delete a source with history unless the user explicitly asks for complete removal. Otherwise, prefer disabling it or moving it to `removed` with a reason.
