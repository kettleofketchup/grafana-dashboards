from __future__ import annotations

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2
from grafana_foundation_sdk.models.dashboardv2beta1 import (
    VariableOption,
    VariableRefresh,
)

from grafana_dashboards.panels._common import PromQuery


def build_variables() -> list:
    ds_prom = (
        v2.DatasourceVariable("ds_prom")
        .label("Prometheus")
        .plugin_id("prometheus")
        .description("Prometheus datasource for host metrics.")
    )
    ds_loki = (
        v2.DatasourceVariable("ds_loki")
        .label("Loki")
        .plugin_id("loki")
        .description("Loki datasource for host journald logs.")
    )
    # Panels scope via role="workstation" (see panels/_common.HOST_FILTER), so
    # this is now display-only — a constant, not a query. A QueryVariable using
    # label_values(...) rendered empty under Grafana 12.4's v2 schema (the
    # label_values template fn isn't interpreted in a raw v2 DataQuery), which
    # left the dropdown blank; a CustomVariable can't go empty.
    host = (
        v2.CustomVariable("host")
        .label("Host")
        .query("kettle-omarchy")
        .current(VariableOption(text="kettle-omarchy", value="kettle-omarchy"))
        .multi(False)
        .include_all(False)
    )
    window = (
        v2.CustomVariable("window")
        .label("Window")
        .query("1m,5m,15m,1h,6h")
        # current must be a VariableOption (text/value), NOT a bare string —
        # Grafana 12.4's v2beta1 schema rejects a string here with
        # "cannot unmarshal string ... into v2beta1.DashboardVariableOption".
        .current(VariableOption(text="5m", value="5m"))
    )
    return [ds_prom, ds_loki, host, window]
