---
name: disable-source
description: Disable an existing source so future social crawler runs skip it.
---

From the repository root, use `web.repository.Repository`.

1. Load the catalog with `load_catalog()`. Identify the entry by ID in
   `sources` or `inbox_sources.items`. Ask if the match is ambiguous.
2. If it is enabled, set `enabled` to `false`. Add a short
   `disabled_reason` that states why and when to re-enable it. Save with
   `save_catalog(catalog)`. This validates the change and keeps the entry.
3. Reload the catalog. Verify the source is disabled and report its status.

The change applies to new run plans.
