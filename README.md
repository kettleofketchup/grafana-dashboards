# kettle-grafana-dashboards

[![ci](https://github.com/kettleofketchup/grafana-dashboards/workflows/ci/badge.svg)](https://github.com/kettleofketchup/grafana-dashboards/actions?query=workflow%3Aci)

Reusable Grafana v2 (Scenes) dashboards defined as code with the official
[grafana-foundation-sdk](https://github.com/grafana/grafana-foundation-sdk).
Dashboards are the single source of truth: regenerate the JSON whenever
the library updates.

Targets Grafana's `dashboard.grafana.app/v2beta1` schema — the format
Grafana Cloud's in-app JSON editor now requires for save/apply. v1
dashboards still load but can't be edited in-place.

## Consuming the dashboards

Every tagged release attaches the generated JSON to the GitHub Release
page. Pin a version, fetch by URL, drop into your provisioning of
choice:

```bash
curl -LO https://github.com/kettleofketchup/grafana-dashboards/releases/download/<TAG>/kettle-service-health.json
```

Upload via the Grafana API, paste into the in-app JSON editor, or mount
as a ConfigMap with Grafana's sidecar provisioner.

> **PyPI release is parked.** The package depends on grafana-foundation-sdk's
> main branch (v2beta1 builders aren't in a PyPI release yet), and PyPI
> rejects distributions with direct-URL deps. Once upstream cuts a PyPI
> release containing v2 builders, we relax the dep and start publishing
> the wheel. Until then, regenerate locally from a checkout:
>
> ```bash
> git clone https://github.com/kettleofketchup/grafana-dashboards
> cd grafana-dashboards
> uv sync
> uv run kgd list                              # see registered dashboards
> uv run kgd generate --output ./dashboards    # render all
> uv run kgd generate -d service-health -o .   # render one
> ```

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

Tag a commit with `v*` and push the tag. The `release` workflow:

1. Runs the test suite.
2. Runs `kgd generate` to produce the JSON artifacts.
3. Creates a GitHub Release with `*.json` attached and changelog notes.
