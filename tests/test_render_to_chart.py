import importlib
import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def runner(tmp_path):
    mod = importlib.import_module("scripts.render_to_chart")
    return mod, tmp_path


def _undo_helm_escape(text: str) -> str:
    """Reverse the `{{ "{{" }}` Helm-template escape that render_to_chart
    applies so the file survives `helm template`. Tests read the raw
    file (no Helm pipeline) so we undo it before YAML+JSON parsing."""
    return text.replace('{{ "{{" }}', "{{")


def test_emits_dashboard_cr_and_prometheus_rule(runner):
    mod, tmp = runner
    out_dir = tmp / "chart"
    mod.render(["host-omarchy"], out_dir)
    cr_file = out_dir / "templates" / "kettle-host-omarchy.yaml"
    rule_file = out_dir / "templates" / "kettle-host-omarchy-rules.yaml"
    assert cr_file.exists()
    assert rule_file.exists()

    cr = yaml.safe_load(_undo_helm_escape(cr_file.read_text()))
    assert cr["apiVersion"] == "grafana.integreatly.org/v1beta1"
    # grafana-operator v5 CRD kind is `GrafanaDashboard` (single word).
    assert cr["kind"] == "GrafanaDashboard"
    assert cr["metadata"]["name"] == "kettle-host-omarchy"
    body = json.loads(cr["spec"]["json"])
    assert body["apiVersion"] == "dashboard.grafana.app/v2beta1"
    assert body["kind"] == "Dashboard"
    assert "spec" in body and "elements" in body["spec"]
    assert cr["spec"]["instanceSelector"]["matchLabels"]["dashboards"] == "kube-prometheus-stack"

    rule = yaml.safe_load(rule_file.read_text())
    assert rule["apiVersion"] == "monitoring.coreos.com/v1"
    assert rule["kind"] == "PrometheusRule"
    rule_records = [r["record"] for r in rule["spec"]["groups"][0]["rules"]]
    assert "host:psi_cpu_waiting:ratio1m" in rule_records
    assert "host:cgroup_cpu:sum5m" in rule_records


def test_skips_rule_when_no_RECORDING_RULES(runner):
    mod, tmp = runner
    out_dir = tmp / "chart"
    mod.render(["service-health"], out_dir)
    cr = out_dir / "templates" / "kettle-service-health.yaml"
    rule = out_dir / "templates" / "kettle-service-health-rules.yaml"
    assert cr.exists()
    assert not rule.exists()
