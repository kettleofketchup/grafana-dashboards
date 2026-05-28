from __future__ import annotations

from grafana_foundation_sdk.builders import (
    dashboardv2beta1 as v2,
    table as table_b,
)

from grafana_dashboards.panels._common import (
    HOST_FILTER, LokiQuery, PromQuery, target,
)


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


def top_error_units_table() -> v2.Panel:
    expr = (
        'topk(10, sum by (unit) ('
        f'rate({{{HOST_FILTER},priority=~"0|1|2|3"}}[5m])'
        '))'
    )
    return _table_panel(701, "Top error-emitting units (5m)", LokiQuery(expr))
