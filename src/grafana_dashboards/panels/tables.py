from __future__ import annotations

from grafana_foundation_sdk.builders import (
    bargauge as bargauge_b,
    common as common_b,
    dashboardv2beta1 as v2,
    table as table_b,
)
from grafana_foundation_sdk.models.common import (
    BarGaugeDisplayMode, BarGaugeValueMode, VizOrientation,
)

from grafana_dashboards.panels._common import (
    HOST_FILTER, LokiQuery, PromQuery, target,
)


def _bars_panel(pid: int, title: str, query, unit: str) -> v2.Panel:
    # Horizontal bar gauge: one bar per series (groupname), length ∝ value, so
    # the worst offenders are obvious at a glance — unlike a table of one
    # instant-vector row. lastNotNull reduces each series to its current value.
    viz = (
        bargauge_b.Visualization()
        .unit(unit)
        .orientation(VizOrientation.HORIZONTAL)
        .display_mode(BarGaugeDisplayMode.GRADIENT)
        .value_mode(BarGaugeValueMode.TEXT)
        .reduce_options(
            common_b.ReduceDataOptions().calcs(["lastNotNull"]).values(False)
        )
    )
    return v2.Panel().id(pid).title(title).data(target(query)).visualization(viz)


def _table_panel(pid: int, title: str, query) -> v2.Panel:
    return (
        v2.Panel()
        .id(pid)
        .title(title)
        .data(target(query))
        .visualization(table_b.Visualization())
    )


def top_cgroup_cpu_table() -> v2.Panel:
    expr = f"topk(10, host:cgroup_cpu:sum5m{{{HOST_FILTER}}})"
    return _table_panel(601, "Top units by CPU (5m)", PromQuery(expr, instant=True))


def top_cgroup_mem_table() -> v2.Panel:
    expr = f"topk(10, host:cgroup_memory_rss:sum5m{{{HOST_FILTER}}})"
    return _table_panel(602, "Top units by RSS (5m)", PromQuery(expr, instant=True))


def top_process_cpu_table() -> v2.Panel:
    # process-exporter groups by .Comm -> groupname. Sum across user/system
    # modes. "what's eating CPU" answered per executable (chrome, hyprland, …).
    expr = (
        "topk(15, sum by (groupname) ("
        f'rate(namedprocess_namegroup_cpu_seconds_total{{{HOST_FILTER}}}[5m])'
        "))"
    )
    return _bars_panel(603, "Top processes by CPU (cores, 5m)",
                       PromQuery(expr, instant=True, legend="{{groupname}}"),
                       "none")


def top_process_mem_table() -> v2.Panel:
    # PSS (proportionalResident), NOT RSS: RSS sums shared pages once per process
    # so multi-process apps (chrome = ~99 procs) balloon to nonsense. PSS splits
    # shared memory across sharers = the real per-app footprint.
    expr = (
        "topk(15, sum by (groupname) ("
        f'namedprocess_namegroup_memory_bytes{{{HOST_FILTER},memtype="proportionalResident"}}'
        "))"
    )
    return _bars_panel(604, "Top processes by memory (PSS)",
                       PromQuery(expr, instant=True, legend="{{groupname}}"),
                       "bytes")


def top_error_units_table() -> v2.Panel:
    expr = (
        'topk(10, sum by (unit) ('
        f'rate({{{HOST_FILTER},priority=~"0|1|2|3"}}[5m])'
        '))'
    )
    return _table_panel(701, "Top error-emitting units (5m)", LokiQuery(expr))
