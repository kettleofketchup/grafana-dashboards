# kettle-grafana-dashboards

[![ci](https://github.com/kettleofketchup/grafana-dashboards/workflows/ci/badge.svg)](https://github.com/kettleofketchup/grafana-dashboards/actions?query=workflow%3Aci)
[![pypi version](https://img.shields.io/pypi/v/kettle-grafana-dashboards.svg)](https://pypi.org/project/kettle-grafana-dashboards/)

Reusable Grafana v2 (Scenes) dashboards defined as code with the official
[grafana-foundation-sdk](https://github.com/grafana/grafana-foundation-sdk).
Dashboards are the single source of truth: regenerate the JSON whenever
the library updates.

Targets Grafana's `dashboard.grafana.app/v2beta1` schema — the format
Grafana Cloud's in-app JSON editor now requires for save/apply. v1
dashboards still load but can't be edited in-place.

## Consuming the dashboards

Three ways to get the JSON, depending on your stack:

| Consumer | How |
|---|---|
| Python project | `pip install kettle-grafana-dashboards && kgd generate` |
| Anything else | Download `*.json` from a [GitHub Release](https://github.com/kettleofketchup/grafana-dashboards/releases) |
| Kubernetes | Mount the JSON via Grafana's sidecar / ConfigMap pattern |

### Python

```bash
pip install kettle-grafana-dashboards
kgd list                              # see registered dashboards
kgd generate --output ./dashboards    # render all to ./dashboards/*.json
kgd generate -d service-health -o .   # render one
```

Upload each `*.json` via the Grafana API or paste into the in-app editor.

### Release artifacts

Every tagged release attaches generated JSON to the GitHub Release page.
Pin a version, fetch by URL, drop into your provisioning of choice:

```bash
curl -LO https://github.com/kettleofketchup/grafana-dashboards/releases/download/<TAG>/kettle-service-health.json
```

## Authoring a new dashboard

1. Copy `src/grafana_dashboards/dashboards/service_health.py` to
   `src/grafana_dashboards/dashboards/<your_slug>.py`.
2. Edit the panels, variables, layout. Each module returns a
   `DashboardSpec(uid, builder)` from a `@register("<slug>")`-decorated
   `build()` function.
3. Add the module to `_AUTOLOAD` in
   `src/grafana_dashboards/dashboards/__init__.py`.
4. `uv run kgd generate` — the structural validator catches missing
   layout references, duplicate panel IDs, undeclared `$variable` refs,
   and unbalanced parens in queries before they hit Grafana.

## Releasing

Tag a commit and push the tag. The `release` workflow:

1. Builds the wheel + sdist.
2. Runs `kgd generate` to produce the JSON artifacts.
3. Creates a GitHub Release with the wheel, sdist, and `*.json` attached.
4. Publishes the wheel to PyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
   (configure once in your PyPI project settings).
