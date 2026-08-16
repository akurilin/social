# Use procedural adapters as the primary path for stable sources

Status: accepted on 2026-08-15.

## Context

The crawl ledger already plans sources, stages discoveries, deduplicates events,
and queues new events for assessment. Retrieval was still executed by Codex
Desktop for each source, even when a site had stable HTTP data.

This used an LLM for repeatable download and parsing work. It also kept source
knowledge in prose instead of executable, tested code.

## Decision

A catalog source can select a procedural Python adapter with an optional
`adapter` field. The source keeps its existing source ID, retrieval profile,
and run history. There is no second source catalog.

The run plan copies the adapter ID into the work row. `tools/crawl_run.py`
retrieves pending adapters with a bounded thread pool. Its main thread stages
the results in stable source order through the existing `source_runs` and
`discoveries` contract. `tools/crawl_source.py` remains available for one-source
tests and retries. The existing finalizer remains responsible for validation,
aliases, deduplication, and canonical events.

An adapter does factual work only:

1. Discover event detail URLs.
2. Download public source data.
3. Extract a raw event with provenance.
4. Normalize source facts into the ingestion contract.
5. Reject clear date-window and parser failures.

Before staging, the runner validates the complete adapter result with strict
runtime models. The models enforce the source-result envelope, raw-event fields,
provenance, artifacts, timestamps, and field types. Rejection evidence can keep
any JSON shape because it often records malformed source data. A schema failure
becomes a visible parse failure.

The LLM still assesses a canonical event against the local
`EVENT_RANKING_CRITERIA.md` file. It reads that file only after retrieval,
factual validation, and deduplication. It does not download or parse stable
source pages during a normal adapter run, and the ranking criteria never change
which factual candidates the adapter emits.

The retrieval profile stays on every adapted source. It defines the independent
browser or agent audit and fallback path. An adapter failure must be visible as
a source-run failure. It must not silently return an empty result.

The first adapter is `out_there`. It uses listing `ItemList` JSON-LD, event
`Event` JSON-LD, and a small HTML rule for explicit age and format labels.

## Consequences

- Normal Out There runs use HTTP and Python without a browser or LLM.
- Raw discoveries retain the parser version, fetch time, content hash, source
  event ID, source URL, and extracted JSON.
- Sample raw HTML stays in `.cache` for inspection.
- Direct signup URLs become event aliases and improve cross-source matching.
- Sources without adapters continue to use the existing Codex Desktop path.
- Procedural sources can retrieve in parallel without concurrent SQLite writes.
- Adapter changes require fixture tests and a live audit sample.
