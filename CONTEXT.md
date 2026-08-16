# Social Event Crawler

This project discovers events from a configured source catalog, judges their fit for the operator, and keeps the reliability of each source visible.

## Language

**Source Catalog**:
The maintained set of places to check, including the retrieval recipe and status for each place.
_Avoid_: Crawl history

**Run**:
One complete attempt to discover and store events at a particular point in time.
_Avoid_: Source Run, event

**Source Run**:
The attempt to retrieve and interpret one catalog source during a Run.
_Avoid_: Source, event

**Discovery**:
An event candidate reported by a Source Run before or during validation, filtering, and deduplication.
_Avoid_: Event

**Event**:
One deduplicated, dated event instance with its fit assessment.
_Avoid_: Discovery, series

**Event URL alias**:
One event-specific web address that points to an Event. One Event can have aliases from more than one website.
_Avoid_: Event ID, source calendar

**Artifact**:
Raw evidence retained to diagnose a Source Run, such as a response body, screenshot, or browser capture.
_Avoid_: Discovery, event

**Adapter**:
A tested Python implementation that discovers and parses one source without an LLM during a normal run.
_Avoid_: Retrieval profile, source

**Event Stub**:
An event detail URL and small listing facts found by an Adapter before the detail page is downloaded.
_Avoid_: Discovery, Event

**Raw Event**:
A factual source record with fetch and parser provenance before canonical deduplication and fit assessment.
_Avoid_: Event, Event Assessment
