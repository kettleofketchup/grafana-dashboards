"""Workstation host dashboard for kettle-omarchy.

UID: kettle-host-omarchy. Deployed as a grafana-operator Dashboard CR;
recording rules deployed as a PrometheusRule CR.
"""

from __future__ import annotations

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2
from grafana_foundation_sdk.models.dashboardv2beta1 import DashboardCursorSync

from grafana_dashboards.dashboards import DashboardSpec, register
from grafana_dashboards.variables import build_variables


# Recording rules — order matters: rule N may depend on rule <N.
RECORDING_RULES = [
    {
        "record": "host:psi_cpu_waiting:ratio1m",
        "expr": "rate(node_pressure_cpu_waiting_seconds_total[1m])",
    },
    {
        "record": "host:psi_memory_waiting:ratio1m",
        "expr": "rate(node_pressure_memory_waiting_seconds_total[1m])",
    },
    {
        "record": "host:psi_io_waiting:ratio1m",
        "expr": "rate(node_pressure_io_waiting_seconds_total[1m])",
    },
    {
        "record": "host:psi_cpu_stutter_events:count5m",
        # Non-bool comparison filters samples; count_over_time then
        # counts only the truthy 1m samples in the 5m window.
        "expr": (
            "count_over_time("
            "(host:psi_cpu_waiting:ratio1m > 0.30)[5m:1m]"
            ")"
        ),
    },
    {
        "record": "host:cgroup_cpu:sum5m",
        "expr": (
            "sum by (host_name, name) ("
            'rate(container_cpu_usage_seconds_total{name!=""}[5m])'
            ")"
        ),
    },
    {
        "record": "host:cgroup_memory_rss:sum5m",
        "expr": (
            "sum by (host_name, name) ("
            "avg_over_time(container_memory_rss[5m])"
            ")"
        ),
    },
]


@register("host-omarchy")
def build() -> DashboardSpec:
    builder = (
        v2.Dashboard("Workstation — kettle-omarchy")
        .description(
            "Host monitoring for the Omarchy workstation: CPU, memory, GPU, "
            "I/O, network, PSI, IRQ, journald errors. Click-drag a PSI spike "
            "to zoom every panel below to the stutter window."
        )
        .tags(["workstation", "kettle-omarchy", "host", "psi"])
        .editable(True)
        .preload(False)
        .live_now(False)
        # v2 feature: crosshair sync across panels (hover on one =
        # crosshair on all). Surfaces correlations in stutter forensics.
        .cursor_sync(DashboardCursorSync.CROSSHAIR)
        .time_settings(
            v2.TimeSettings()
            .from_val("now-1h").to("now").auto_refresh("30s").timezone("browser")
        )
    )
    for var in build_variables():
        builder = builder.variable(var)
    # Empty layout for now — B10b fills it.
    builder = builder.layout(v2.Rows())
    return DashboardSpec(uid="kettle-host-omarchy", builder=builder)
