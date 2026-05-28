from __future__ import annotations

from grafana_foundation_sdk.builders import (
    dashboardv2beta1 as v2,
    logs as logs_b,
)
# LogsDedupStrategy lives in models.common (verified). Not in models.logs.
from grafana_foundation_sdk.models.common import LogsDedupStrategy

from grafana_dashboards.panels._common import HOST_FILTER, LokiQuery, target
from grafana_dashboards.panels.timeseries import _ts_viz  # type: ignore[attr-defined]


def error_rate_timeseries() -> v2.Panel:
    expr = (
        'sum by (unit) ('
        f'rate({{{HOST_FILTER},priority=~"0|1|2|3"}}[$__rate_interval])'
        ')'
    )
    return (
        v2.Panel()
        .id(702)
        .title("Error log rate by unit")
        .data(target(LokiQuery(expr)))
        .visualization(_ts_viz())
    )


def logs_panel() -> v2.Panel:
    expr = f'{{{HOST_FILTER},priority=~"0|1|2|3"}}'
    viz = (
        logs_b.Visualization()
        .show_time(True)
        .show_labels(False)
        .show_common_labels(False)
        .wrap_log_message(True)
        .enable_log_details(True)
        .dedup_strategy(LogsDedupStrategy.NONE)
    )
    return (
        v2.Panel()
        .id(703)
        .title("Error log tail")
        .data(target(LokiQuery(expr)))
        .visualization(viz)
    )
