from __future__ import annotations

import re
from typing import Any

_VAR_RE = re.compile(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)")

_GRAFANA_BUILTINS = frozenset({
    "__interval",
    "__interval_ms",
    "__range",
    "__range_s",
    "__range_ms",
    "__rate_interval",
    "__auto",
    "__from",
    "__to",
    "__name",
    "__org",
    "__user",
    "__dashboard",
    "__timeFilter",
    "__all",
})

_REQUIRED_SPEC_FIELDS = (
    "title",
    "layout",
    "elements",
    "cursorSync",
    "timeSettings",
    "variables",
    "annotations",
    "links",
    "preload",
    "editable",
    "tags",
)


def validate_v2(dashboard: dict) -> list[str]:
    """Return a list of structural issues with a v2beta1 dashboard dict.

    Empty list means valid. Checks envelope shape, required spec fields,
    layout↔element name resolution, panel id uniqueness, and balanced
    parens/braces/brackets plus variable references inside every `expr`.
    """
    issues: list[str] = []

    for k in ("apiVersion", "kind", "metadata", "spec"):
        if k not in dashboard:
            issues.append(f"envelope: missing {k!r}")
    if dashboard.get("apiVersion") != "dashboard.grafana.app/v2beta1":
        issues.append(
            f"envelope: apiVersion={dashboard.get('apiVersion')!r} (expected v2beta1)"
        )

    spec = dashboard.get("spec", {})
    for k in _REQUIRED_SPEC_FIELDS:
        if k not in spec:
            issues.append(f"spec: missing required field {k!r}")

    elements = spec.get("elements", {})
    if not isinstance(elements, dict):
        issues.append("spec.elements is not a dict")
        elements = {}

    referenced: set[str] = set()
    _walk_layout(spec.get("layout", {}), referenced)
    for n in referenced - set(elements):
        issues.append(f"layout references element {n!r} but it's not in spec.elements")
    for n in set(elements) - referenced:
        issues.append(f"element {n!r} declared but never referenced by layout")

    seen_ids: dict[int, str] = {}
    for name, el in elements.items():
        if not isinstance(el, dict):
            continue
        pid = el.get("spec", {}).get("id")
        if pid is None:
            issues.append(f"element {name!r}: missing spec.id")
            continue
        if pid in seen_ids:
            issues.append(
                f"duplicate panel id {pid}: {seen_ids[pid]!r} and {name!r}"
            )
        else:
            seen_ids[pid] = name

    declared_vars = {
        v.get("spec", {}).get("name") for v in spec.get("variables", [])
    }
    declared_vars.discard(None)

    for path, expr in _walk_exprs(dashboard):
        for name in _VAR_RE.findall(expr):
            if name in _GRAFANA_BUILTINS or name in declared_vars:
                continue
            issues.append(
                f"expr {path}: references ${name} which is not a declared "
                f"variable or known Grafana built-in"
            )
        for opener, closer in (("(", ")"), ("{", "}"), ("[", "]")):
            if expr.count(opener) != expr.count(closer):
                issues.append(f"expr {path}: {opener}{closer} imbalance")

    return issues


def _walk_layout(layout: Any, found: set[str]) -> None:
    if not isinstance(layout, dict):
        return
    sub = layout.get("spec", {})
    for row in sub.get("rows", []) or []:
        _walk_layout(row.get("spec", {}).get("layout", {}), found)
    for item in sub.get("items", []) or []:
        n = item.get("spec", {}).get("element", {}).get("name")
        if n:
            found.add(n)


def _walk_exprs(o: Any, path: str = "$") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    _walk_exprs_into(o, path, out)
    return out


def _walk_exprs_into(o: Any, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(o, dict):
        for k, v in o.items():
            _walk_exprs_into(v, f"{path}.{k}", out)
            if k == "expr" and isinstance(v, str):
                out.append((path, v))
    elif isinstance(o, list):
        for i, x in enumerate(o):
            _walk_exprs_into(x, f"{path}[{i}]", out)
