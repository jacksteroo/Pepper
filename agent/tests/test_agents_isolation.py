"""Lint enforcement for the `agents/` boundary (ADR-0004 + ADR-0006).

Four rules, all enforced by static AST analysis (no module execution):

1. No `agents/<X>/` module imports from `agents/<Y>/` for any other
   archetype Y. (`_shared` is the documented exception — see rule 3.)
2. No module under `agents/` imports from `subsystems/` or from
   `agent.core` (or its submodules).
3. Every file under `agents/_shared/` has a module-level docstring.
4. `agents/_shared/` does not contain module-level mutable state —
   no top-level `name = {}`, `name: T = []`, etc., for mutable
   container types or unguarded `global` writes.

Each rule has a positive case (the real `agents/` tree passes) and a
negative fixture-based case (a deliberately-violating tree under a
tmp dir is detected).
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "agents"


# ── Static-analysis helpers ──────────────────────────────────────────────────


def _display_path(p: Path) -> str:
    """Render a path relative to the repo root when possible, else absolute."""
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def _iter_python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _module_path_for(path: Path, agents_root: Path) -> str:
    """`agents_root/reflector/main.py` → `agents.reflector.main`."""
    rel = path.relative_to(agents_root.parent).with_suffix("")
    return ".".join(rel.parts)


def _imports_from(tree: ast.AST) -> list[str]:
    """Collect every fully-qualified import target as dotted names.

    For `from X import a, b`, we emit both `X` AND `X.a` / `X.b`. The
    second form catches the `from agents import monitor` shape, where
    the imported symbol IS the archetype module — emitting only `X`
    would silently miss it.
    """
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            prefix = "." * node.level
            base = f"{prefix}{mod}"
            out.append(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                if base and not base.endswith("."):
                    out.append(f"{base}.{alias.name}")
                else:
                    out.append(f"{base}{alias.name}")
    # Escape hatches: `importlib.import_module("agents.monitor")` and
    # `__import__("subsystems.calendar")` are also imports for the
    # purpose of the boundary. AST-detect their string-literal first
    # argument and add it to the import set.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target is not None:
                out.append(target)
    return out


def _dynamic_import_target(call: ast.Call) -> str | None:
    """Return the string-literal target of a dynamic-import call, or None.

    Recognises:
      - `importlib.import_module("X")`
      - `import_module("X")`        (when imported as bare name)
      - `__import__("X")`
    """
    func = call.func
    name: str | None = None
    if isinstance(func, ast.Attribute) and func.attr == "import_module":
        name = "import_module"
    elif isinstance(func, ast.Name) and func.id in {"import_module", "__import__"}:
        name = func.id
    if name is None:
        return None
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _archetype_of(module_path: str, agents_root: Path) -> str | None:
    """Return the archetype name for a module under `agents/`, or None.

    `agents.reflector.main` → "reflector"
    `agents._shared.logging` → "_shared"
    `agents.runner` → None (top-level runner, not part of any archetype)
    `agents` → None
    `agent.tests.foo` → None (not under agents/)
    """
    parts = module_path.split(".")
    if not parts or parts[0] != "agents":
        return None
    if len(parts) < 2:
        return None
    return parts[1]


def _resolve_relative(importer_module: str, raw_target: str) -> str:
    """Resolve a possibly-relative import target into an absolute one."""
    if not raw_target.startswith("."):
        return raw_target
    level = 0
    while level < len(raw_target) and raw_target[level] == ".":
        level += 1
    tail = raw_target[level:]
    parts = importer_module.split(".")
    base = parts[: max(0, len(parts) - level)]
    return ".".join([*base, tail] if tail else base)


# ── Rule 1 + 2: import boundaries ────────────────────────────────────────────


def _scan_imports(agents_root: Path) -> list[tuple[Path, str, str]]:
    """Yield (file, importer_module, resolved_target) for every import
    statement in every .py file under `agents_root`.
    """
    findings: list[tuple[Path, str, str]] = []
    for py in _iter_python_files(agents_root):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        importer = _module_path_for(py, agents_root)
        for raw in _imports_from(tree):
            resolved = _resolve_relative(importer, raw)
            findings.append((py, importer, resolved))
    return findings


def _violations_cross_archetype(
    findings: list[tuple[Path, str, str]], agents_root: Path
) -> list[str]:
    bad: list[str] = []
    for py, importer, target in findings:
        if not target.startswith("agents."):
            continue
        importer_arch = _archetype_of(importer, agents_root)
        target_arch = _archetype_of(target, agents_root)
        if importer_arch is None or target_arch is None:
            continue
        if importer_arch == target_arch:
            continue
        # `_shared` is the only carve-out (ADR-0004 §"_shared/ discipline").
        if target_arch == "_shared":
            continue
        bad.append(f"{_display_path(py)}: {importer} imports {target}")
    return bad


def _violations_into_orchestrator_or_subsystems(
    findings: list[tuple[Path, str, str]],
) -> list[str]:
    bad: list[str] = []
    for py, importer, target in findings:
        if target == "agent.core" or target.startswith("agent.core."):
            bad.append(f"{_display_path(py)}: {importer} imports {target}")
        elif target == "subsystems" or target.startswith("subsystems."):
            bad.append(f"{_display_path(py)}: {importer} imports {target}")
    return bad


# ── Rule 3 + 4: `_shared/` discipline ─────────────────────────────────────────


def _missing_docstring_files(shared_root: Path) -> list[str]:
    bad: list[str] = []
    for py in _iter_python_files(shared_root):
        # __init__.py with body counts; bare empty __init__.py would also
        # be a violation under this rule because it carves out a shared
        # surface without explaining why.
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        if ast.get_docstring(tree) is None:
            bad.append(_display_path(py))
    return bad


_MUTABLE_CONTAINER_NAMES = {"dict", "list", "set"}


def _is_mutable_value(node: ast.AST) -> bool:
    """Heuristic: detect the common module-level state shapes."""
    # Literals that hold mutable state.
    if isinstance(node, (ast.Dict, ast.List, ast.Set)):
        return True
    # `dict()`, `list()`, `set()`, `defaultdict(...)`, etc.
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _MUTABLE_CONTAINER_NAMES:
            return True
        if isinstance(func, ast.Name) and func.id in {"defaultdict", "OrderedDict", "Counter"}:
            return True
        if isinstance(func, ast.Attribute) and func.attr in {
            "defaultdict",
            "OrderedDict",
            "Counter",
        }:
            return True
    return False


_DOCSTRING_BODY_NODES = (ast.Expr,)  # the docstring itself


def _module_level_state_violations(shared_root: Path) -> list[str]:
    bad: list[str] = []
    for py in _iter_python_files(shared_root):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in tree.body:
            # `name = {...}` / `name = []` / `name = dict()` etc.
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and _is_mutable_value(node.value):
                        bad.append(
                            f"{_display_path(py)}:{node.lineno}: "
                            f"module-level mutable assignment to {tgt.id!r}"
                        )
            # `name: dict = {}`
            elif isinstance(node, ast.AnnAssign):
                if (
                    node.value is not None
                    and isinstance(node.target, ast.Name)
                    and _is_mutable_value(node.value)
                ):
                    bad.append(
                        f"{_display_path(py)}:{node.lineno}: "
                        f"module-level mutable annotated assignment to {node.target.id!r}"
                    )
            # `global X` at module level is malformed Python; skip.
    return bad


# ── Real-tree tests ──────────────────────────────────────────────────────────


@pytest.mark.skipif(not AGENTS_DIR.exists(), reason="agents/ tree not present")
class TestRealAgentsTree:
    def test_no_cross_archetype_imports(self) -> None:
        findings = _scan_imports(AGENTS_DIR)
        bad = _violations_cross_archetype(findings, AGENTS_DIR)
        assert bad == [], "cross-archetype imports detected:\n" + "\n".join(bad)

    def test_no_imports_into_orchestrator_or_subsystems(self) -> None:
        findings = _scan_imports(AGENTS_DIR)
        bad = _violations_into_orchestrator_or_subsystems(findings)
        assert bad == [], (
            "agents/ modules importing agent.core or subsystems/:\n" + "\n".join(bad)
        )

    def test_shared_files_have_module_docstring(self) -> None:
        bad = _missing_docstring_files(AGENTS_DIR / "_shared")
        assert bad == [], "agents/_shared/ files missing module docstring:\n" + "\n".join(bad)

    def test_shared_has_no_module_level_mutable_state(self) -> None:
        bad = _module_level_state_violations(AGENTS_DIR / "_shared")
        assert bad == [], "agents/_shared/ module-level state:\n" + "\n".join(bad)


# ── Fixture tests: deliberate violations are caught ──────────────────────────


def _write(tree_root: Path, rel: str, content: str) -> Path:
    p = tree_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return p


class TestFixtureViolationsAreCaught:
    """Each fixture builds a minimal `agents/` tree under a tmp dir and
    asserts the relevant rule fires. This guards against future
    refactors that silently weaken a rule (the real-tree tests would
    keep passing if the rule were inverted)."""

    def test_cross_archetype_import_is_flagged(self, tmp_path: Path) -> None:
        agents = tmp_path / "agents"
        _write(agents, "__init__.py", '"""docs"""\n')
        _write(agents, "reflector/__init__.py", '"""docs"""\n')
        _write(
            agents,
            "reflector/main.py",
            '"""reflector main"""\nfrom agents.monitor import helper  # noqa\n',
        )
        _write(agents, "monitor/__init__.py", '"""docs"""\n')
        _write(agents, "monitor/helper.py", '"""monitor helper"""\n')

        findings = _scan_imports(agents)
        bad = _violations_cross_archetype(findings, agents)
        assert any("reflector/main.py" in b and "agents.monitor" in b for b in bad), bad

    def test_shared_imports_from_archetype_are_allowed(self, tmp_path: Path) -> None:
        agents = tmp_path / "agents"
        _write(agents, "__init__.py", '"""docs"""\n')
        _write(agents, "_shared/__init__.py", '"""shared"""\n')
        _write(agents, "_shared/logging.py", '"""shared logging"""\n')
        _write(agents, "reflector/__init__.py", '"""docs"""\n')
        _write(
            agents,
            "reflector/main.py",
            '"""reflector main"""\nfrom agents._shared import logging  # noqa\n',
        )

        findings = _scan_imports(agents)
        bad = _violations_cross_archetype(findings, agents)
        assert bad == []

    def test_import_into_subsystems_is_flagged(self, tmp_path: Path) -> None:
        agents = tmp_path / "agents"
        _write(agents, "__init__.py", '"""docs"""\n')
        _write(agents, "reflector/__init__.py", '"""docs"""\n')
        _write(
            agents,
            "reflector/main.py",
            '"""reflector main"""\nfrom subsystems.calendar import x  # noqa\n',
        )

        findings = _scan_imports(agents)
        bad = _violations_into_orchestrator_or_subsystems(findings)
        assert any("subsystems.calendar" in b for b in bad), bad

    def test_import_into_agent_core_is_flagged(self, tmp_path: Path) -> None:
        agents = tmp_path / "agents"
        _write(agents, "__init__.py", '"""docs"""\n')
        _write(agents, "reflector/__init__.py", '"""docs"""\n')
        _write(
            agents,
            "reflector/main.py",
            '"""reflector main"""\nfrom agent.core import orchestrator  # noqa\n',
        )

        findings = _scan_imports(agents)
        bad = _violations_into_orchestrator_or_subsystems(findings)
        assert any("agent.core" in b for b in bad), bad

    def test_missing_docstring_in_shared_is_flagged(self, tmp_path: Path) -> None:
        shared = tmp_path / "agents" / "_shared"
        # Deliberately no docstring.
        _write(shared, "no_doc.py", "VALUE = 1\n")
        bad = _missing_docstring_files(shared)
        assert any(b.endswith("no_doc.py") for b in bad), bad

    def test_module_level_dict_in_shared_is_flagged(self, tmp_path: Path) -> None:
        shared = tmp_path / "agents" / "_shared"
        _write(
            shared,
            "stateful.py",
            '"""shared utility"""\n_cache: dict = {}\n',
        )
        bad = _module_level_state_violations(shared)
        assert bad, bad

    def test_module_level_list_in_shared_is_flagged(self, tmp_path: Path) -> None:
        shared = tmp_path / "agents" / "_shared"
        _write(
            shared,
            "stateful.py",
            '"""shared utility"""\nbuffer = []\n',
        )
        bad = _module_level_state_violations(shared)
        assert bad, bad

    def test_from_agents_import_archetype_is_flagged(self, tmp_path: Path) -> None:
        # `from agents import monitor` — the imported symbol IS the
        # archetype module, easily missed if only `node.module` is read.
        agents = tmp_path / "agents"
        _write(agents, "__init__.py", '"""docs"""\n')
        _write(agents, "reflector/__init__.py", '"""docs"""\n')
        _write(
            agents,
            "reflector/main.py",
            '"""reflector main"""\nfrom agents import monitor  # noqa\n',
        )
        _write(agents, "monitor/__init__.py", '"""docs"""\n')

        findings = _scan_imports(agents)
        bad = _violations_cross_archetype(findings, agents)
        assert any("agents.monitor" in b for b in bad), bad

    def test_importlib_escape_hatch_into_subsystems_is_flagged(
        self, tmp_path: Path
    ) -> None:
        agents = tmp_path / "agents"
        _write(agents, "__init__.py", '"""docs"""\n')
        _write(agents, "reflector/__init__.py", '"""docs"""\n')
        _write(
            agents,
            "reflector/main.py",
            textwrap.dedent(
                '''
                """reflector main"""
                import importlib
                m = importlib.import_module("subsystems.calendar")
                '''
            ),
        )

        findings = _scan_imports(agents)
        bad = _violations_into_orchestrator_or_subsystems(findings)
        assert any("subsystems.calendar" in b for b in bad), bad

    def test_dunder_import_escape_hatch_into_agent_core_is_flagged(
        self, tmp_path: Path
    ) -> None:
        agents = tmp_path / "agents"
        _write(agents, "__init__.py", '"""docs"""\n')
        _write(agents, "reflector/__init__.py", '"""docs"""\n')
        _write(
            agents,
            "reflector/main.py",
            textwrap.dedent(
                '''
                """reflector main"""
                m = __import__("agent.core")
                '''
            ),
        )

        findings = _scan_imports(agents)
        bad = _violations_into_orchestrator_or_subsystems(findings)
        assert any("agent.core" in b for b in bad), bad

    def test_module_level_constants_in_shared_are_allowed(self, tmp_path: Path) -> None:
        shared = tmp_path / "agents" / "_shared"
        _write(
            shared,
            "constants.py",
            '"""shared constants"""\nDEFAULT = "x"\nPORT: int = 5432\n',
        )
        bad = _module_level_state_violations(shared)
        assert bad == []
