def _build_origin_description() -> str:
    from agent.accounts import get_location
    home = get_location("home") or "home address"
    work = get_location("work") or "work address"
    return (
        f"Starting address or place name. "
        f"Use '{home}' if they say 'from home' or don't specify an origin. "
        f"Use '{work}' if they say 'from work'."
    )


MEMORY_TOOLS = [
    {
        "type": "function",
        "side_effects": True,
        "function": {
            "name": "save_memory",
            "description": "Save an important piece of information to recall memory for future reference. Use this when you learn something important about your owner that should be remembered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "What to remember — be specific and include context",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Importance score 0.0-1.0 (1.0=critical life event, 0.5=useful, 0.0=trivial)",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_memory",
            "description": (
                "Search your memory for information relevant to a query. "
                "ALWAYS call this before answering questions about past events, decisions, or people. "
                "Also call this whenever the owner asks what you remember, what's in your memory, "
                "or anything about a previous conversation — never answer those from your own recall, "
                "always run this tool first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": True,
        "function": {
            "name": "update_life_context",
            "description": "Update a section of the life context document when you learn something important and lasting about your owner. Use sparingly — only for genuine updates to their situation, not every conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "The section name to update (e.g. 'What I Am Responsible For', 'My Family')",
                    },
                    "content": {
                        "type": "string",
                        "description": "The new or updated content for this section",
                    },
                },
                "required": ["section", "content"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_driving_time",
            "description": "Get real-time driving duration and distance between two locations using Google Maps. ONLY use when the owner is explicitly asking how long a drive takes, how far away a specific place is, or for turn-by-turn directions. Do NOT use for account status questions, logistics planning, or any question that merely mentions a location name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": _build_origin_description(),
                    },
                    "destination": {
                        "type": "string",
                        "description": "Destination address or place name",
                    },
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_web",
            "description": "Search the web for current information. Use when the owner asks about news, current events, prices, or anything that requires up-to-date information not in your training data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": True,
        "function": {
            "name": "mark_commitment_complete",
            "description": "Mark a commitment or promise as completed. Use when your owner tells you they've followed through on something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Description of the commitment that was completed",
                    },
                },
                "required": ["description"],
            },
        },
    },
]
