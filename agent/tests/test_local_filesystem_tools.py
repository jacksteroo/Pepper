from __future__ import annotations

from unittest.mock import patch

from agent.local_filesystem_tools import (
    extract_path_from_text,
    inspect_local_path_sync,
    resolve_local_path,
)


def test_extract_path_from_text_absolute():
    assert extract_path_from_text("What is in /data/messages folder?") == "/data/messages"


def test_extract_path_from_text_relative_repo_path():
    assert extract_path_from_text("Do you have access to docs/LIFE_CONTEXT.md?") == "docs/LIFE_CONTEXT.md"


def test_resolve_local_path_maps_host_repo_docs_path(tmp_path):
    app_root = tmp_path / "app"
    docs_dir = app_root / "docs"
    docs_dir.mkdir(parents=True)
    target = docs_dir / "LIFE_CONTEXT.md"
    target.write_text("hello")

    with patch("agent.local_filesystem_tools._REPO_ROOT", app_root), \
         patch("agent.local_filesystem_tools._APP_ROOT", app_root):
        resolved, mapped_from = resolve_local_path("/Users/jack/Developer/Pepper/docs/LIFE_CONTEXT.md")

    assert resolved == target
    assert mapped_from == "/Users/jack/Developer/Pepper/docs/LIFE_CONTEXT.md"


def test_inspect_local_path_sync_lists_directory(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "LIFE_CONTEXT.md").write_text("hi")
    (docs_dir / "notes.txt").write_text("hello")

    with patch("agent.local_filesystem_tools._REPO_ROOT", tmp_path), \
         patch("agent.local_filesystem_tools._APP_ROOT", tmp_path):
        result = inspect_local_path_sync("docs", max_entries=10, max_chars=100)

    assert result["kind"] == "directory"
    assert result["entry_count"] == 2
    assert {entry["name"] for entry in result["entries"]} == {"LIFE_CONTEXT.md", "notes.txt"}


def test_inspect_local_path_sync_reads_text_file(tmp_path):
    path = tmp_path / "docs" / "LIFE_CONTEXT.md"
    path.parent.mkdir()
    path.write_text("Jack lives here")

    with patch("agent.local_filesystem_tools._REPO_ROOT", tmp_path), \
         patch("agent.local_filesystem_tools._APP_ROOT", tmp_path):
        result = inspect_local_path_sync("docs/LIFE_CONTEXT.md", max_chars=100)

    assert result["kind"] == "file"
    assert result["previewable_text"] is True
    assert "Jack lives here" in result["content"]
