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

### 5. Draft the entry

Follow the existing style. Required fields:

```json
{
  "id": "kebab-case-unique-id",
  "title": "Human-readable name",
  "url": "https://the-most-specific-events-or-calendar-url",
  "priority": 1,
  "geo": "City, area, or neighborhood",
  "parse_hint": "...",
  "retrieval_profile": "chosen-profile"
}
```

An existing procedural adapter can also be selected with:

```json
"adapter": "adapter_registry_id"
```

Only add this field when the adapter exists in `crawler/registry.py` and has
fixture tests. The retrieval profile stays required because it defines the
independent audit and fallback path. A source with an adapter stays in the same
catalog and keeps the same source ID and run history.

- Use the deepest stable page that lists events.
- Use priority 1 for proven high-value sources, 2 for normal sources, and 3 for low-volume or unproven sources.
- Write `parse_hint` for a future agent. State the page structure, priorities, empty signal, overlap, and traps.
- Do not set `enabled: false` on a new source. Disabling an existing source requires a `disabled_reason` that says what would justify re-enabling it.

### 6. Get confirmation

Show the draft to the user and get confirmation before you write it. State any uncertainty.

### 7. Insert through the repository layer

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
}
repo = Repository(root=".")
repo.upsert_source(source)
PY
```

For updates, use `upsert_source(source, original_id=...)`. Warn the user before you rename an ID because the ID links to history.

### 8. Validate

Run:

```sh
python3 tools/events_store.py catalog-check
```

It must pass. Then confirm that the source appears correctly in the web control center.

### 9. Report

Tell the user what changed, which retrieval profile you chose and why, that it will be included in the next full crawl, and any operational warning.

## Other source changes

- To disable a source, set `enabled: false` and add a `disabled_reason` that says what would justify re-enabling it.
- Do not delete a source with history unless the user explicitly asks for complete removal. Otherwise, prefer disabling it or moving it to `removed` with a reason.
