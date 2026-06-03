"""Workstation host dashboard for kettle-omarchy.

UID: kettle-host-omarchy. Deployed as a grafana-operator Dashboard CR;
recording rules deployed as a PrometheusRule CR.
"""

from __future__ import annotations

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2
from grafana_foundation_sdk.models.dashboardv2beta1 import DashboardCursorSync

from grafana_dashboards.dashboards import DashboardSpec, register
from grafana_dashboards.panels import logs as logs_p
from grafana_dashboards.panels import stat
from grafana_dashboards.panels import tables
from grafana_dashboards.panels import timeseries as ts
from grafana_dashboards.rows import compose_grid
from grafana_dashboards.variables import build_variables


# Recording rules — order matters: rule N may depend on rule <N.
# CRITICAL: every rule MUST scope to role="workstation". Without it these
# recording rules compute over the cluster's own node-exporters too (the 4
# Talos nodes, which have no host_name label), so panels filtered on
# host_name="$host" with an empty $host match the cluster series instead of
# the workstation. role="workstation" is an external label Alloy stamps on all
# workstation remote_write metrics; the cluster node-exporters never carry it.
WS = '{role="workstation"}'
RECORDING_RULES = [
    {
        "record": "host:psi_cpu_waiting:ratio1m",
        "expr": f"rate(node_pressure_cpu_waiting_seconds_total{WS}[1m])",
    },
    {
        "record": "host:psi_memory_waiting:ratio1m",
        "expr": f"rate(node_pressure_memory_waiting_seconds_total{WS}[1m])",
    },
    {
        "record": "host:psi_io_waiting:ratio1m",
        "expr": f"rate(node_pressure_io_waiting_seconds_total{WS}[1m])",
    },
    {
        "record": "host:psi_cpu_stutter_events:count5m",
        # Non-bool comparison filters samples; count_over_time then
        # counts only the truthy 1m samples in the 5m window. Already scoped
        # via host:psi_cpu_waiting:ratio1m (workstation-only above).
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
            'rate(container_cpu_usage_seconds_total{role="workstation",name!=""}[5m])'
            ")"
        ),
    },
    {
        "record": "host:cgroup_memory_rss:sum5m",
        "expr": (
            "sum by (host_name, name) ("
            'avg_over_time(container_memory_rss{role="workstation"}[5m])'
            ")"
        ),
    },
]


@register("host-omarchy")
def build() -> DashboardSpec:
    # Each section is a collapsible RowsLayout row: (title, collapsed_by_default,
    # [(element_name, builder_callable, width, height), ...]). Panels within a
    # section auto-flow on a fresh 24-col grid (compose_grid resets per section).
    # Headline sections (Overview / CPU / GPU / Logs) start expanded; the rest
    # collapse so the page isn't a wall of graphs.
    SECTIONS = [
        ("Overview", False, [
            # right-now indicators — widths sum to 24:
            # 3+3+3 (PSI) + 2+2+2 (load) + 3+3+3 (uptime/temp/stutter)
            ("psi-cpu",        stat.stat_psi_cpu,            3, 3),
            ("psi-mem",        stat.stat_psi_mem,            3, 3),
            ("psi-io",         stat.stat_psi_io,             3, 3),
            ("load1",          stat.stat_load1,              2, 3),
            ("load5",          stat.stat_load5,              2, 3),
            ("load15",         stat.stat_load15,             2, 3),
            ("uptime",         stat.stat_uptime,             3, 3),
            ("temp-max",       stat.stat_temp,               3, 3),
            ("stutter-count",  stat.stat_stutter_count,      3, 3),
            ("psi-all",        ts.ts_psi_all,               24, 8),
        ]),
        ("CPU", False, [
            # Actionable "what's happening" panels stay expanded.
            ("top-cpu",        tables.top_cgroup_cpu_table, 12, 6),
            ("sched-runq",     ts.ts_sched_runqueue,        12, 6),
        ]),
        ("CPU — cores & frequency", True, [
            # Noisy per-core detail (24 lines) + frequency, collapsed by default.
            ("cpu-per-core",   ts.ts_cpu_per_core,          12, 8),
            ("cpu-freq",       ts.ts_cpu_freq,              12, 8),
        ]),
        ("Memory", True, [
            ("mem-break",      ts.ts_mem_breakdown,         12, 8),
            ("top-mem",        tables.top_cgroup_mem_table, 12, 8),
        ]),
        ("GPU", False, [
            ("gpu-util",       ts.ts_gpu_util,              12, 6),
            ("gpu-mem",        ts.ts_gpu_mem,               12, 6),
            ("gpu-temp-power", ts.ts_gpu_temp_power,        12, 6),
            ("gpu-clock",      ts.ts_gpu_clock,             12, 6),
        ]),
        ("Disk & I/O", True, [
            ("disk-iops",      ts.ts_disk_iops,             12, 6),
            ("disk-throughput",ts.ts_disk_throughput,       12, 6),
            ("disk-io-latency",ts.ts_disk_io_latency_avg,   12, 6),
            ("io-wait",        ts.ts_io_wait,               12, 6),
        ]),
        ("IRQ", True, [
            ("irqs",           ts.ts_irqs,                  12, 6),
            ("softirqs",       ts.ts_softirqs,              12, 6),
        ]),
        ("Network", True, [
            ("net-bytes",      ts.ts_net_bytes,             12, 6),
            ("net-errors",     ts.ts_net_errors,            12, 6),
        ]),
        ("Logs & Errors", False, [
            ("err-rate",       logs_p.error_rate_timeseries, 12, 8),
            ("err-units",      tables.top_error_units_table, 12, 8),
            ("err-tail",       logs_p.logs_panel,            24, 10),
        ]),
    ]

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
        .cursor_sync(DashboardCursorSync.CROSSHAIR)
        .time_settings(
            v2.TimeSettings()
            .from_val("now-1h").to("now").auto_refresh("30s").timezone("browser")
        )
    )

    for _title, _collapsed, panels in SECTIONS:
        for name, factory, _w, _h in panels:
            builder = builder.element(name, factory())

    rows = v2.Rows()
    for title, collapsed, panels in SECTIONS:
        grid = v2.Grid()
        for name, x, y, w, h in compose_grid([(n, w, h) for n, _, w, h in panels]):
            grid = grid.item(v2.GridItem().name(name).x(x).y(y).width(w).height(h))
        rows = rows.row(v2.Row().title(title).collapse(collapsed).layout(grid))
    builder = builder.layout(rows)

    for var in build_variables():
        builder = builder.variable(var)

    return DashboardSpec(uid="kettle-host-omarchy", builder=builder)
