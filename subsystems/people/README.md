# People Subsystem — Stub

**Status**: Future integration  

This subsystem will eventually expose relationship intelligence as a standard Pepper subsystem interface.

## Integration is NOT Phase 1

This layer is intentionally deferred because:

1. The relationship data hasn't been fully curated
2. Pepper needs to be operational first to know what relationship data it actually needs
3. Pepper should expand this layer based on observed gaps, not speculation

## What Pepper Will Drive in the People Layer Over Time

- Populate contacts from iMessage, email, and calendar attendees Pepper encounters
- Flag relationship health issues based on Pepper's broader life context (not just message frequency)
- Recommend People subsystem improvements based on questions Pepper can't answer
- Provide richer context for people who appear across multiple life domains

## When Integration Happens

The trigger is: Pepper repeatedly fails to answer a people/relationship question and that failure points to a clear capability gap. That surfaces the integration as a real need, not a hypothetical one.

## Interface Contract (to be implemented)

```text
GET  /health
GET  /tools           # People tools relevant to Pepper
POST /tools/{name}    # Execute tool
GET  /status
```
