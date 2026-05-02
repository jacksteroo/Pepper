"""CapabilityBlockSelector unit tests."""
from __future__ import annotations

import json

from agent.capability_registry import CapabilityRegistry, CapabilityStatus
from agent.context.selectors import CapabilityBlockSelector


def _registry_with(available: list[str]) -> CapabilityRegistry:
    """Build a CapabilityRegistry stub seeded with the given sources marked AVAILABLE.

    We bypass ``populate`` (which probes the OS / OAuth tokens) by directly
    invoking the protected ``_set`` so tests stay hermetic.
    """
    reg = CapabilityRegistry()
    for src in available:
        reg._set(src, src.replace("_", " ").title(), CapabilityStatus.AVAILABLE)
    return reg


def test_select_with_registry_reports_available_sources() -> None:
    reg = _registry_with(["calendar_google", "email_gmail"])
    sel = CapabilityBlockSelector(capability_registry=reg)
    rec = sel.select()

    assert rec.name == "capability_block"
    assert isinstance(rec.content, str)
    prov = rec.provenance
    assert prov["registry_present"] is True
    assert "calendar_google" in prov["available_sources"]
    assert "email_gmail" in prov["available_sources"]
    assert prov["block_chars"] == len(rec.content)


def test_select_without_registry() -> None:
    sel = CapabilityBlockSelector(capability_registry=None)
    rec = sel.select()
    assert rec.name == "capability_block"
    assert rec.provenance["registry_present"] is False
    assert rec.provenance["available_sources"] == []


def test_provenance_is_json_serializable() -> None:
    sel = CapabilityBlockSelector(
        capability_registry=_registry_with(["slack"]),
    )
    rec = sel.select()
    json.dumps(rec.provenance)


def test_failing_registry_does_not_propagate(monkeypatch) -> None:
    """A registry that explodes during ``get_available_sources`` must not
    crash the assembler. The selector swallows the error and reports an
    empty list in provenance.
    """
    reg = CapabilityRegistry()

    def _boom(self) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(CapabilityRegistry, "get_available_sources", _boom)
    sel = CapabilityBlockSelector(capability_registry=reg)
    rec = sel.select()
    assert rec.provenance["available_sources"] == []
