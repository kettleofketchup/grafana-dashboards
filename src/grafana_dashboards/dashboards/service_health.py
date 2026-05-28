"""Example: minimal service-health dashboard.

A two-panel starter board (instance count + per-instance up timeline)
that demonstrates the v2beta1 generator pattern: datasource variable,
query variable, panels wrapping ``DataQueryKind`` envelopes, and a
``RowsLayout`` containing a single ``Grid``.

Copy this module to ``src/grafana_dashboards/dashboards/<your_slug>.py``,
adjust the panels/queries/variables, and add the import to
``dashboards/__init__.py:_AUTOLOAD``.
"""

from __future__ import annotations

from grafana_foundation_sdk.builders import common as common_b
from grafana_foundation_sdk.builders import dashboardv2beta1 as v2
from grafana_foundation_sdk.builders import stat as stat_b
from grafana_foundation_sdk.builders import timeseries as timeseries_b
from grafana_foundation_sdk.cog.builder import Builder
from grafana_foundation_sdk.models.common import (
    BigValueGraphMode,
    GraphDrawStyle,
    LegendDisplayMode,
    LegendPlacement,
    SortOrder,
    StackingMode,
    TooltipDisplayMode,
)
from grafana_foundation_sdk.models.dashboardv2beta1 import (
    DashboardCursorSync,
    Dashboardv2beta1DataQueryKindDatasource,
    DataQueryKind,
    VariableRefresh,
)

from grafana_dashboards.dashboards import DashboardSpec, register

__all__: list[str] = []

PROM_DS_VAR = "$ds_prom"


class _PromQuery(Builder[DataQueryKind]):
    """Wrap a PromQL expression in v2's ``DataQueryKind`` envelope.

    The SDK's per-datasource builders emit v1-shaped query bodies; v2
    requires a kind/group/version envelope around them or Grafana Cloud's
    validator rejects the dashboard. Same shim pattern as draftforge.
    """

    def __init__(self, expr: str, *, legend: str = "", instant: bool = False) -> None:
        spec: dict = {"expr": expr, "editorMode": "code", "refId": "A"}
        if legend:
            spec["legendFormat"] = legend
        if instant:
            spec["instant"] = True
        self._inner = DataQueryKind(
            group="prometheus",
            version="v0",
            datasource=Dashboardv2beta1DataQueryKindDatasource(name=PROM_DS_VAR),
            spec=spec,
        )

    def build(self) -> DataQueryKind:
        return self._inner


def _query_group(expr: str, legend: str = "", *, instant: bool = False) -> v2.QueryGroup:
    return v2.QueryGroup().target(
        v2.Target().ref_id("A").query(_PromQuery(expr, legend=legend, instant=instant)),
    )


def _ts_viz() -> timeseries_b.Visualization:
    return (
        timeseries_b.Visualization()
        .unit("short")
        .draw_style(GraphDrawStyle.LINE)
        .fill_opacity(10)
        .stacking(common_b.StackingConfig().mode(StackingMode.NONE).group("A"))
        .legend(
            common_b.VizLegendOptions()
            .show_legend(True)
            .placement(LegendPlacement.RIGHT)
            .display_mode(LegendDisplayMode.TABLE)
            .calcs(["lastNotNull", "max"]),
        )
        .tooltip(
            common_b.VizTooltipOptions()
            .mode(TooltipDisplayMode.MULTI)
            .sort(SortOrder.DESCENDING),
        )
    )


def _stat_viz() -> stat_b.Visualization:
    return stat_b.Visualization().unit("short").graph_mode(BigValueGraphMode.AREA)


def _panel(
    pid: int,
    title: str,
    viz: timeseries_b.Visualization | stat_b.Visualization,
    query: v2.QueryGroup,
    *,
    description: str = "",
) -> v2.Panel:
    return (
        v2.Panel()
        .id(pid)
        .title(title)
        .description(description)
        .data(query)
        .visualization(viz)
    )


def _variables() -> list:
    ds = (
        v2.DatasourceVariable("ds_prom")
        .label("Prometheus")
        .plugin_id("prometheus")
        .description("Prometheus datasource to query.")
    )
    job = (
        v2.QueryVariable("job")
        .label("Job")
        .query(_PromQuery("label_values(up, job)"))
        .refresh(VariableRefresh.ON_DASHBOARD_LOAD)
        .multi(True)
        .include_all(True)
        # `.+` rather than the default `.*` so the All expansion never
        # produces an empty-compatible matcher that Prometheus rejects.
        .all_value(".+")
    )
    return [ds, job]


@register("service-health")
def build() -> DashboardSpec:
    """Build the service-health dashboard."""
    instances = _panel(
        1,
        "Instances up",
        _stat_viz(),
        _query_group('sum(up{job=~"$job"})', instant=True),
        description="Count of up instances across selected jobs.",
    )
    timeline = _panel(
        2,
        "Per-instance up timeline",
        _ts_viz(),
        _query_group('up{job=~"$job"}', legend="{{job}}/{{instance}}"),
        description="Per-instance up=1/down=0 series. Drops to 0 mark outages.",
    )

    grid = (
        v2.Grid()
        .item(v2.GridItem().name("instances-up").x(0).y(0).width(6).height(6))
        .item(v2.GridItem().name("up-timeline").x(6).y(0).width(18).height(6))
    )
    rows = v2.Rows().row(
        v2.Row().title("Service health").collapse(False).layout(grid),
    )

    builder = (
        v2.Dashboard("Service Health")
        .description(
            "Minimal Prometheus service-health dashboard. Generated from "
            "kettle-grafana-dashboards as a starter template.",
        )
        .tags(["kettle-grafana-dashboards", "prometheus", "service-health"])
        .editable(True)
        .preload(False)
        .live_now(False)
        .cursor_sync(DashboardCursorSync.OFF)
        .time_settings(
            v2.TimeSettings()
            .from_val("now-1h")
            .to("now")
            .auto_refresh("30s")
            .timezone("browser"),
        )
        .layout(rows)
        .element("instances-up", instances)
        .element("up-timeline", timeline)
    )
    for var in _variables():
        builder = builder.variable(var)

    return DashboardSpec(uid="kettle-service-health", builder=builder)
