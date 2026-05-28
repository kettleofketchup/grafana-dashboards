"""kettle-grafana-dashboards package.

Reusable Grafana v2 (Scenes) dashboards as code. The public surface
is small: the CLI entry points, the v2 envelope helper, and the
structural validator. Dashboard authoring uses the registry under
:mod:`grafana_dashboards.dashboards`.
"""

from __future__ import annotations

from grafana_dashboards._internal.cli import get_parser, main
from grafana_dashboards._internal.envelope import wrap_v2
from grafana_dashboards._internal.validate import validate_v2

__all__: list[str] = ["get_parser", "main", "validate_v2", "wrap_v2"]
