from __future__ import annotations

from grafana_foundation_sdk.builders import (
    dashboard as dashboard_b,    # ThresholdsConfig BUILDER lives here
    dashboardv2beta1 as v2,
    stat as stat_b,
)
from grafana_foundation_sdk.cog.builder import Builder
# Threshold and ThresholdsMode MODELS live in dashboardv2beta1 (matches
# the type that stat.Visualization.thresholds expects).
from grafana_foundation_sdk.models.dashboardv2beta1 import (
    Threshold,
    ThresholdsConfig,
    ThresholdsMode,
)
from grafana_foundation_sdk.models.common import BigValueGraphMode

from grafana_dashboards.panels._common import HOST_FILTER, PromQuery, target


def _thresholds(*steps: tuple[str, float | None]) -> Builder[ThresholdsConfig]:
    # Return the BUILDER, not the built model. stat.thresholds() expects
    # Builder[ThresholdsConfig] per its signature.
    return (
        dashboard_b.ThresholdsConfig()
        .mode(ThresholdsMode.ABSOLUTE)
        .steps([Threshold(color=c, value=v) for c, v in steps])
    )


_PSI_THRESHOLDS = _thresholds(
    ("green", None), ("yellow", 10), ("orange", 30), ("red", 60),
)
_TEMP_THRESHOLDS = _thresholds(
    ("green", None), ("yellow", 75), ("orange", 85), ("red", 95),
)
_STUTTER_THRESHOLDS = _thresholds(
    ("green", None), ("yellow", 1), ("red", 3),
)


def _stat(pid: int, title: str, expr: str, *, unit: str = "short",
          thresholds: Builder[ThresholdsConfig] | None = None) -> v2.Panel:
    viz = stat_b.Visualization().unit(unit).graph_mode(BigValueGraphMode.AREA)
    if thresholds is not None:
        viz = viz.thresholds(thresholds)
    return (
        v2.Panel()
        .id(pid)
        .title(title)
        .data(target(PromQuery(expr, instant=True)))
        .visualization(viz)
    )


def stat_psi_cpu() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_cpu_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(101, "PSI CPU (1m %)", expr, unit="percent",
                 thresholds=_PSI_THRESHOLDS)


def stat_psi_mem() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_memory_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(102, "PSI Memory (1m %)", expr, unit="percent",
                 thresholds=_PSI_THRESHOLDS)


def stat_psi_io() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_io_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(103, "PSI I/O (1m %)", expr, unit="percent",
                 thresholds=_PSI_THRESHOLDS)


def stat_load1() -> v2.Panel:
    return _stat(104, "Load 1m", f"node_load1{{{HOST_FILTER}}}")


def stat_load5() -> v2.Panel:
    return _stat(105, "Load 5m", f"node_load5{{{HOST_FILTER}}}")


def stat_load15() -> v2.Panel:
    return _stat(106, "Load 15m", f"node_load15{{{HOST_FILTER}}}")


def stat_uptime() -> v2.Panel:
    expr = (
        f"node_time_seconds{{{HOST_FILTER}}} - "
        f"node_boot_time_seconds{{{HOST_FILTER}}}"
    )
    return _stat(107, "Uptime", expr, unit="s")


def stat_temp() -> v2.Panel:
    expr = f"max(node_hwmon_temp_celsius{{{HOST_FILTER}}})"
    return _stat(108, "Max CPU temp", expr, unit="celsius",
                 thresholds=_TEMP_THRESHOLDS)


def stat_stutter_count() -> v2.Panel:
    # NOTE: v2.Panel in SDK 0.0.12 does not expose a per-panel time
    # override method. The recording rule itself uses a 5m window, so
    # the value reads "events in the last 5m" naturally.
    expr = f"host:psi_cpu_stutter_events:count5m{{{HOST_FILTER}}}"
    return _stat(109, "Stutter events (last 5m)", expr,
                 thresholds=_STUTTER_THRESHOLDS)
