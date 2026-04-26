from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import structlog

logger = structlog.get_logger()

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = Path("/app")
_DATA_ROOT = Path("/data")
_MOUNTED_REPO_DIRS = ("agent", "subsystems", "docs", "logs")
_TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".env",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".csv",
    ".sql",
    ".log",
}
_PATH_RE = re.compile(
    r"(?P<path>(?:/(?!/)[^\s\"'`<>]+|(?:docs|agent|subsystems|logs)/[^\s\"'`<>]+))",
    re.IGNORECASE,
)


FILESYSTEM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_local_path",
            "description": (
                "Inspect a local mounted file or directory on this machine, read-only. "
                "Use this for questions about paths like /data/messages, /data/whatsapp, "
                "docs/LIFE_CONTEXT.md, or mounted repo files. It can list directory entries "
                "or preview text files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or repo-relative path to inspect.",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "Maximum number of directory entries to return.",
                        "default": 20,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum number of text characters to preview.",
                        "default": 2000,
                    },
                },
                "required": ["path"],
            },
        },
    }
]


def extract_path_from_text(text: str) -> str | None:
    if not text:
        return None
    match = _PATH_RE.search(text)
    if not match:
        return None
    path = match.group("path").rstrip(".,!?;:)]}\"'>")
    return path or None


def allowed_roots() -> list[Path]:
    roots = [_REPO_ROOT]
    if _APP_ROOT != _REPO_ROOT:
        roots.append(_APP_ROOT)
    if _DATA_ROOT.exists():
        roots.append(_DATA_ROOT)
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(root.resolve())
    return unique


def _map_host_repo_path(path: Path) -> Path | None:
    parts = path.parts
    for anchor in _MOUNTED_REPO_DIRS:
        if anchor not in parts:
            continue
        idx = max(i for i, part in enumerate(parts) if part == anchor)
        candidate = _APP_ROOT.joinpath(*parts[idx:])
        return candidate
    return None


def resolve_local_path(raw_path: str) -> tuple[Path | None, str | None]:
    if not raw_path:
        return None, None

    requested = Path(os.path.expanduser(raw_path))
    mapped_from: str | None = None

    candidates: list[Path] = []
    if requested.is_absolute():
        candidates.append(requested)
        mapped = _map_host_repo_path(requested)
        if mapped is not None:
            candidates.append(mapped)
    else:
        candidates.append(_REPO_ROOT / requested)
        if _APP_ROOT != _REPO_ROOT:
            candidates.append(_APP_ROOT / requested)

    roots = allowed_roots()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        if any(resolved.is_relative_to(root) for root in roots):
            if str(resolved) != str(requested):
                mapped_from = str(requested)
            return resolved, mapped_from

    return None, None


def _is_text_previewable(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_EXTENSIONS


def inspect_local_path_sync(
    raw_path: str,
    *,
    max_entries: int = 20,
    max_chars: int = 2000,
) -> dict:
    resolved, mapped_from = resolve_local_path(raw_path)
    roots = [str(root) for root in allowed_roots()]
    if resolved is None:
        return {
            "error": (
                "Path is outside Pepper's read-only local filesystem scope. "
                f"Allowed roots: {', '.join(roots)}"
            ),
            "requested_path": raw_path,
            "allowed_roots": roots,
        }

    if not resolved.exists():
        return {
            "error": f"Path not found: {resolved}",
            "requested_path": raw_path,
            "resolved_path": str(resolved),
            "mapped_from": mapped_from,
        }

    if resolved.is_dir():
        try:
            entries = sorted(
                resolved.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError as exc:
            return {
                "error": f"Couldn't read directory {resolved}: {exc}",
                "requested_path": raw_path,
                "resolved_path": str(resolved),
                "mapped_from": mapped_from,
            }

        listed = []
        for entry in entries[: max(1, max_entries)]:
            try:
                size_bytes = entry.stat().st_size if entry.is_file() else None
            except OSError:
                size_bytes = None
            listed.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "kind": "directory" if entry.is_dir() else "file",
                    "size_bytes": size_bytes,
                }
            )
        return {
            "requested_path": raw_path,
            "resolved_path": str(resolved),
            "mapped_from": mapped_from,
            "kind": "directory",
            "entry_count": len(entries),
            "entries": listed,
            "truncated": len(entries) > max_entries,
        }

    try:
        stat = resolved.stat()
    except OSError as exc:
        return {
            "error": f"Couldn't stat file {resolved}: {exc}",
            "requested_path": raw_path,
            "resolved_path": str(resolved),
            "mapped_from": mapped_from,
        }

    result = {
        "requested_path": raw_path,
        "resolved_path": str(resolved),
        "mapped_from": mapped_from,
        "kind": "file",
        "size_bytes": stat.st_size,
        "previewable_text": False,
    }

    if not _is_text_previewable(resolved):
        return result

    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        result["error"] = f"Couldn't read text file {resolved}: {exc}"
        return result

    preview = content[: max(1, max_chars)]
    result["previewable_text"] = True
    result["content"] = preview
    result["truncated"] = len(content) > max_chars
    return result


async def execute_inspect_local_path(args: dict) -> dict:
    path = str(args.get("path", "")).strip()
    if not path:
        return {"error": "inspect_local_path requires a non-empty 'path' argument"}

    max_entries = int(args.get("max_entries", 20) or 20)
    max_chars = int(args.get("max_chars", 2000) or 2000)

    result = await asyncio.to_thread(
        inspect_local_path_sync,
        path,
        max_entries=max_entries,
        max_chars=max_chars,
    )
    logger.debug(
        "inspect_local_path_completed",
        requested_path=path,
        resolved_path=result.get("resolved_path"),
        kind=result.get("kind"),
        error=result.get("error", ""),
    )
    return result
