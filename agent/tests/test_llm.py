from agent.llm import _extract_text_tool_calls


def test_extract_text_tool_calls_parses_hermes4_xml_tool_call():
    content = """
<think>
I'll search memory first.
</think>
<tool_call>
{"name":"search_memory","arguments":{"query":"Susan career transition","limit":3}}
</tool_call>
"""

    tool_calls = _extract_text_tool_calls(content)

    assert tool_calls == [
        {
            "id": "call_0_search_memory",
            "function": {
                "name": "search_memory",
                "arguments": {"query": "Susan career transition", "limit": 3},
            },
        }
    ]


def test_extract_text_tool_calls_parses_hermes4_xml_with_stringified_arguments():
    content = """
<tool_call>
{"name":"queue_outbound_action","arguments":"{\\"tool_name\\": \\"send_email\\", \\"args\\": {\\"to\\": \\"a@example.com\\"}}"}
</tool_call>
"""

    tool_calls = _extract_text_tool_calls(content)

    assert tool_calls == [
        {
            "id": "call_0_queue_outbound_action",
            "function": {
                "name": "queue_outbound_action",
                "arguments": {
                    "tool_name": "send_email",
                    "args": {"to": "a@example.com"},
                },
            },
        }
    ]

