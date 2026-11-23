---
name: enable-source
description: Enable or re-enable an existing source for future social crawler runs.
---

From the repository root, use `web.repository.Repository`.

1. Load the catalog with `load_catalog()`. Identify the entry by ID in
   `sources` or `inbox_sources.items`. Ask if the match is ambiguous.
2. If it is disabled, set `enabled` to `true` and remove `disabled_reason`.
   Save the catalog with `save_catalog(catalog)`. This validates the change.
3. Reload the catalog. Verify the source is enabled and report its status.

The change applies to new run plans.
