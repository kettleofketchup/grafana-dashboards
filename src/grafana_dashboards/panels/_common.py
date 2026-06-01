from __future__ import annotations

from grafana_foundation_sdk.builders import (
    common as common_b,
    dashboardv2beta1 as v2,
)
from grafana_foundation_sdk.cog.builder import Builder
from grafana_foundation_sdk.models.common import (
    LegendDisplayMode,
    LegendPlacement,
    SortOrder,
    TooltipDisplayMode,
)
from grafana_foundation_sdk.models.dashboardv2beta1 import (
    DataQueryKind,
    Dashboardv2beta1DataQueryKindDatasource,
)

PROM_DS_VAR = "$ds_prom"
LOKI_DS_VAR = "$ds_loki"

# Scope EVERY panel (Prometheus + Loki) to the workstation via the role label
# Alloy stamps on all its metrics/logs. We deliberately do NOT use
# host_name="$host": the cluster's own node-exporters carry no role/host_name,
# so an empty/unresolved $host collapses the filter to host_name="" which
# matches the unlabeled CLUSTER series instead of the PC (uptime showed 4 Talos
# nodes, disk showed Ceph rbd0). role="workstation" can't go empty and works for
# raw metrics, recording-rule outputs (they inherit role), nvidia, cadvisor, and
# Loki streams alike. Reintroduce a host variable here only when >1 workstation.
HOST_FILTER = 'role="workstation"'


class PromQuery(Builder[DataQueryKind]):
    """Wrap a PromQL expression in v2's DataQueryKind envelope.

    v2 rejects the v1-shape query bodies the SDK's per-datasource
    builders still emit. Same shim pattern as service_health.py.
    """

    def __init__(
        self,
        expr: str,
        *,
        legend: str = "",
        ref_id: str = "A",
        instant: bool = False,
    ) -> None:
        spec: dict = {"expr": expr, "editorMode": "code", "refId": ref_id}
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


class LokiQuery(Builder[DataQueryKind]):
    """Wrap a LogQL expression in v2's DataQueryKind envelope."""

    def __init__(self, expr: str, *, legend: str = "", ref_id: str = "A") -> None:
        spec: dict = {"expr": expr, "editorMode": "code", "refId": ref_id}
        if legend:
            spec["legendFormat"] = legend
        self._inner = DataQueryKind(
            group="loki",
            version="v0",
            datasource=Dashboardv2beta1DataQueryKindDatasource(name=LOKI_DS_VAR),
            spec=spec,
        )

    def build(self) -> DataQueryKind:
        return self._inner


def target(query: Builder[DataQueryKind], ref_id: str = "A") -> v2.QueryGroup:
    """Single-target QueryGroup."""
    return v2.QueryGroup().target(v2.Target().ref_id(ref_id).query(query))


def legend_table_right() -> common_b.VizLegendOptions:
    return (
        common_b.VizLegendOptions()
        .show_legend(True)
        .placement(LegendPlacement.RIGHT)
        .display_mode(LegendDisplayMode.TABLE)
        .calcs(["lastNotNull", "max"])
    )


def tooltip_multi() -> common_b.VizTooltipOptions:
    """Multi-series tooltip, descending sort. Uses the SortOrder enum
    (NOT the string 'desc' — SDK type-checks against the model)."""
    return (
        common_b.VizTooltipOptions()
        .mode(TooltipDisplayMode.MULTI)
        .sort(SortOrder.DESCENDING)
    )
