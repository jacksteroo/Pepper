---
name: weekly_review
description: Weekly review — what happened, what's still open, and what needs attention next week
triggers:
  - weekly review
  - week in review
  - review my week
  - weekly summary
  - end of week review
  - how was my week
tools:
  - search_memory
  - get_upcoming_events
model: local
version: 1
---

## Workflow

1. Open with the current week label (e.g., "Week of April 14").

2. Call `search_memory` with query `"week OR happened OR learned OR shipped OR completed"` (limit 10).
   - Summarize what occurred this week in 2–4 bullets: decisions made, things shipped, people connected with.
   - Be specific — use real names and projects from the memory results.

3. Call `search_memory` with query `"COMMITMENT OR open loop OR unresolved OR pending"` (limit 8).
   - Identify items that are still unresolved.
   - Group by: overdue (should have happened this week) vs. carry-forward (still valid for next week).
   - Skip anything starting with `[RESOLVED]`.

4. Call `get_upcoming_events` with `days: 7` for next week's calendar.
   - Highlight any deadline-adjacent events (reviews, presentations, travel, important meetings).
   - Note any conflicts or back-to-back blocks worth flagging.

5. End with 1–2 forward-looking priorities:
   - What is the single most important thing for next week?
   - What risk or open loop needs resolution before Monday?

Keep the total output to 10 bullets or fewer. Be direct.
