from agent.query_intents import (
    CALENDAR_QUERY_TERMS,
    EMAIL_QUERY_TERMS,
    IMESSAGE_QUERY_TERMS,
    NON_EMAIL_CHANNEL_TERMS,
    SLACK_QUERY_TERMS,
    WHATSAPP_QUERY_TERMS,
    infer_calendar_days,
    infer_recent_hours,
    is_action_item_request,
    is_attention_request,
    is_source_query,
)


def test_is_attention_request_reused_for_text_messages():
    assert is_attention_request(
        "Summarize my text messages",
        IMESSAGE_QUERY_TERMS,
        extra_terms=("who texted", "any texts"),
    ) is True


def test_is_attention_request_reused_for_whatsapp():
    assert is_attention_request(
        "What recent WhatsApp messages do I need to be aware of?",
        WHATSAPP_QUERY_TERMS,
    ) is True


def test_is_source_query_supports_slack_deadline_language():
    assert is_source_query(
        "What deadlines do I have from Slack?",
        SLACK_QUERY_TERMS,
    ) is True


def test_email_query_does_not_match_other_messaging_sources():
    assert is_source_query(
        "What recent WhatsApp messages do I need to be aware of?",
        EMAIL_QUERY_TERMS,
        disallowed_terms=NON_EMAIL_CHANNEL_TERMS,
    ) is False


def test_is_action_item_request_reused_for_email():
    assert is_action_item_request(
        "Any action items from my personal email?",
        EMAIL_QUERY_TERMS,
        disallowed_terms=NON_EMAIL_CHANNEL_TERMS,
    ) is True


def test_infer_recent_hours_reused_across_sources():
    assert infer_recent_hours("Summarize my emails received overnight.") == 12
    assert infer_recent_hours("Catch me up on messages from this week.") == 168


def test_infer_calendar_days_reused_for_schedule_queries():
    assert infer_calendar_days("What do I have today?") == 1
    assert infer_calendar_days("What's on my calendar tomorrow?") == 2
    assert infer_calendar_days("What's on my schedule next week?") == 14


def test_calendar_source_query_handles_schedule_phrasing():
    assert is_source_query(
        "What do I have on my schedule today?",
        CALENDAR_QUERY_TERMS,
        extra_terms=("today", "tomorrow", "this week", "next week", "coming up"),
    ) is True
