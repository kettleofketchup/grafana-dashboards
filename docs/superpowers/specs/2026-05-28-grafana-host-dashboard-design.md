# Workstation Host Monitoring Dashboard — Design

- **Date:** 2026-05-28
- **Owner:** kettle
- **Status:** Approved (brainstorm); ready for implementation plan
- **Repos affected:** `~/git_repos/grafana-dashboards`, `~/KettleCluster/home/apps/{grafana-dashboards,kube-prometheus-stack,loki}`
- **Builds on existing scaffold:** Commits `a306284` (dashboard generator scaffold) and `297078d` (CI/CD fixes) are in place; this design adds the host-monitoring use case on top of them.

## Goal

Stand up a Grafana dashboard that answers three questions about this Omarchy workstation:

1. **What is my system doing right now?** — CPU, memory, GPU, I/O, network at a glance.
2. **Why does it stutter?** — surface kernel pressure (PSI) and correlate spikes with the processes/units active during the spike window.
3. **Which apps error?** — error-rate-by-unit panel + tailed log view tied to the selected time window.

The dashboard is rendered from typed Python (`grafana-foundation-sdk`, **v2beta1** schema — the GA April 2026 cut of the Scenes-based dashboard model) and provisioned via the existing kube-prometheus-stack sidecar loader, deployed through ArgoCD.

## Non-goals

- Per-PID metrics (cardinality blast, cgroup-level is the right granularity).
- Hyprland IPC frame-timing metrics (not exposed usefully by Hyprland today; revisit if a viable signal appears).
- Grafana alerting (follow-up; this spec ships the dashboard, not alert rules).
- Pushing dashboards via Grafana HTTP API (we chose GitOps; API push not implemented in this phase).
- Local Grafana on the workstation (single source of truth = cluster Grafana).

## Architecture

```
┌──────────────────────── Omarchy workstation ──────────────────────┐
│                                                                   │
│  /proc/pressure/*  ┐                                              │
│  /proc, /sys       ├─► Grafana Alloy (systemd)  ─────┐            │
│  systemd cgroups   │   • prometheus.exporter.unix    │            │
│  journald          ┘   • prometheus.scrape (nvidia)  │            │
│                        • loki.source.journal         │            │
│  nvidia_gpu_exporter on 127.0.0.1:9835 ──────────────┘            │
└───────────────────────────────────────────────────┬───────────────┘
                                                   │ HTTPS + basic-auth
                                                   ▼
┌──────────────────────── KettleCluster ────────────────────────────┐
│                                                                   │
│  Traefik (TLS) ─► prometheus-ingest.home.kettle.sh                │
│                   /api/v1/write  ─► kube-prometheus-stack (RW)    │
│                ─► loki-ingest.home.kettle.sh                      │
│                   /loki/api/v1/push  ─► loki                      │
│                                                                   │
│  grafana.home.kettle.sh                                           │
│    "Workstation" folder                                           │
│      "kettle-omarchy" dashboard                                   │
│    ▲ ConfigMap loaded by sidecar (label grafana_dashboard=1)      │
│    │                                                              │
│    home/apps/grafana-dashboards/chart/dashboards/host-omarchy.json│
│    ▲                                                              │
│    │ git commit → ArgoCD reconcile                                │
└────┼──────────────────────────────────────────────────────────────┘
     │
┌────┼────────────────── ~/git_repos/grafana-dashboards ────────────┐
│  uv-managed Python package + just interface                       │
│  • src/grafana_dashboards/panels/* (reusable builders)            │
│  • src/grafana_dashboards/dashboards/host_omarchy.py (composition)│
│  • cli: `kgd generate -o DIR -d host-omarchy`                     │
│  • just alloy::* (host setup), dash::*, cluster::*                │
│  • grafana-foundation-sdk (dashboardv2beta1) pinned via git+URL   │
└───────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Host agent — Grafana Alloy on Omarchy

**Install.** `grafana-alloy` and `nvidia_gpu_exporter` from AUR (`yay -S --needed ...`). Both run as system-level systemd services. Alloy ships its own unit; `nvidia_gpu_exporter` listens on `127.0.0.1:9835`.

**Config layout.**
- `/etc/alloy/config.alloy` — rendered from `alloy/config.alloy.j2` in the repo, parameterised on `HOSTNAME` and the cluster ingest URLs.
- `/etc/alloy/env` — `0600 alloy:alloy`, contains `PROM_USER`, `PROM_PASS`, `LOKI_USER`, `LOKI_PASS`. Never committed with real values; `alloy/env.example` is the template.

**Collected signals.**

| Source | Alloy component | Yields |
|---|---|---|
| `/proc`, `/sys` | `prometheus.exporter.unix` with PSI + hwmon + filesystem collectors enabled | CPU per-core (P/E labelled via topology), memory, disk, network, temps, **PSI** |
| systemd cgroups | `prometheus.exporter.unix` (`systemd` collector) and/or `prometheus.exporter.cadvisor` | Per-unit CPU and memory (Hyprland.service, slack.service, chromium-*.scope, …) |
| NVIDIA GPU | `prometheus.scrape` against `127.0.0.1:9835` | GPU util %, VRAM, temp, power, clock |
| journald | `loki.source.journal` (priority ≤ info) | All host logs labelled `unit`, `priority`, `boot_id` |

**External labels** added to every series and log line:

```
host="kettle-omarchy"   role="workstation"   distro="omarchy"   gpu="nvidia-rtx4090"
```

**Cardinality controls.**
- Drop uninteresting filesystems (`tmpfs`, `overlay`, container mounts) at relabel time.
- No per-process collector; cgroup granularity only.
- Journald: debug priority dropped locally before push.

**Outbound.**
- Metrics → `prometheus.remote_write` to `https://prometheus-ingest.home.kettle.sh/api/v1/write`.
- Logs → `loki.write` to `https://loki-ingest.home.kettle.sh/loki/api/v1/push`.

**Footprint.** Alloy ~50–100 MB RSS, ~1–2% of a single core on i9-13900K. `nvidia_gpu_exporter` ~10 MB RSS.

### 2. Cluster-side enablement (one-time, idempotent)

- `kube-prometheus-stack`: enable `prometheus.prometheusSpec.enableRemoteWriteReceiver: true`.
- New Traefik `IngressRoute` for `prometheus-ingest.home.kettle.sh` → `kube-prometheus-stack-prometheus.monitoring.svc:9090`.
- New Traefik `IngressRoute` for `loki-ingest.home.kettle.sh` → `loki-gateway.<ns>.svc:80` (or `loki.<ns>.svc:3100` depending on the chart's mode; resolved at implementation time).
- New Traefik `Middleware` with basic-auth, secret rendered through whatever pattern that chart already uses for secrets (SOPS or SealedSecrets — verified during implementation).
- These routes do **not** carry the `authentik-forwardauth` middleware. They are machine-only endpoints.

### 3. Dashboard — "Workstation / kettle-omarchy"

Registered slug `host-omarchy`; dashboard UID `kettle-host-omarchy`; output file `kettle-host-omarchy.json`. v2beta1 schema (`dashboardv2beta1` SDK module). Default time `now-1h`, refresh `30s`. Lives in a "Workstation" Grafana folder (set via the ConfigMap annotation `grafana_folder: "Workstation"`, mirroring the existing `cluster-overview.yaml` pattern).

**Template variables** (declared on the dashboard via `v2.DatasourceVariable` / `v2.QueryVariable`, referenced from panels by name): `$ds_prom`, `$ds_loki` (datasource pickers), `$host` (multi-value query variable, default `kettle-omarchy`), `$window` (custom variable: 1m/5m/15m/1h/6h).

**Rows.**

| # | Row | Panels |
|---|---|---|
| 1 | Right-now indicators | PSI CPU/Mem/IO stats (1m), load avg, max-core temp, stutter-events-in-window count |
| 2 | Pressure over time | Single timeseries with all three PSI lines (the headline graph) |
| 3 | CPU detail | Per-core util (repeat-by-core), CPU frequency, top units by CPU over `$window` (table) |
| 4 | Memory detail | Used/cached/free/swap timeseries, top units by RSS over `$window` (table) |
| 5 | GPU (RTX 4090) | Util %, VRAM, temp + power, clock |
| 6 | I/O & disk | IOPS per device, throughput per device, I/O wait |
| 7 | Network | Bytes/s per interface, errors + drops |
| 8 | Errors & logs | Error rate by unit (Loki), top error-emitting units, live error tail panel |

The page is laid out so that a PSI spike (row 2) sits directly above the cgroup top-talker tables (rows 3–4) and the error rate (row 8) — when you click-drag a spike to zoom, every panel below repaints to that window via `$__from` / `$__to`, exposing "what was burning CPU AND what was erroring during the stutter."

**Recording rules** (delivered as a `PrometheusRule` CRD at `home/apps/grafana-dashboards/chart/templates/host-omarchy-rules.yaml`):
- `kettle_host:psi_cpu_stutter_events:1m` — count of minutes with PSI CPU > 30, drives the stutter-events stat panel.
- `kettle_host:cgroup_cpu_top10:5m` — pre-aggregates top-10 cgroups by CPU over 5-minute windows so the table panel does not re-aggregate at query time.

**Output artifacts per dashboard.** Three files in the cluster repo:

| File | Source | Owner |
|---|---|---|
| `chart/dashboards/host-omarchy.json` | Generated by `dash::render host-omarchy` | Overwritten on every render |
| `chart/templates/host-omarchy.yaml` | ConfigMap wrapping the JSON (mirrors existing `cluster-overview.yaml` shape) | Generated by `dash::render` if missing; idempotent once present |
| `chart/templates/host-omarchy-rules.yaml` | `PrometheusRule` CRD with the two recording rules | Generated alongside the JSON; overwritten on every render |

### 4. Dashboard generator repo

`~/git_repos/grafana-dashboards/` — **existing scaffold extended.** Distribution name is `kettle-grafana-dashboards`; Python 3.11+; SDK is pinned via `git+https://github.com/grafana/grafana-foundation-sdk.git@<commit>#subdirectory=python` because v2beta1 builders are on the SDK's `main` branch and PyPI wheels still lag.

```
justfile                            # NEW — mod alloy / dash / cluster + dev imports
just/{dev,alloy,dash,cluster}.just  # NEW
pyproject.toml                      # EXISTS — dependency-groups + grafana-foundation-sdk
uv.lock                             # EXISTS
alloy/                              # NEW
  config.alloy.j2                   # Jinja-rendered to /etc/alloy/config.alloy
  env.example
  alloy.service.example
  nvidia_gpu_exporter.service.example
src/grafana_dashboards/
  _internal/
    cli.py                          # EXISTS — argparse `kgd` CLI (list / generate -o -d --no-validate)
    envelope.py                     # EXISTS — wrap_v2(spec, uid) → CRD-shaped dict
    validate.py                     # EXISTS — v2beta1 structural validator
  dashboards/
    __init__.py                     # EXISTS — DashboardSpec, @register(slug), _AUTOLOAD
    service_health.py               # EXISTS — starter; mirror its v2 patterns
    host_omarchy.py                 # NEW — registered as @register("host-omarchy"), uid "kettle-host-omarchy"
  panels/                           # NEW — reusable v2 builders
    _common.py                      # thresholds, units, legend defaults, _PromQuery / _LokiQuery shims
    stat.py                         # stat_psi(), stat_loadavg(), stat_temp(), stat_stutter_count()
    timeseries.py                   # ts_psi_all(), ts_cpu_per_core(), ts_cpu_freq(),
                                    # ts_mem_breakdown(), ts_gpu_util(), ts_gpu_mem(),
                                    # ts_gpu_temp_power(), ts_disk_*, ts_net_*
    tables.py                       # top_cgroup_cpu_table(), top_cgroup_mem_table(),
                                    # top_error_units_table()
    logs.py                         # logs_panel(), error_rate_timeseries()
  rows.py                           # NEW — compose panels into v2 Rows/Grid layout helpers
  variables.py                      # NEW — $ds_prom (DatasourceVariable), $host, $window
  recording_rules/
    host_omarchy.yaml               # NEW — PrometheusRule body emitted alongside the dashboard
tests/
  test_render.py                    # NEW — host-omarchy round-trip + validator-runs-clean
  test_panels.py                    # NEW — per-builder shape checks
```

**Foundation-SDK / scaffold conventions used (anchored on `service_health.py`):**
- V2 module names are one word: `from grafana_foundation_sdk.builders import dashboardv2beta1 as v2` and `from grafana_foundation_sdk.models.dashboardv2beta1 import ...`.
- Each dashboard module exposes `build() -> DashboardSpec` (a `NamedTuple(uid, builder)`) decorated `@register("<slug>")`. The module path must also be added to `dashboards/__init__.py:_AUTOLOAD` so registry membership stays a property of source, not import order.
- Datasource references go through a `DatasourceVariable` declared on the dashboard (`v2.DatasourceVariable("ds_prom")`, `"ds_loki"`); panels reference it by name via `Dashboardv2beta1DataQueryKindDatasource(name="$ds_prom")`. v2 has no `__inputs` substitution block — the runtime variable replaces it.
- PromQL/LogQL queries are wrapped in v2's `DataQueryKind(group=..., version="v0", datasource=..., spec={"expr":..., "editorMode":"code", "refId":...})` envelope — the SDK's per-datasource builders still emit v1 query shapes, and v2 rejects them otherwise. Use the `_PromQuery` / `_LokiQuery` shims from `panels/_common.py` (same pattern as `service_health.py`).
- Layout: `v2.Rows().row(v2.Row().title(...).collapse(...).layout(v2.Grid().item(v2.GridItem().name(N).x(...).y(...).width(...).height(...))...))`. Elements are registered on the dashboard via `.element(name, panel)` and referenced by `name` from grid items.

**Generator output.** `kgd generate -o DIR` writes one file per registered dashboard, named `<uid>.json`, content shape:

```json
{
  "apiVersion": "dashboard.grafana.app/v2beta1",
  "kind": "Dashboard",
  "metadata": {"name": "<uid>"},
  "spec": { "title": "...", "layout": {...}, "elements": {...}, ... }
}
```

That's the CRD-envelope shape. For the **ConfigMap sidecar provisioning path** used by your cluster, `just dash::render` strips the envelope down to `.spec` before placing the JSON in the chart (see open item in Risks — this stripping behaviour is the design decision pending sidecar/Grafana-version verification).

**Validation.** Already implemented in `_internal/validate.py` — checks envelope shape, required spec fields, layout↔element name resolution, panel-id uniqueness, balanced parens/braces/brackets in `expr` fields, and that `${var}` references resolve to declared variables or known Grafana built-ins. `kgd generate` runs the validator by default; `--no-validate` skips it.

**Dashboard discovery.** Decorator + explicit `_AUTOLOAD` tuple in `dashboards/__init__.py`. Adding a new dashboard: (1) drop `dashboards/<slug>.py` with a `@register("<slug>")`-decorated `build()`; (2) append the module path to `_AUTOLOAD`; (3) run `just dash::render-all`.

### 5. `just` interface

Root `justfile`:

```just
set quiet
set dotenv-load

import 'just/dev.just'

mod alloy   'just/alloy.just'
mod dash    'just/dash.just'
mod cluster 'just/cluster.just'

default:
    just --list --list-submodules
```

**`just/dev.just`** (imported): `dev` (uv sync + pre-commit install), `lint` (ruff + ty), `test` (pytest), `clean`.

**`just/alloy.just`** (the headline ask):

| Recipe | Action |
|---|---|
| `alloy::install` | Preflight (Arch?, NVIDIA?), `yay -S --needed grafana-alloy nvidia_gpu_exporter`, ensure `alloy` user/group, lay down `/etc/alloy/`. |
| `alloy::configure HOSTNAME=$(hostname)` | Render `alloy/config.alloy.j2` → `/etc/alloy/config.alloy`; render `/etc/alloy/env` from `env.example` if missing; `chown alloy:alloy /etc/alloy/env && chmod 0600`. |
| `alloy::enable` | `systemctl enable --now alloy nvidia_gpu_exporter`; report unit health. |
| `alloy::reload` | `systemctl reload alloy`; restart fallback. |
| `alloy::status` | Unit states + tail + Alloy `/-/healthy` HTTP probe. |
| `alloy::logs N=200` | `journalctl -u alloy -n {{N}} -f --no-pager`. |
| `alloy::test-ingest` | Push synthetic `kettle_smoketest 1` to the ingest endpoint, then query it back from Grafana to verify the round trip. Exit non-zero on failure. |
| `alloy::uninstall` | `[confirm]`-gated removal of units, `/etc/alloy/`, and packages. |

**`just/dash.just`** (thin wrappers around the existing `kgd` CLI):

| Recipe | Action |
|---|---|
| `dash::render SLUG` | Runs `uv run kgd generate -o $(mktemp -d) -d {{SLUG}}` into a scratch dir, then for each emitted `<uid>.json`: (a) strips the v2 envelope to `.spec` and writes it as `~/KettleCluster/home/apps/grafana-dashboards/chart/dashboards/<uid>.json`; (b) ensures `chart/templates/<uid>.yaml` (ConfigMap wrapper) exists, generating it from a template if missing; (c) if the dashboard module exports `RECORDING_RULES`, writes `chart/templates/<uid>-rules.yaml` as a `PrometheusRule` CRD. Idempotent. |
| `dash::render-all` | Same as above without the `-d` filter. |
| `dash::validate SLUG` | Runs `kgd generate -o /tmp/... -d {{SLUG}}` — the structural validator in `_internal/validate.py` runs by default; exit code drives pre-commit. |
| `dash::diff SLUG` | Renders to a tempfile and diffs against the committed JSON in the cluster repo; no writes. |

**`just/cluster.just`** (one-time enablement):

| Recipe | Action |
|---|---|
| `cluster::secret USER PASS_FILE` | Generate htpasswd; write SealedSecret/SOPS-encrypted Secret matching the kube-prometheus-stack chart's existing pattern. |
| `cluster::deploy-ingest` | Write the two `IngressRoute`s + `Middleware`; enable `enableRemoteWriteReceiver` in `kube-prometheus-stack` values; print `git diff` of cluster repo so the user commits and lets ArgoCD reconcile (per project rule: no `kubectl` patches). |

## End-to-end dev loop

```sh
# First-time setup
cd ~/git_repos/grafana-dashboards
just dev                                       # uv sync + pre-commit
just cluster::secret kettle-omarchy pass.txt   # K8s secret YAML
just cluster::deploy-ingest                    # writes IngressRoutes, prints diff
# commit + push KettleCluster; ArgoCD syncs.

just alloy::install
just alloy::configure
just alloy::enable
just alloy::test-ingest                        # round-trip sanity

# Iterating on the dashboard
just dash::render host-omarchy
just dash::validate host-omarchy
git -C ~/KettleCluster add ... && git -C ~/KettleCluster commit -m "..."
# ArgoCD picks up the new ConfigMap; sidecar loads it; refresh Grafana.
```

## Testing

- **Render round-trip** (`tests/test_render.py`): renders `host-omarchy` end-to-end and asserts `_internal/validate.py:validate_v2` returns an empty issues list. The existing validator covers envelope shape, required spec fields, layout↔element name resolution, panel-id uniqueness, expr paren/brace/bracket balance, and variable-reference resolution — so the test is mostly "does the SDK produce something the validator accepts."
- **Panel-builder unit tests** (`tests/test_panels.py`): each builder produces a panel with the expected datasource variable reference (`$ds_prom` / `$ds_loki`), title, query envelope shape, and visualization type. Sanity-only — the SDK's typing carries most of the weight.
- **Pre-commit hook**: ruff + ty + `pytest -q` + `dash::validate-all`.

## Risks and open items

- **Sidecar consumes v2beta1.** The scaffold emits the CRD-shaped envelope (`apiVersion: dashboard.grafana.app/v2beta1`), which natively targets the grafana-operator. Your cluster provisions via the kube-prometheus-stack ConfigMap sidecar, not an operator. The design's chosen path is: **`just dash::render` strips the envelope down to `.spec` before placing the JSON in the chart.** This depends on the Grafana version in `kube-prometheus-stack` being recent enough to parse a v2beta1-shaped `.spec` document directly from a file. Implementation plan must (a) verify the running Grafana version on `grafana.home.kettle.sh`, (b) render one dashboard, drop it in via the sidecar, and confirm it loads. Fallback if it doesn't: install grafana-operator and apply the envelope-wrapped JSON as a CRD instead of via ConfigMap.
- **Loki service name and port.** The current Loki Helm values are an upstream-defaults stub; the actual Service name (`loki-gateway` vs `loki`) and port depend on whether the chart deploys in SingleBinary or simple-scalable mode. Resolved during implementation by `kubectl -n <ns> get svc`.
- **SealedSecret vs SOPS.** Chart-side secret pattern verified at implementation time; the spec assumes whichever pattern other apps in the cluster already use.
- **Foundation-SDK pin.** Already pinned via `git+URL` at commit `a8c311b58` for v2beta1 builders. Implementation may need to bump if upstream gains useful additions; PyPI wheels remain the longer-term target once they track v2 cleanly.
- **GPU exporter packaging.** `nvidia_gpu_exporter` is on AUR; if the package is missing or broken, fall back to running the upstream binary release directly under systemd. Implementation plan should include this fallback.
- **AUR package staleness.** AUR packages can go unmaintained. Implementation plan should record the upstream binary download URLs as a fallback for both Alloy and the GPU exporter.

## Out of scope (will revisit)

- Grafana alerting on PSI / error-rate thresholds.
- Hyprland-specific instrumentation beyond cgroup-level (no viable frame-timing source today).
- Pushing dashboards via the Grafana HTTP API for fast local iteration.
- A second dashboard. The panel-builder library is built so the next one is cheap; it just isn't this PR.
