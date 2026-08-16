# Event Ranking Criteria

Copy this file to `EVENT_RANKING_CRITERIA.md`, and replace the examples with your own criteria. The local file is ignored by Git.

These criteria apply only after the crawler retrieves, validates, and deduplicates events. They do not control source selection, page retrieval, parsing, or factual ingestion. A poor-fit event stays in the event store with a low rank.

## Ranking scale

- **High:** A strong match for the positive criteria, with no hard conflict.
- **Medium:** A possible match with missing evidence or an important tradeoff.
- **Low:** A hard conflict, a poor match, or no useful evidence of fit.

## Who this is for

Example: An adult who wants nearby events that make it easy to meet people and take part in a shared activity.

## Strong fit signals

- The format helps attendees talk or work together.
- A recurring group gives attendees a reason to return.
- The location and schedule are practical.
- The subject matches the person's interests or experience.
- The listing gives clear evidence about the format.

## Medium-fit signals

- Interaction is likely, but the listing does not explain the format.
- The event matches an interest but has a longer trip or a schedule conflict.
- The event is new, and there are no reliable field notes about the crowd.

## Automatic low-fit rules

These rules cause a low rank. They do not cause retrieval rejection.

- The format is passive and has no discussion or shared activity.
- The attendee cannot join because of a stated access rule.
- The time or travel requirement is not practical.
- The event conflicts with a strong personal preference.

## Logistics

- Example travel limit: 45 minutes by public transit.
- Example schedule rule: Weeknights after 6:00 p.m. and weekend afternoons work best.
- Example budget rule: Prefer events below $50, but allow a higher price for an exceptional fit.

## Evidence rules

- Prefer direct format details over promotional claims.
- Treat crowd estimates as uncertain unless they come from direct attendance notes.
- State important missing information in the fit note.

## Field notes

Add observations after you attend an event. Include the group size, interaction format, general crowd, and whether you would return.

- Example: The organizer made introductions, and most attendees stayed for the full discussion.

## Ranking overrides

### Minimum rank for a host or series

- Example: Rank a trusted recurring group at least medium unless the event has a hard conflict.

### Always rank low

- Example: A group that you tried and do not want recommended again.

## Additional ranking instructions

- Example: Give a small preference to events within walking distance.
