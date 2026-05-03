"""Issue #33: capability_block_version is a stable hash that changes when the
underlying capability registry state changes."""
from __future__ import annotations

from agent.context.selectors import CapabilityBlockSelector


class _Status:
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"


class _StubRegistry:
    """Minimal duck-typed registry for the selector."""

    def __init__(self, available: list[str]) -> None:
        self._available = list(available)
        # Ensure ``get_status`` returns AVAILABLE for these keys, anything
        # else falls back to NOT_CONFIGURED. ``build_capability_block``
        # uses these statuses to render notes; the selector hashes the
        # result.
        from agent.capability_registry import CapabilityStatus

        self._statuses = {k: CapabilityStatus.AVAILABLE for k in available}

    def get_status(self, key: str):
        from agent.capability_registry import CapabilityStatus

        return self._statuses.get(key, CapabilityStatus.NOT_CONFIGURED)

    def get_available_sources(self) -> list[str]:
        return list(self._available)

    def get(self, key: str):
        return None


def test_version_is_stable_when_registry_unchanged() -> None:
    sel = CapabilityBlockSelector(capability_registry=None)
    a = sel.select().provenance["capability_block_version"]
    sel.refresh()
    b = sel.select().provenance["capability_block_version"]
    assert a == b
    assert isinstance(a, str)
    assert len(a) == 12  # 12-char hash prefix


def test_version_changes_when_registry_changes() -> None:
    sel_a = CapabilityBlockSelector(capability_registry=_StubRegistry(["calendar_google"]))
    sel_b = CapabilityBlockSelector(capability_registry=_StubRegistry(["whatsapp"]))
    va = sel_a.select().provenance["capability_block_version"]
    vb = sel_b.select().provenance["capability_block_version"]
    assert va != vb


def test_version_is_lowercase_hex() -> None:
    sel = CapabilityBlockSelector(capability_registry=None)
    v = sel.select().provenance["capability_block_version"]
    int(v, 16)  # raises if not valid hex
    assert v.lower() == v
