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
    # count_over_time (not rate): error logs are sparse, so a per-second rate
    # reads as an empty/near-zero line. Counting matched lines per interval
    # bucket renders each error burst as a clear discrete value. priority<=3 =
    # err/crit/alert/emerg. "No data" here legitimately means "no errors".
    expr = (
        'sum by (unit) ('
        f'count_over_time({{{HOST_FILTER},priority=~"0|1|2|3"}}[$__interval])'
        ')'
    )
    return (
        v2.Panel()
        .id(702)
        .title("Error log count by unit")
        .data(target(LokiQuery(expr)))
        .visualization(_ts_viz())
    )


def _logs_viz() -> logs_b.Visualization:
    return (
        logs_b.Visualization()
        .show_time(True)
        .show_labels(False)         # keep the row clean; click a line for labels
        .show_common_labels(False)
        .wrap_log_message(True)
        .enable_log_details(True)
        .dedup_strategy(LogsDedupStrategy.NONE)
    )


def logs_panel() -> v2.Panel:
    # All journal logs except debug (priority 7), newest first — a live per-line
    # feed so the Logs section actually has content (errors alone are too
    # sparse). unit + priority are one click away per line.
    expr = f'{{{HOST_FILTER},priority!="7"}}'
    return (
        v2.Panel()
        .id(703)
        .title("Log tail (all units)")
        .data(target(LokiQuery(expr)))
        .visualization(_logs_viz())
    )


def error_logs_panel() -> v2.Panel:
    # Errors only (priority<=3), per line — the actionable subset.
    expr = f'{{{HOST_FILTER},priority=~"0|1|2|3"}}'
    return (
        v2.Panel()
        .id(704)
        .title("Error log tail")
        .data(target(LokiQuery(expr)))
        .visualization(_logs_viz())
    )
