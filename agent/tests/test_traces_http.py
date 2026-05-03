"""Tests for the /traces FastAPI route (#24).

Covers:

- Localhost-bind enforcement (rejects non-loopback when the setting is on,
  allows it when off).
- API-key dependency wires up correctly.
- list view returns projected summaries; detail view returns the full payload.
- find_similar validates embedding dimension at the request layer (Pydantic
  Field constraints).
- The router does NOT register a DELETE on traces (append-only at the
  HTTP layer, mirrors ADR-0005).

The DB session is mocked via FastAPI dependency overrides so these tests
do not require a live Postgres.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.auth import require_api_key
from agent.db import get_db
from agent.error_classifier import DataSensitivity
from agent.traces import Archetype, Trace, TraceTier, TriggerSource
from agent.traces.http import router


def _build_app(*, repo_query=None, repo_get=None, assembler=None) -> FastAPI:
    """Build a FastAPI app with the traces router and dependency overrides."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _fake_session():
        yield "fake-session"  # never used because we patch the repository

    async def _fake_auth():
        return "test-api-key"

    app.dependency_overrides[get_db] = _fake_session
    app.dependency_overrides[require_api_key] = _fake_auth

    # #34 — the rerender route depends on the live ContextAssembler. Tests
    # plug in a stub by overriding ``get_assembler``.
    if assembler is not None:
        from agent.traces.http import get_assembler

        def _fake_assembler() -> object:
            return assembler

        app.dependency_overrides[get_assembler] = _fake_assembler
    return app


def _trace(**overrides) -> Trace:
    base = dict(
        trace_id=str(uuid.uuid4()),
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        trigger_source=TriggerSource.USER,
        archetype=Archetype.ORCHESTRATOR,
        input="hello",
        output="world",
        model_selected="hermes3-local",
        latency_ms=42,
        data_sensitivity=DataSensitivity.LOCAL_ONLY,
        tier=TraceTier.WORKING,
    )
    base.update(overrides)
    return Trace(**base)


# ── Localhost-bind enforcement ────────────────────────────────────────────────


class TestLocalhostBind:
    def test_loopback_request_passes_through(self) -> None:
        # Patch the loopback check directly because TestClient's
        # client.host is "testclient", not 127.0.0.1.
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg, \
             patch("agent.traces.http._client_is_loopback", return_value=True):
            cfg.PEPPER_BIND_LOCALHOST_ONLY = True
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=[_trace()])
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
            assert r.status_code == 200, r.text

    def test_non_loopback_request_with_bind_on_returns_403(self) -> None:
        app = _build_app()
        with patch("agent.config.settings") as cfg, \
             patch("agent.traces.http._client_is_loopback", return_value=False):
            cfg.PEPPER_BIND_LOCALHOST_ONLY = True
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
            assert r.status_code == 403

    def test_disabled_bind_lets_non_loopback_in(self) -> None:
        # When the operator opts out, the route stops checking client.host.
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=[])
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
            assert r.status_code == 200


# ── List view ────────────────────────────────────────────────────────────────


class TestListView:
    def test_returns_projected_summaries(self) -> None:
        app = _build_app()
        traces = [
            _trace(input="user question", output="assistant reply"),
            _trace(
                trigger_source=TriggerSource.SCHEDULER,
                scheduler_job_name="morning_brief",
                input="brief",
                output="brief output",
            ),
        ]
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=traces)
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["traces"]) == 2
        # Summaries do NOT include input/output fields.
        for t in body["traces"]:
            assert "input" not in t
            assert "output" not in t
            assert "assembled_context" not in t
        assert body["traces"][1]["scheduler_job_name"] == "morning_brief"

    def test_filters_propagate_to_repository(self) -> None:
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=[])
            with TestClient(app) as client:
                r = client.get(
                    "/api/traces"
                    "?archetype=orchestrator&trigger_source=scheduler"
                    "&data_sensitivity=local_only&tier=working"
                    "&contains_text=foo&limit=25",
                    headers={"x-api-key": "k"},
                )
            assert r.status_code == 200
            kwargs = repo.query.await_args.kwargs
            assert kwargs["archetype"] is Archetype.ORCHESTRATOR
            assert kwargs["trigger_source"] is TriggerSource.SCHEDULER
            assert kwargs["data_sensitivity"] is DataSensitivity.LOCAL_ONLY
            assert kwargs["tier"] is TraceTier.WORKING
            assert kwargs["contains_text"] == "foo"
            assert kwargs["limit"] == 25
            assert kwargs["with_payload"] is False


# ── Detail view ──────────────────────────────────────────────────────────────


class TestDetailView:
    def test_returns_full_payload(self) -> None:
        app = _build_app()
        t = _trace(input="hello", output="world")
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=t)
            with TestClient(app) as client:
                r = client.get(f"/api/traces/{t.trace_id}", headers={"x-api-key": "k"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["input"] == "hello"
        assert body["output"] == "world"
        assert body["assembled_context"] == {}
        assert body["has_embedding"] is False
        # #34 — detail response carries decision_reasons; empty {} when no
        # provenance was stored (legacy rows or empty assembled_context).
        assert body["decision_reasons"] == {}

    def test_decision_reasons_populated_from_stored_provenance(self) -> None:
        # #34 — when provenance was persisted by #33, the detail response
        # should annotate each named selector with a human-readable reason.
        app = _build_app()
        t = _trace(
            input="hello",
            output="world",
            assembled_context={
                "life_context_sections_used": ["work", "health"],
                "last_n_turns": 3,
                "memory_ids": [],
                "skill_match": None,
                "capability_block_version": "v1",
                "selectors": {
                    "life_context": {
                        "life_context_sections_used": ["work", "health"],
                    },
                    "capability_block": {
                        "available_sources": ["calendar", "email"],
                        "capability_block_version": "v1",
                    },
                    "retrieved_memory": {"memory_ids": [], "present": False},
                    "skill_match": {"included": True, "n_skills": 5},
                    "last_n_turns": {
                        "isolated": False,
                        "last_n_turns": 3,
                        "n_messages": 6,
                        "limit": 20,
                    },
                },
            },
        )
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=t)
            with TestClient(app) as client:
                r = client.get(
                    f"/api/traces/{t.trace_id}", headers={"x-api-key": "k"}
                )
        assert r.status_code == 200, r.text
        reasons = r.json()["decision_reasons"]
        assert set(reasons.keys()) == {
            "life_context",
            "capability_block",
            "retrieved_memory",
            "skill_match",
            "last_n_turns",
        }
        # Sanity-check the human strings are non-empty and informative.
        for v in reasons.values():
            assert isinstance(v, str) and len(v) > 0

    def test_404_when_missing(self) -> None:
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=None)
            with TestClient(app) as client:
                r = client.get(
                    f"/api/traces/{uuid.uuid4()}", headers={"x-api-key": "k"}
                )
        assert r.status_code == 404


# ── Append-only at the HTTP layer ────────────────────────────────────────────


class TestNoMutationRoutes:
    def test_router_has_no_delete_routes(self) -> None:
        # Mirrors ADR-0005's append-only invariant at the HTTP API layer.
        for route in router.routes:
            methods = getattr(route, "methods", set()) or set()
            assert "DELETE" not in methods, (
                f"unexpected DELETE on {route.path}: {methods}"
            )

    def test_router_only_exposes_documented_paths(self) -> None:
        paths = sorted({getattr(r, "path", "") for r in router.routes})
        # Empty string entries come from internal routes; filter.
        public = [p for p in paths if p.startswith("/")]
        assert public == [
            "/traces",
            "/traces/{trace_id}",
            "/traces/{trace_id}/find_similar",
            "/traces/{trace_id}/rerender-prompt",
        ]


# ── find_similar validation ──────────────────────────────────────────────────


class TestFindSimilar:
    def test_rejects_wrong_embedding_dimension(self) -> None:
        app = _build_app()
        with patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{uuid.uuid4()}/find_similar",
                    headers={"x-api-key": "k"},
                    json={"embedding": [0.0] * 16, "limit": 5},
                )
        # Pydantic constraint at request time → 422.
        assert r.status_code == 422

    def test_returns_id_only_matches(self) -> None:
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.find_similar = AsyncMock(
                return_value=[("11111111-1111-1111-1111-111111111111", 0.12)],
            )
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{uuid.uuid4()}/find_similar",
                    headers={"x-api-key": "k"},
                    json={"embedding": [0.0] * 1024, "limit": 5},
                )
        assert r.status_code == 200
        body = r.json()
        assert body["matches"][0]["trace_id"] == "11111111-1111-1111-1111-111111111111"
        assert body["matches"][0]["distance"] == pytest.approx(0.12)


# ── Rerender prompt (#34) ─────────────────────────────────────────────────────


class _StubAssembled:
    """Minimal stand-in for AssembledContext used by rerender tests."""

    def __init__(self, system_prompt: str, provenance: dict) -> None:
        self._system_prompt = system_prompt
        self._provenance = provenance

    def render_prompt(self) -> str:
        return self._system_prompt

    @property
    def provenance(self) -> dict:
        return self._provenance


class _StubAssembler:
    """Returns a deterministic AssembledContext per assemble() call."""

    def __init__(self, system_prompt: str, provenance: dict) -> None:
        self._system_prompt = system_prompt
        self._provenance = provenance
        self.calls: list = []

    def assemble(self, turn) -> _StubAssembled:
        self.calls.append(turn)
        return _StubAssembled(self._system_prompt, self._provenance)


class TestRerenderPrompt:
    def test_requires_auth_dependency_wired(self) -> None:
        # Smoke-test: missing the assembler dep yields a 503 rather than
        # a 500. Confirms the dependency is wired through the route.
        app = FastAPI()
        app.include_router(router, prefix="/api")
        async def _fake_session():
            yield "fake"
        async def _fake_auth():
            return "k"
        app.dependency_overrides[get_db] = _fake_session
        app.dependency_overrides[require_api_key] = _fake_auth
        with patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            # No pepper module global → get_assembler should 503.
            with patch("agent.main._get_pepper", return_value=None):
                with TestClient(app) as client:
                    r = client.post(
                        f"/api/traces/{uuid.uuid4()}/rerender-prompt",
                        headers={"x-api-key": "k"},
                    )
            assert r.status_code == 503

    def test_returns_prompt_and_structural_match_for_unchanged_provenance(
        self,
    ) -> None:
        # When the assembler reproduces the same provenance shape that's
        # already on the trace, ``matches_original`` should be True. This
        # is the deterministic check the issue asks for.
        provenance = {
            "life_context_sections_used": ["work"],
            "last_n_turns": 0,  # volatile - excluded from match
            "memory_ids": [],
            "skill_match": None,
            "capability_block_version": "v1",
            "selectors": {
                "life_context": {"life_context_sections_used": ["work"]},
                "capability_block": {
                    "available_sources": ["calendar"],
                    "capability_block_version": "v1",
                },
                "retrieved_memory": {"memory_ids": [], "present": False},
                "skill_match": {"included": True, "n_skills": 1},
                "last_n_turns": {
                    "isolated": False,
                    "last_n_turns": 5,  # volatile drift
                    "n_messages": 10,
                    "limit": 20,
                },
            },
        }
        # Trace stored these earlier with last_n_turns=0 / n_messages=0.
        stored = dict(provenance)
        stored["last_n_turns"] = 0
        stored["selectors"] = dict(provenance["selectors"])
        stored["selectors"]["last_n_turns"] = {
            "isolated": False,
            "last_n_turns": 0,
            "n_messages": 0,
            "limit": 20,
        }
        t = _trace(input="hi", output="ok", assembled_context=stored)
        stub = _StubAssembler(system_prompt="SYSTEM_PROMPT_TEXT", provenance=provenance)
        app = _build_app(assembler=stub)
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=t)
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{t.trace_id}/rerender-prompt",
                    headers={"x-api-key": "k"},
                )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["trace_id"] == t.trace_id
        assert body["prompt"] == "SYSTEM_PROMPT_TEXT"
        assert isinstance(body["prompt_hash"], str)
        assert len(body["prompt_hash"]) == 64  # sha256 hex
        assert body["matches_original"] is True
        assert isinstance(body["notes"], list)
        assert len(body["notes"]) >= 1
        # Stub assembler was called exactly once with a Turn carrying the
        # trace input.
        assert len(stub.calls) == 1
        assert stub.calls[0].user_message == "hi"

    def test_returns_diff_when_provenance_diverges(self) -> None:
        new_prov = {
            "life_context_sections_used": ["work", "health"],  # added
            "last_n_turns": 0,
            "memory_ids": [],
            "skill_match": None,
            "capability_block_version": "v2",  # bumped
            "selectors": {
                "life_context": {
                    "life_context_sections_used": ["work", "health"],
                },
                "capability_block": {"capability_block_version": "v2"},
            },
        }
        stored_prov = {
            "life_context_sections_used": ["work"],
            "last_n_turns": 0,
            "memory_ids": [],
            "skill_match": None,
            "capability_block_version": "v1",
            "selectors": {
                "life_context": {"life_context_sections_used": ["work"]},
                "capability_block": {"capability_block_version": "v1"},
            },
        }
        t = _trace(input="hi", output="ok", assembled_context=stored_prov)
        stub = _StubAssembler(system_prompt="NEW", provenance=new_prov)
        app = _build_app(assembler=stub)
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=t)
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{t.trace_id}/rerender-prompt",
                    headers={"x-api-key": "k"},
                )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["matches_original"] is False
        # Both shapes round-trip in the response so the UI can diff client-side.
        assert body["original_provenance"]["capability_block_version"] == "v1"
        assert body["provenance"]["capability_block_version"] == "v2"

    def test_404_when_trace_missing(self) -> None:
        app = _build_app(assembler=_StubAssembler("X", {}))
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=None)
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{uuid.uuid4()}/rerender-prompt",
                    headers={"x-api-key": "k"},
                )
        assert r.status_code == 404

    def test_localhost_bind_enforced(self) -> None:
        # The new endpoint inherits the same localhost-bind enforcement as
        # the rest of /traces — non-loopback requests with bind on get 403.
        app = _build_app(assembler=_StubAssembler("X", {}))
        with patch("agent.config.settings") as cfg, \
             patch("agent.traces.http._client_is_loopback", return_value=False):
            cfg.PEPPER_BIND_LOCALHOST_ONLY = True
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{uuid.uuid4()}/rerender-prompt",
                    headers={"x-api-key": "k"},
                )
        assert r.status_code == 403
