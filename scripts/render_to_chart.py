"""Render registered dashboards as grafana-operator Dashboard CRs.

For each registered dashboard:
  - writes templates/<uid>.yaml (a Dashboard CR carrying the full
    v2beta1 envelope JSON in spec.json)
  - writes templates/<uid>-rules.yaml (PrometheusRule CR) if the
    dashboard module exports a RECORDING_RULES list
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import yaml
from grafana_foundation_sdk.cog.encoder import JSONEncoder

from grafana_dashboards._internal.envelope import wrap_v2
from grafana_dashboards._internal.validate import validate_v2
from grafana_dashboards.dashboards import all_dashboards

ENCODER = JSONEncoder(sort_keys=False, indent=2)


def _dashboard_cr(uid: str, envelope: dict, *, folder: str,
                  instance_label: str) -> dict:
    # grafana-operator v5 CRD kind is `GrafanaDashboard` (single word,
    # matches `grafanadashboards.grafana.integreatly.org`). Likewise
    # `GrafanaDatasource`, `GrafanaFolder`, `Grafana`. NOT plain `Dashboard`.
    return {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "GrafanaDashboard",
        "metadata": {
            "name": uid,
            "labels": {"app.kubernetes.io/managed-by": "grafana-dashboards"},
        },
        "spec": {
            "instanceSelector": {
                "matchLabels": {"dashboards": instance_label},
            },
            "folder": folder,
            "resyncPeriod": "5m",
            "json": json.dumps(envelope, indent=2),
        },
    }


def _prometheus_rule(uid: str, rules: list[dict]) -> dict:
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": f"{uid}-rules",
            "labels": {"prometheus": "kube-prometheus", "role": "alert-rules"},
        },
        "spec": {
            "groups": [{
                "name": uid,
                "interval": "30s",
                "rules": [{"record": r["record"], "expr": r["expr"]} for r in rules],
            }],
        },
    }


def render(slugs: list[str], chart_dir: Path, *,
           folder: str = "Workstation",
           instance_label: str = "kube-prometheus-stack") -> None:
    templates = chart_dir / "templates"
    templates.mkdir(parents=True, exist_ok=True)

    registry = all_dashboards()
    for slug in slugs:
        if slug not in registry:
            print(f"unknown slug: {slug}", file=sys.stderr)
            sys.exit(2)

        spec = registry[slug]()
        body = json.loads(ENCODER.encode(spec.builder.build()))
        envelope = wrap_v2(body, uid=spec.uid)

        issues = validate_v2(envelope)
        if issues:
            for i in issues:
                print(f"{slug}: {i}", file=sys.stderr)
            sys.exit(1)

        cr_path = templates / f"{spec.uid}.yaml"
        cr_yaml = yaml.safe_dump(
            _dashboard_cr(spec.uid, envelope, folder=folder,
                          instance_label=instance_label),
            sort_keys=False, width=10_000,
        )
        # Escape Helm Go-template delimiters that appear in Grafana legend
        # formats like {{cpu}} or {{device}} embedded in the Dashboard JSON.
        # Helm parses `{{ ... }}` in chart templates as actions; wrap the
        # opening `{{` as a literal-string action so Helm renders it back.
        cr_yaml = cr_yaml.replace("{{", '{{ "{{" }}')
        cr_path.write_text(cr_yaml)
        print(f"wrote {cr_path}")

        dash_module = importlib.import_module(
            f"grafana_dashboards.dashboards.{slug.replace('-', '_')}"
        )
        rules = getattr(dash_module, "RECORDING_RULES", None)
        if rules:
            rule_path = templates / f"{spec.uid}-rules.yaml"
            rule_path.write_text(yaml.safe_dump(
                _prometheus_rule(spec.uid, rules), sort_keys=False, width=10_000,
            ))
            print(f"wrote {rule_path}")


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-o", "--out", type=Path, required=True)
    p.add_argument("-d", "--dashboard", action="append", default=None)
    p.add_argument("--folder", default="Workstation")
    p.add_argument("--instance-label", default="kube-prometheus-stack")
    args = p.parse_args()
    slugs = args.dashboard or sorted(all_dashboards())
    render(slugs, args.out, folder=args.folder,
           instance_label=args.instance_label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
