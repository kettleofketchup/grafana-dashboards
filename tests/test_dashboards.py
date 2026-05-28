"""Smoke tests for the dashboard registry and generator."""

from __future__ import annotations

import json
from pathlib import Path

from grafana_dashboards import main
from grafana_dashboards._internal.validate import validate_v2
from grafana_dashboards.dashboards import all_dashboards


def test_registry_nonempty() -> None:
    """At least one dashboard is registered."""
    assert all_dashboards(), "expected at least one registered dashboard"


def test_example_dashboard_present() -> None:
    """The example dashboard ships in the registry."""
    assert "service-health" in all_dashboards()


def test_generate_writes_valid_v2(tmp_path: Path) -> None:
    """`kgd generate` writes a JSON file per dashboard and the result validates."""
    rc = main(["generate", "--output", str(tmp_path)])
    assert rc == 0

    written = sorted(tmp_path.glob("*.json"))
    assert written, "generate produced no JSON files"

    for path in written:
        dashboard = json.loads(path.read_text())
        issues = validate_v2(dashboard)
        assert not issues, f"{path.name} failed validation:\n  " + "\n  ".join(issues)
        assert dashboard["apiVersion"] == "dashboard.grafana.app/v2beta1"
        assert "spec" in dashboard


def test_list_command(capsys) -> None:  # noqa: ANN001
    """`kgd list` prints registered slugs."""
    rc = main(["list"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "service-health" in captured


def test_generate_unknown_slug_errors(tmp_path: Path) -> None:
    """Unknown --dashboard slug returns a usage error."""
    rc = main(["generate", "--output", str(tmp_path), "--dashboard", "does-not-exist"])
    assert rc == 2
