"""Smoke tests for scripts/run_retrieval_eval.py — argument parsing only.

The DB-touching paths are covered locally by `--mode baseline/after/gate`
runs; this file verifies the argument-validation guard against path-
traversal-style `--tag` values reaches the user as a clean rejection."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def script_module():
    path = Path(__file__).resolve().parent.parent.parent / "scripts" / "run_retrieval_eval.py"
    spec = importlib.util.spec_from_file_location("_run_retrieval_eval", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "bad_tag",
    ["../escape", "..\\escape", "a/b", "a b", "tag;rm-rf", "tag\nnewline", ".."],
)
def test_main_rejects_traversal_in_tag(script_module, monkeypatch, capsys, bad_tag):
    monkeypatch.setattr(
        sys, "argv", ["run_retrieval_eval.py", "--mode", "after", "--tag", bad_tag]
    )
    rc = script_module.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "--tag" in err


@pytest.mark.parametrize("good_tag", ["bm25", "rrf", "recency-v2", "snake_case"])
def test_main_accepts_safe_tag_format(script_module, good_tag):
    assert script_module._TAG_RE.match(good_tag)
