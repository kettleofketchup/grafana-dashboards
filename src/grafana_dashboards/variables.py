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
    host = (
        v2.QueryVariable("host")
        .label("Host")
        # Scope the source to role="workstation" so this only ever lists
        # workstation host_names — never the cluster's own node-exporters
        # (which carry no host_name). Default current to kettle-omarchy so an
        # empty selection can't collapse panel filters to host_name="" (which
        # in PromQL matches the unlabeled cluster series instead of the PC).
        .query(PromQuery('label_values(up{role="workstation"}, host_name)'))
        .refresh(VariableRefresh.ON_DASHBOARD_LOAD)
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
