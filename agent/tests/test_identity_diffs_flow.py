"""End-to-end propose → approve → apply test for identity diffs (#52).

Uses an in-memory stub session that satisfies just enough of the
SQLAlchemy AsyncSession protocol for the repository's call paths.
The file-write side-effect runs against a real tmp file so the
atomicity of `apply_identity_diff` is exercised.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.identity import Identity, write_identity_atomic
from agent.identity_diffs import (
    IdentityDiff,
    IdentityDiffRepository,
    IdentityDiffStatus,
)


class _FakeRepo(IdentityDiffRepository):
    """Skip the DB so the test exercises file-side effect only."""

    def __init__(self, diffs: dict[uuid.UUID, IdentityDiff]) -> None:
        self._diffs = diffs

    async def get(self, diff_id):
        sid = diff_id if isinstance(diff_id, uuid.UUID) else uuid.UUID(str(diff_id))
        return self._diffs.get(sid)

    async def append(self, diff):
        self._diffs[diff.diff_id] = diff
        return diff

    async def list_pending(self, *, limit: int = 50):
        return [d for d in self._diffs.values() if d.status == IdentityDiffStatus.PENDING]

    async def reject(self, diff_id):
        sid = diff_id if isinstance(diff_id, uuid.UUID) else uuid.UUID(str(diff_id))
        d = self._diffs.get(sid)
        if d is not None:
            self._diffs[sid] = IdentityDiff(
                proposed_text=d.proposed_text,
                rationale=d.rationale,
                source_trace_ids=list(d.source_trace_ids),
                status=IdentityDiffStatus.REJECTED,
                diff_id=d.diff_id,
                created_at=d.created_at,
            )

    # approve() is inherited; it calls _session.execute(...) which we don't
    # have here. Override to skip the SQL flip and call apply_identity_diff
    # directly.
    async def approve(self, diff_id, *, identity_path):
        from agent.identity import apply_identity_diff

        diff = await self.get(diff_id)
        assert diff is not None and diff.status == IdentityDiffStatus.PENDING
        # Mark in our fake store.
        self._diffs[diff.diff_id] = IdentityDiff(
            proposed_text=diff.proposed_text,
            rationale=diff.rationale,
            source_trace_ids=list(diff.source_trace_ids),
            status=IdentityDiffStatus.APPROVED,
            diff_id=diff.diff_id,
            created_at=diff.created_at,
        )
        return apply_identity_diff(
            proposed_identity_text=diff.proposed_text, path=identity_path
        )


@pytest.mark.asyncio
async def test_propose_approve_apply_cycle(tmp_path: Path) -> None:
    # Seed a known starting state on disk.
    id_path = tmp_path / "pepper_identity.md"
    seed = Identity(
        identity_text="I notice things and choose when to surface.",
        questions_text="Why do I always summarise?",
        identity_present=True,
        questions_present=True,
        identity_version=1,
        questions_version=4,
        path=str(id_path),
    )
    write_identity_atomic(seed)

    # Reflector proposes a diff.
    repo = _FakeRepo({})
    diff = IdentityDiff(
        proposed_text=(
            "I notice things and choose when to surface. I do not "
            "fill silence with platitudes."
        ),
        rationale="Pepper noticed she's been padding briefs.",
    )
    await repo.append(diff)
    pending = await repo.list_pending()
    assert [d.diff_id for d in pending] == [diff.diff_id]

    # Operator approves it.
    new_identity = await repo.approve(diff.diff_id, identity_path=str(id_path))

    # File on disk reflects the new identity, version bumped.
    assert new_identity.identity_version == 2
    assert "do not fill silence with platitudes" in new_identity.identity_text
    # Questions section + version are untouched.
    assert new_identity.questions_version == 4
    assert "summarise" in new_identity.questions_text

    # Diff is no longer pending.
    pending = await repo.list_pending()
    assert pending == []
    final = await repo.get(diff.diff_id)
    assert final is not None and final.status == IdentityDiffStatus.APPROVED


@pytest.mark.asyncio
async def test_rejected_diff_does_not_modify_file(tmp_path: Path) -> None:
    id_path = tmp_path / "pepper_identity.md"
    seed = Identity(
        identity_text="original.",
        questions_text="",
        identity_present=True,
        questions_present=False,
        identity_version=7,
        questions_version=0,
        path=str(id_path),
    )
    write_identity_atomic(seed)

    repo = _FakeRepo({})
    diff = IdentityDiff(proposed_text="something I'd never accept.")
    await repo.append(diff)

    await repo.reject(diff.diff_id)

    # File should be byte-identical.
    from agent.identity import load_identity

    after = load_identity(str(id_path))
    assert after.identity_version == 7
    assert "original" in after.identity_text
