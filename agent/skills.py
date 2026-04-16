"""Phase 4 skill system: load, match, and inject structured workflow skills.

Skills are SKILL.md files in the skills/ directory. Each file has YAML
frontmatter (name, description, triggers, tools, model, version) followed by
a markdown workflow body.

On each user turn, the SkillMatcher finds skills whose trigger phrases appear
in the message and injects their content into the system prompt as fenced
<skill> blocks. The model treats these as guidance, not user input.

Skills are opt-in: if no skill matches, Pepper reasons from scratch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Skills directory: skills/ at the repo root (parent of agent/)
_SKILLS_DIR = Path(__file__).parent.parent / "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    triggers: list[str]
    tools: list[str]
    model: str          # "local" | "frontier"
    version: int
    content: str        # Workflow body (no frontmatter)
    path: Path


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split raw text into (frontmatter_dict, body).

    Returns ({}, raw) if the file has no valid --- delimiters.
    """
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    fm_text = match.group(1)
    body = match.group(2).strip()

    try:
        import yaml  # pyyaml — declared in pyproject.toml
        fm = yaml.safe_load(fm_text) or {}
    except Exception:
        # Fallback: minimal parser that handles both scalar and list values.
        # Covers the exact frontmatter shape used by all skill files:
        #   key: scalar_value
        #   list_key:
        #     - item one
        #     - item two
        fm: dict = {}
        current_list_key: str | None = None
        for line in fm_text.splitlines():
            stripped = line.rstrip()
            if not stripped:
                continue
            # List item (indented dash)
            if stripped.lstrip().startswith("- "):
                if current_list_key is not None:
                    item = stripped.lstrip().removeprefix("- ").strip()
                    fm.setdefault(current_list_key, []).append(item)
                continue
            # Key-value line (no leading whitespace)
            if ":" in stripped and not stripped.startswith(" "):
                current_list_key = None
                k, _, v = stripped.partition(":")
                k = k.strip()
                v = v.strip()
                if v:
                    fm[k] = v
                else:
                    # No inline value — next indented dash lines are list items
                    current_list_key = k

    return fm, body


def _load_skill(path: Path) -> Skill | None:
    """Parse and validate a single skill file. Returns None on any error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skill_read_failed", path=str(path), error=str(e))
        return None

    fm, body = _parse_frontmatter(raw)

    name = fm.get("name", "")
    if not name:
        logger.warning("skill_missing_name", path=str(path))
        return None

    if not body:
        logger.warning("skill_empty_body", name=name, path=str(path))
        return None

    triggers = fm.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [triggers]

    tools = fm.get("tools") or []
    if isinstance(tools, str):
        tools = [tools]

    return Skill(
        name=str(name),
        description=str(fm.get("description", "")),
        triggers=[str(t).lower().strip() for t in triggers if t],
        tools=[str(t) for t in tools if t],
        model=str(fm.get("model", "local")),
        version=int(fm.get("version", 1)),
        content=body,
        path=path,
    )


def load_skills(skills_dir: Path | None = None) -> list[Skill]:
    """Load all *.md files from the skills directory.

    Skips files that fail validation with a warning — never crashes startup.
    """
    directory = skills_dir or _SKILLS_DIR
    if not directory.exists():
        logger.info("skills_dir_not_found", path=str(directory))
        return []

    skills: list[Skill] = []
    for path in sorted(directory.glob("*.md")):
        skill = _load_skill(path)
        if skill is not None:
            skills.append(skill)
            logger.info(
                "skill_loaded",
                name=skill.name,
                version=skill.version,
                triggers=skill.triggers[:3],
                tools=skill.tools[:5],
            )
        else:
            logger.warning("skill_load_skipped", path=str(path))

    logger.info("skills_loaded", count=len(skills))
    return skills


def validate_skills_against_tools(skills: list[Skill], available_tools: set[str]) -> None:
    """Log a warning for each skill that declares a tool not in available_tools.

    Skills are never disabled — this is advisory only. A missing tool will
    produce a tool-not-found error at runtime, which the model handles gracefully.
    """
    for skill in skills:
        missing = [t for t in skill.tools if t and t not in available_tools]
        if missing:
            logger.warning(
                "skill_declared_tools_unavailable",
                name=skill.name,
                missing_tools=missing,
            )


class SkillMatcher:
    """Matches user messages to skills and injects workflow guidance into prompts.

    Matching uses a fast literal-substring path on lowercased trigger phrases.
    Semantic similarity via pgvector is deferred — trigger coverage is sufficient
    for the initial skill library and can be layered in when needed.
    """

    def __init__(self, skills: list[Skill]) -> None:
        self._skills = skills

    @property
    def skills(self) -> list[Skill]:
        return self._skills

    def match(self, user_message: str, top_n: int = 3) -> list[Skill]:
        """Return up to top_n skills whose trigger phrases appear in the message.

        Scored by number of distinct triggers matched; ties broken alphabetically.
        Returns [] when no skills are loaded or none match.
        """
        if not self._skills:
            return []

        lower = user_message.lower()
        scored: list[tuple[int, str, Skill]] = []

        for skill in self._skills:
            hits = sum(1 for trigger in skill.triggers if trigger and trigger in lower)
            if hits > 0:
                scored.append((hits, skill.name, skill))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [s for _, _, s in scored[:top_n]]

    def inject_into_prompt(
        self,
        system_prompt: str,
        user_message: str,
        top_n: int = 3,
    ) -> str:
        """Append matched skill blocks to the system prompt.

        Each skill is wrapped in <skill name="...">...</skill> tags so the model
        treats the content as structured guidance rather than user input.

        Returns system_prompt unchanged if no skills match.
        """
        matched = self.match(user_message, top_n=top_n)
        if not matched:
            return system_prompt

        logger.info(
            "skills_matched",
            count=len(matched),
            names=[s.name for s in matched],
            message_preview=user_message[:80],
        )

        blocks = [
            f'<skill name="{skill.name}">\n{skill.content}\n</skill>'
            for skill in matched
        ]
        return system_prompt + "\n\n" + "\n\n".join(blocks)
