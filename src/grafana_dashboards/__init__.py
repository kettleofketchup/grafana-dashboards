"""grafana-dashboards package.

Grafana Dashboards that are reusable across multiple projects or a good starting point
"""

from __future__ import annotations

from grafana_dashboards._internal.cli import get_parser, main

__all__: list[str] = ["get_parser", "main"]
