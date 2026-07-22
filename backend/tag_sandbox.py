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
    "__build_class__": __builtins__["__build_class__"]
    if isinstance(__builtins__, dict)
    else getattr(__builtins__, "__build_class__"),
}

_BANNED_CALLS = frozenset({
    "eval", "exec", "open", "__import__", "compile",
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
        self.errors.append(
            f"{self.filepath}:{node.lineno}: "
            f"import statements are forbidden")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        self.errors.append(
            f"{self.filepath}:{node.lineno}: "
            f"import statements are forbidden")
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
        # Injected: plugins can't `import re` (AST-banned), so hand it in.
        # Safe: re has no IO/reflection; the only escape path (re.__class__…)
        # is already killed by the dunder-access AST ban.
        # Wart: re.compile() collides with the _BANNED_CALLS name check —
        # use re.search/match/sub/findall directly, not compile-then-use.
        # ReDoS is possible but self-inflicted (no external input reaches here);
        # if bounded matching is ever needed, upgrade to a ctx.match() host
        # helper with a timeout (hooks run inside DB transactions).
        "re": re,
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

    # Optional human description shown in `list_tags`. ONE place, natural
    # language, covering BOTH hooks (what on_mutation does at write time +
    # what an audit reports). Not split per-hook: a reader always needs the
    # whole effect before relying on the tag, so half of it is never useful.
    # Convention: module-level `DESCRIPTION = "..."` (multi-line allowed).
    description = namespace.get("DESCRIPTION")
    if description is not None and not isinstance(description, str):
        raise TagPluginError(
            f"{filepath_str}: DESCRIPTION must be a string if defined")

    return {
        "on_mutation": namespace["on_mutation"],
        "audit_cluster": namespace["audit_cluster"],
        "description": description,
    }


# ── Proxy Classes ────────────────────────────────────────────────────────────

class ConceptProxy:
    """Lazy, read-only proxy for a concept. No sqlite3.Connection stored."""

    def __init__(self, concept_id: int, *,
                 fetch_name: Callable[[], str],
                 fetch_disclosure: Callable[[], str | None],
                 fetch_tags: Callable[[], list[str]],
                 fetch_variations: Callable[[], list],
                 fetch_used_in_variations: Callable[[], list]):
        self.concept_id = concept_id
        self._fetch_name = fetch_name
        self._fetch_disclosure = fetch_disclosure
        self._fetch_tags = fetch_tags
        self._fetch_variations = fetch_variations
        self._fetch_used_in_variations = fetch_used_in_variations
        self._cache: dict[str, Any] = {}

    @property
    def name(self) -> str:
        if "name" not in self._cache:
            self._cache["name"] = self._fetch_name()
        return self._cache["name"]

    @property
    def disclosure(self) -> str | None:
        if "disclosure" not in self._cache:
            self._cache["disclosure"] = self._fetch_disclosure()
        return self._cache["disclosure"]

    @property
    def tags(self) -> list[str]:
        if "tags" not in self._cache:
            self._cache["tags"] = self._fetch_tags()
        return self._cache["tags"]

    @property
    def variations(self) -> list[VariationProxy]:
        if "variations" not in self._cache:
            self._cache["variations"] = self._fetch_variations()
        return self._cache["variations"]

    @property
    def used_in_variations(self) -> list[VariationProxy]:
        if "used_in_variations" not in self._cache:
            self._cache["used_in_variations"] = self._fetch_used_in_variations()
        return self._cache["used_in_variations"]


class VariationProxy:
    """Lazy, read-only proxy for a variation. No sqlite3.Connection stored."""

    def __init__(self, concept_id: int, short_code: str, *,
                 fetch_concept_name: Callable[[], str],
                 fetch_type: Callable[[], str | None],
                 fetch_status: Callable[[], str | None],
                 fetch_content: Callable[[], str | None],
                 fetch_valence: Callable[[], float | None],
                 fetch_members: Callable[[], list]):
        self.concept_id = concept_id
        self.short_code = short_code
        self._fetch_concept_name = fetch_concept_name
        self._fetch_type = fetch_type
        self._fetch_status = fetch_status
        self._fetch_content = fetch_content
        self._fetch_valence = fetch_valence
        self._fetch_members = fetch_members
        self._cache: dict[str, Any] = {}

    @property
    def concept_name(self) -> str:
        if "concept_name" not in self._cache:
            self._cache["concept_name"] = self._fetch_concept_name()
        return self._cache["concept_name"]

    @property
    def type(self) -> str | None:
        if "type" not in self._cache:
            self._cache["type"] = self._fetch_type()
        return self._cache["type"]

    @property
    def status(self) -> str | None:
        if "status" not in self._cache:
            self._cache["status"] = self._fetch_status()
        return self._cache["status"]

    @property
    def content(self) -> str | None:
        if "content" not in self._cache:
            self._cache["content"] = self._fetch_content()
        return self._cache["content"]

    @property
    def valence(self) -> float | None:
        if "valence" not in self._cache:
            self._cache["valence"] = self._fetch_valence()
        return self._cache["valence"]

    @property
    def members(self) -> list[ConceptProxy]:
        if "members" not in self._cache:
            self._cache["members"] = self._fetch_members()
        return self._cache["members"]


class ClusterProxy:
    """Lazy proxy for all concepts carrying a given tag."""

    def __init__(self, *,
                 fetch_concepts: Callable[[], list[ConceptProxy]],
                 fetch_count: Callable[[], int]):
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

    def __init__(self, tag_name: str, this_proxy: ConceptProxy,
                 changed: dict | None):
        self.tag_name = tag_name
        self.this = this_proxy
        self.changed = changed if changed is not None else {}
        self._infos: list[str] = []

    def reject(self, msg: str) -> None:
        raise HookRejection(msg)

    def info(self, msg: str) -> None:
        self._infos.append(msg)

    def warn(self, msg: str) -> None:
        raise RuntimeError(
            "warn() is not available in on_mutation context. "
            "Use reject() to block or info() to advise.")


class AuditContext:
    """Context passed to audit_cluster(ctx)."""

    def __init__(self, tag_name: str, cluster_proxy: ClusterProxy):
        self.tag_name = tag_name
        self.cluster = cluster_proxy
        self._warnings: list[str] = []

    def warn(self, msg: str) -> None:
        self._warnings.append(msg)

    def reject(self, msg: str) -> None:
        raise RuntimeError(
            "reject() is not available in audit_cluster context. "
            "Use warn() to report issues.")

    def info(self, msg: str) -> None:
        raise RuntimeError(
            "info() is not available in audit_cluster context. "
            "Use warn() to report issues.")
