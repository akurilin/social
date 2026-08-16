# Separate the static source catalog from operational state

Keep maintained source definitions and retrieval recipes in the local,
ignored `sources.json` file. Use the tracked `sources.example.json` file as the
starting template. Store runs, per-source outcomes, discoveries, deduplicated
events, and fit assessments in a local SQLite database. The web control center
edits and validates the local catalog, so the user does not need to work with
the file directly. This provides an auditable history of empty, failed,
rejected, duplicate, and successful retrievals without a server database.

Keep personal fit rules in the local, ignored `EVENT_RANKING_CRITERIA.md` file.
Apply them only after retrieval, factual validation, and deduplication. The
tracked `EVENT_RANKING_CRITERIA.example.md` file documents the expected form
without storing a user's personal criteria in Git.
