# Working agreements — read before touching anything in this folder

House rules for any agent working on the social event crawler.

## Protect user-authored ranking criteria

`EVENT_RANKING_CRITERIA.md` is the operator's local preference record. It is intentionally ignored by Git. `EVENT_RANKING_CRITERIA.example.md` is the tracked setup example.

Do not read or apply the local criteria during planning, retrieval, factual validation, or deduplication. Read them only during the final fit-ranking phase. Do not change the local file unless the operator explicitly asks for a change. The operator can also change it through the web control center.

## Do not duplicate state

| Record | Contains |
|---|---|
| `EVENT_RANKING_CRITERIA.md` | Local preferences, exclusions, context, field notes, and ranking overrides |
| `EVENT_RANKING_CRITERIA.example.md` | Version-controlled example for a new local ranking file |
| `.agents/skills/social-crawler/SKILL.md` | Version-controlled workflow contract for a crawl |
| `sources.json` | Local source catalog, retrieval profiles, source status, and source-specific notes |
| `sources.example.json` | Version-controlled starting template for a local source catalog |
| `social.db` | Dynamic runs, source results, discoveries, deduplicated events, and fit assessments |
| `.cache/` | Large raw responses, screenshots, and temporary crawl payloads |

`sources.json` says what must happen. `social.db` records what did happen. The web control center provides user access to both without making another copy.

## Never hand-edit social.db

Use `tools/events_store.py` or the web application's repository layer. These own schema changes, run history, deduplication keys, ID creation, merging, and field separation. Large raw artifacts belong in `.cache`; store their paths on the related Source Run.

Ranks are written once and kept. An event is a fixed date, room, and format. Re-rank only when the operator's ranking criteria change or new field knowledge applies. Use `needs-rank --all`, or use `needs-rank --host "Example Host"` for one recurring series.
