---
name: morning_brief
description: Comprehensive morning brief — calendar, inbox snapshot, open loops, and pending commitments
triggers:
  - morning brief
  - generate my brief
  - daily brief
  - morning update
  - start of day brief
  - good morning brief
tools:
  - get_upcoming_events
  - get_email_unread_counts
  - get_comms_health_summary
  - search_memory
model: local
version: 1
---

## Workflow

1. Open with today's date in a natural greeting — one line, no filler.

2. Call `get_upcoming_events` with `days: 1` to surface today's calendar.
   - List each event on its own line: time — title — location (if any).
   - If no events, say "Clear calendar today."

3. Call `search_memory` with query `"open loop OR unresolved OR pending OR waiting"` (limit 5).
   - Surface any items that still need attention. Skip anything obviously stale or completed.

4. Call `search_memory` with query `"COMMITMENT"` (limit 5).
   - Show commitments that do NOT start with `[RESOLVED]`.
   - If all are resolved or none exist, skip this section.

5. Call `get_email_unread_counts` for a quick inbox snapshot.
   - Show total unread count per account. One line per account.
   - Skip accounts with 0 unread.

6. Call `get_comms_health_summary` with `quiet_days: 14`.
   - Surface at most 2 signals: e.g. a contact you haven't responded to, someone who
     has been reaching out, or an overdue reply.
   - Skip this section entirely if the result has no signals.

7. Synthesize into a brief, direct morning message:
   - Section order: date → calendar → open loops → commitments → inbox → comms health
   - Lead with the single most important thing (urgent meeting, overdue commitment, or high unread count)
   - Max 6 bullet points total across all sections
   - Be concrete — use real names, real titles, real numbers
   - No placeholders, no "TBD", no invented data
