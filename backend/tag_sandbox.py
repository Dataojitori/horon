"""
Tag Plugin Sandbox — AST validation, safe execution, and Proxy classes.

Plugins are loaded from backend/tag_plugins/<tag_name>.py.
Each must define on_mutation(ctx) and audit_cluster(ctx).

Security model: prevents accidental misuse by a trusted author.
NOT hardened against deliberate CPython-level escapes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Callable


_PLUGINS_DIR = Path(__file__).parent / "tag_plugins"

_ALLOWED_IMPORTS: dict[str, Any] = {
    "re": re,
}


def _safe_import(name: str, globals: dict[str, Any] | None = None, locals: dict[str, Any] | None = None, fromlist: tuple[str, ...] = (), level: int = 0) -> Any:
    base_name = name.split('.')[0] if name else ""
    if base_name in _ALLOWED_IMPORTS:
        return _ALLOWED_IMPORTS[base_name]
    raise ImportError(f"import of '{name}' is forbidden in sandbox")


_SAFE_BUILTINS = {
    "len": len, "isinstance": isinstance, "str": str, "int": int,
    "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "range": range, "enumerate": enumerate, "zip": zip,
    "any": any, "all": all, "min": min, "max": max, "sorted": sorted,
    "sum": sum, "abs": abs, "round": round, "float": float,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "AttributeError": AttributeError,
    "RuntimeError": RuntimeError, "ZeroDivisionError": ZeroDivisionError,
    "StopIteration": StopIteration,
    "__import__": _safe_import,
    "__build_class__": __builtins__["__build_class__"]
    if isinstance(__builtins__, dict)
    else getattr(__builtins__, "__build_class__"),
}

_BANNED_CALLS = frozenset({
    "eval", "exec", "open", "compile",
    "globals", "locals", "getattr", "setattr", "delattr",
})


class TagPluginError(Exception):
    """Plugin file is invalid (loading/validation phase)."""
    pass


class HookRejection(Exception):
    """Raised by ctx.reject() inside on_mutation. Caught by db.py."""
    pass


# ── AST Validation ───────────────────────────────────────────────────────────

class _ASTValidator(ast.NodeVisitor):
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.errors: list[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            base_name = alias.name.split('.')[0]
            if base_name not in _ALLOWED_IMPORTS:
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: "
                    f"import of '{alias.name}' is forbidden")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        base_name = mod.split('.')[0]
        if base_name not in _ALLOWED_IMPORTS:
            self.errors.append(
                f"{self.filepath}:{node.lineno}: "
                f"import from '{mod}' is forbidden")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr.startswith("__"):
            self.errors.append(
                f"{self.filepath}:{node.lineno}: "
                f"access to dunder attribute '{node.attr}' is forbidden")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name and name in _BANNED_CALLS:
            self.errors.append(
                f"{self.filepath}:{node.lineno}: "
                f"call to '{name}' is forbidden")
        self.generic_visit(node)


def _validate_ast(source: str, filepath: str) -> None:
    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError as e:
        raise TagPluginError(
            f"{filepath}:{e.lineno}: syntax error: {e.msg}")
    validator = _ASTValidator(filepath)
    validator.visit(tree)
    if validator.errors:
        raise TagPluginError("\n".join(validator.errors))


# ── Plugin Loading ───────────────────────────────────────────────────────────

def load_plugin(tag_name: str) -> dict | None:
    """Load a plugin for the given tag. Returns None if no file exists.

    Returns a dict with keys 'on_mutation' and 'audit_cluster' (callables).
    Raises TagPluginError on validation/loading failure.
    """
    filepath = _PLUGINS_DIR / f"{tag_name}.py"
    if not filepath.exists():
        return None

    source = filepath.read_text(encoding="utf-8")
    filepath_str = str(filepath)
    _validate_ast(source, filepath_str)

    namespace: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "__name__": f"tag_plugins.{tag_name}",
    }
    try:
        exec(compile(source, filepath_str, "exec"), namespace)  # noqa: S102
    except Exception as e:
        raise TagPluginError(
            f"{filepath_str}: execution failed: {e}")

    for fn_name in ("on_mutation", "audit_cluster"):
        if fn_name not in namespace or not callable(namespace[fn_name]):
            raise TagPluginError(
                f"{filepath_str}: missing required function '{fn_name}'")

    # Required human description shown in `list_tags`. ONE place, natural
    # language, covering BOTH hooks (what on_mutation does at write time +
    # what an audit reports). Not split per-hook: a reader always needs the
    # whole effect before relying on the tag, so half of it is never useful.
    # Convention: module-level `DESCRIPTION = "..."` (multi-line allowed).
    description = namespace.get("DESCRIPTION")
    if not isinstance(description, str) or not description.strip():
        raise TagPluginError(
            f"{filepath_str}: missing required module-level DESCRIPTION "
            "(non-empty string covering both on_mutation and audit_cluster)")

    return {
        "on_mutation": namespace["on_mutation"],
        "audit_cluster": namespace["audit_cluster"],
        "description": description,
    }


# ── Proxy Classes ────────────────────────────────────────────────────────────

class ConceptProxy:
    """Lazy, read-only proxy for a concept. No sqlite3.Connection stored."""

    def __init__(
        self,
        concept_id: int,
        *,
        fetch_name: Callable[[], str],
        fetch_content: Callable[[], str | None] | None = None,
        fetch_role: Callable[[], str] | None = None,
        fetch_is_active: Callable[[], int] | None = None,
        fetch_lifespan: Callable[[], str | None] | None = None,
        fetch_activation_type: Callable[[], str | None] | None = None,
        fetch_on_fire: Callable[[], str | None] | None = None,
        fetch_disclosure: Callable[[], str | None] | None = None,
        fetch_tags: Callable[[], list[str]],
        fetch_inputs: Callable[[], list[ConceptProxy]] | None = None,
        fetch_downstream: Callable[[], list[ConceptProxy]] | None = None,
    ):
        self.concept_id = concept_id
        self._fetch_name = fetch_name
        self._fetch_content = fetch_content
        self._fetch_role = fetch_role
        self._fetch_is_active = fetch_is_active
        self._fetch_lifespan = fetch_lifespan
        self._fetch_activation_type = fetch_activation_type
        self._fetch_on_fire = fetch_on_fire
        self._fetch_disclosure = fetch_disclosure
        self._fetch_tags = fetch_tags
        self._fetch_inputs = fetch_inputs
        self._fetch_downstream = fetch_downstream
        self._cache: dict[str, Any] = {}

    @property
    def name(self) -> str:
        if "name" not in self._cache:
            self._cache["name"] = self._fetch_name()
        return self._cache["name"]

    @property
    def content(self) -> str | None:
        if "content" not in self._cache:
            self._cache["content"] = self._fetch_content() if self._fetch_content else None
        return self._cache["content"]

    @property
    def role(self) -> str:
        if "role" not in self._cache:
            self._cache["role"] = self._fetch_role() if self._fetch_role else "plain"
        return self._cache["role"]

    @property
    def is_active(self) -> int:
        if "is_active" not in self._cache:
            self._cache["is_active"] = self._fetch_is_active() if self._fetch_is_active else 0
        return self._cache["is_active"]

    @property
    def lifespan(self) -> str | None:
        if "lifespan" not in self._cache:
            self._cache["lifespan"] = self._fetch_lifespan() if self._fetch_lifespan else None
        return self._cache["lifespan"]

    @property
    def activation_type(self) -> str | None:
        if "activation_type" not in self._cache:
            self._cache["activation_type"] = self._fetch_activation_type() if self._fetch_activation_type else None
        return self._cache["activation_type"]

    @property
    def on_fire(self) -> str | None:
        if "on_fire" not in self._cache:
            self._cache["on_fire"] = self._fetch_on_fire() if self._fetch_on_fire else None
        return self._cache["on_fire"]

    @property
    def disclosure(self) -> str | None:
        if "disclosure" not in self._cache:
            self._cache["disclosure"] = self._fetch_disclosure() if self._fetch_disclosure else None
        return self._cache["disclosure"]

    @property
    def tags(self) -> list[str]:
        if "tags" not in self._cache:
            self._cache["tags"] = self._fetch_tags()
        return self._cache["tags"]

    @property
    def inputs(self) -> list[ConceptProxy]:
        """上游输入引脚（激活规则依赖的前置概念列表）。"""
        if "inputs" not in self._cache:
            self._cache["inputs"] = self._fetch_inputs() if self._fetch_inputs else []
        return self._cache["inputs"]

    @property
    def downstream(self) -> list[ConceptProxy]:
        """下游汇聚节点（将本概念作为输入前置引用的下游逻辑/守卫列表）。"""
        if "downstream" not in self._cache:
            self._cache["downstream"] = self._fetch_downstream() if self._fetch_downstream else []
        return self._cache["downstream"]


class ClusterProxy:
    """Lazy proxy for all concepts carrying a given tag."""

    def __init__(
        self,
        *,
        fetch_concepts: Callable[[], list[ConceptProxy]],
        fetch_count: Callable[[], int],
    ):
        self._fetch_concepts = fetch_concepts
        self._fetch_count = fetch_count
        self._cache: dict[str, Any] = {}

    @property
    def concepts(self) -> list[ConceptProxy]:
        if "concepts" not in self._cache:
            self._cache["concepts"] = self._fetch_concepts()
        return self._cache["concepts"]

    @property
    def count(self) -> int:
        if "count" not in self._cache:
            self._cache["count"] = self._fetch_count()
        return self._cache["count"]


# ── Context Classes ──────────────────────────────────────────────────────────

class MutationContext:
    """Context passed to on_mutation(ctx)."""

    def __init__(
        self,
        tag_name: str,
        this_proxy: ConceptProxy,
        changed: dict | None,
        get_concept: Callable[[int], ConceptProxy] | None = None,
    ):
        self.tag_name = tag_name
        self.this = this_proxy
        self.changed = changed if changed is not None else {}
        self._get_concept = get_concept
        self._infos: list[str] = []

    def get_concept(self, concept_id: int) -> ConceptProxy:
        if self._get_concept is None:
            raise RuntimeError("get_concept is not available.")
        return self._get_concept(concept_id)

    def reject(self, msg: str) -> None:
        raise HookRejection(msg)

    def info(self, msg: str) -> None:
        self._infos.append(msg)

    def warn(self, msg: str) -> None:
        raise RuntimeError(
            "warn() is not available in on_mutation context. "
            "Use reject() to block or info() to advise."
        )


class AuditContext:
    """Context passed to audit_cluster(ctx)."""

    def __init__(
        self,
        tag_name: str,
        cluster_proxy: ClusterProxy,
        get_concept: Callable[[int], ConceptProxy] | None = None,
    ):
        self.tag_name = tag_name
        self.cluster = cluster_proxy
        self._get_concept = get_concept
        self._warnings: list[str] = []

    def get_concept(self, concept_id: int) -> ConceptProxy:
        if self._get_concept is None:
            raise RuntimeError("get_concept is not available.")
        return self._get_concept(concept_id)

    def warn(self, msg: str) -> None:
        self._warnings.append(msg)

    def reject(self, msg: str) -> None:
        raise RuntimeError(
            "reject() is not available in audit_cluster context. "
            "Use warn() to report issues."
        )

    def info(self, msg: str) -> None:
        raise RuntimeError(
            "info() is not available in audit_cluster context. "
            "Use warn() to report issues."
        )
