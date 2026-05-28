from __future__ import annotations

from grafana_foundation_sdk.cog.builder import Builder
from grafana_foundation_sdk.models.dashboardv2beta1 import DataQueryKind

from grafana_dashboards.panels._common import (
    PROM_DS_VAR, LOKI_DS_VAR, HOST_FILTER,
    PromQuery, LokiQuery, target,
    legend_table_right, tooltip_multi,
)


def test_prom_query_inherits_builder():
    assert issubclass(PromQuery, Builder)
    assert issubclass(LokiQuery, Builder)


def test_prom_query_wraps_in_v2_envelope():
    q = PromQuery("up", legend="{{instance}}").build()
    assert isinstance(q, DataQueryKind)
    assert q.group == "prometheus"
    assert q.version == "v0"
    assert q.datasource.name == PROM_DS_VAR
    assert q.spec["expr"] == "up"
    assert q.spec["legendFormat"] == "{{instance}}"
    assert q.spec["refId"] == "A"
    assert q.spec["editorMode"] == "code"


def test_loki_query_wraps_in_v2_envelope():
    q = LokiQuery('{job="x"} |= "err"').build()
    assert q.group == "loki"
    assert q.datasource.name == LOKI_DS_VAR
    assert q.spec["expr"] == '{job="x"} |= "err"'


def test_ds_vars_are_variable_expansions():
    assert PROM_DS_VAR == "$ds_prom"
    assert LOKI_DS_VAR == "$ds_loki"


def test_host_filter_uses_underscore_label():
    # Critical: Prometheus label syntax rejects dots; OTEL semconv
    # values land under host_name (underscore form).
    assert HOST_FILTER == 'host_name="$host"'


def test_target_returns_built_query_group():
    qg = target(PromQuery("up"))
    built = qg.build()
    # v2 QueryGroup model exposes targets in spec.queries (list).
    # Smoke-test we can reach the inner Target without exception.
    assert built is not None
