from __future__ import annotations

import json
import re
from grafana_foundation_sdk.cog.encoder import JSONEncoder

from grafana_dashboards._internal.envelope import wrap_v2
from grafana_dashboards._internal.validate import validate_v2
from grafana_dashboards.dashboards import all_dashboards


ENCODER = JSONEncoder(sort_keys=False, indent=2)


def _render(slug: str) -> tuple[dict, str]:
    registry = all_dashboards()
    spec = registry[slug]()
    body = json.loads(ENCODER.encode(spec.builder.build()))
    wrapped = wrap_v2(body, uid=spec.uid)
    return wrapped, json.dumps(wrapped, indent=2)


def test_host_omarchy_passes_validator():
    wrapped, _ = _render("host-omarchy")
    issues = validate_v2(wrapped)
    assert issues == [], "validator issues:\n" + "\n".join(issues)


def test_host_omarchy_uses_ds_var_in_panel_queries():
    """Panel-level datasource refs use the $ds_prom/$ds_loki variables,
    never a hard-coded UID.

    Verified encoding path (grafana-foundation-sdk==0.0.12):
      spec.elements.<name>.spec.data.spec.queries[i].spec.query.datasource
    Note the `.query` level between PanelQuery.spec and DataQueryKind.
    """
    wrapped, _ = _render("host-omarchy")
    elements = wrapped["spec"]["elements"]
    checked = 0
    for name, element in elements.items():
        for pq in (element.get("spec", {})
                          .get("data", {})
                          .get("spec", {})
                          .get("queries", [])):
            ds = (pq.get("spec", {})
                    .get("query", {})
                    .get("datasource") or {})
            name_field = ds.get("name", "")
            uid_field = ds.get("uid", "")
            assert name_field in ("$ds_prom", "$ds_loki"), (
                f"element {name!r} query datasource name={name_field!r} "
                f"(expected $ds_prom or $ds_loki)"
            )
            assert not uid_field, (
                f"element {name!r} query has hardcoded uid={uid_field!r}"
            )
            checked += 1
    assert checked > 0, "no panel queries found — path walking is wrong"


def test_host_omarchy_no_logql_backslash_overescape():
    """In JSON source, `\\\\\\\\.` (4 chars) decodes to `\\\\.` in memory
    (2 chars: literal backslash + dot). In LogQL backtick/regex contexts
    that's `\\.` regex = literal-backslash-then-any-char, NOT `\\.` = dot.

    We render via json.dumps so we get the in-memory representation;
    pattern to find is therefore `\\\\.` (2 chars: backslash + dot) — any
    sequence of 2+ in-memory backslashes immediately followed by a dot
    is an over-escape."""
    _, rendered = _render("host-omarchy")
    bug = re.compile(r"\\{2,}\.")
    matches = bug.findall(rendered)
    assert not matches, f"backslash over-escape in LogQL expr: {matches}"


def test_all_registered_dashboards_render_clean():
    for slug, factory in all_dashboards().items():
        spec = factory()
        body = json.loads(ENCODER.encode(spec.builder.build()))
        wrapped = wrap_v2(body, uid=spec.uid)
        issues = validate_v2(wrapped)
        assert issues == [], f"{slug}:\n" + "\n".join(issues)
