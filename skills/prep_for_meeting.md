---
name: prep_for_meeting
description: Pre-meeting intelligence — attendees, recent context, open threads, and what to raise
triggers:
  - prep for meeting
  - prepare for meeting
  - meeting prep
  - before my meeting
  - about to meet
  - heading into a meeting
  - prep for my call
  - prepare for my call
  - call prep
  - what should i know before
tools:
  - get_upcoming_events
  - get_calendar_events_range
  - get_imessage_conversation
  - get_whatsapp_chat
  - search_emails
  - search_slack
  - get_contact_profile
  - search_memory
model: frontier
version: 1
---

## Workflow

1. Identify the meeting from the user's request:
   - If a meeting name or time is mentioned, use it.
   - Otherwise call `get_upcoming_events` with `days: 1` and ask: "Which of these is the meeting you're prepping for?"

2. Extract attendees from the event description. For each key attendee (skip generic room names):
   - Call `get_contact_profile` to see their dominant channel and last contact time.

3. For each key attendee, surface recent context (last 7 days):
   - Messaging: use whichever channel is their dominant channel from step 2.
     - iMessage dominant (or unknown): `get_imessage_conversation` (limit 8)
     - WhatsApp dominant: call `get_recent_whatsapp_chats` (limit 20) first to
       find the matching chat by name, then call `get_whatsapp_chat` with that
       `chat_id` (limit 8). If no matching chat is found, fall back to
       `get_imessage_conversation`.
   - Email: `search_emails` with `from:[name] OR to:[name]` (limit 5)
   - Slack: `search_slack` with the person's name (limit 5)
   - Summarize in 1–2 bullets per person: what was last discussed, any open asks.

4. Call `search_memory` with the meeting topic or attendee names to surface:
   - Prior commitments made to these people
   - Previous meeting outcomes or decisions
   - Any flagged concerns or open loops

5. Synthesize the brief:
   - **Context**: 2–3 sentences on what this meeting is about and its history
   - **People**: 1 bullet per attendee with the most relevant recent signal
   - **Open threads**: anything that was promised or left unresolved
   - **Suggested agenda items**: 2–3 concrete things to raise or resolve

6. Close with: "Anything specific you want me to dig into before you head in?"

Do not fabricate attendee names, history, or commitments. Only surface what the tools return.
