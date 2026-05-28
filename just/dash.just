CHART_DIR := env("KETTLE_CHART_DIR", "/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart")

[group('dash')]
[no-cd]
render slug:
    uv run python scripts/render_to_chart.py -o {{CHART_DIR}} -d {{slug}}

[group('dash')]
[no-cd]
render-all:
    uv run python scripts/render_to_chart.py -o {{CHART_DIR}}

[group('dash')]
[no-cd]
validate slug:
    uv run kgd generate -o $(mktemp -d) -d {{slug}}

[group('dash')]
[no-cd]
validate-all:
    uv run kgd generate -o $(mktemp -d)

[group('dash')]
[no-cd]
diff slug:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp=$(mktemp -d)
    uv run python scripts/render_to_chart.py -o "$tmp" -d {{slug}}
    diff -u "{{CHART_DIR}}/templates/kettle-{{slug}}.yaml" \
            "$tmp/templates/kettle-{{slug}}.yaml" || true
