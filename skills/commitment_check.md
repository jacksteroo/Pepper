---
name: commitment_check
description: Surface open commitments and promises — especially those overdue or about to slip
triggers:
  - commitment check
  - check my commitments
  - what did i promise
  - pending commitments
  - open commitments
  - what do i owe
  - what am i behind on
  - follow up check
tools:
  - search_memory
model: local
version: 1
---

## Workflow

1. Call `search_memory` with query `"COMMITMENT OR I will OR I'll OR follow up OR promised OR I'll send OR I'll intro OR I'll reach out"` (limit 15).

2. Filter the results:
   - Skip anything starting with `[RESOLVED]` — it is done.
   - Group remaining items by approximate age:
     - **Overdue** (older than 48 hours and still open): flag clearly
     - **Recent** (within 48 hours): show but don't alarm

3. For each overdue commitment, show:
   - What was promised (in plain language)
   - Approximately when it was made (relative: "3 days ago", "last week")
   - A one-line suggested next action (e.g., "Send the intro email to X", "Reply to Y's question")

4. If there are no pending commitments: say "No open commitments found." and stop.

5. If there are pending commitments, close with the count:
   "You have N open commitment(s). The oldest is [description]."

Do not invent commitments. Only surface what appears in memory results.
