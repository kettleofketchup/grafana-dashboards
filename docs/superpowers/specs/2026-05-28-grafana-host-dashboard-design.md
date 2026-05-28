# Workstation Host Monitoring Dashboard — Design

- **Date:** 2026-05-28
- **Owner:** kettle
- **Status:** Approved (brainstorm); ready for implementation plan
- **Repos affected:** `~/git_repos/grafana-dashboards`, `~/KettleCluster/home/apps/{grafana-dashboards,kube-prometheus-stack,loki}`

## Goal

Stand up a Grafana dashboard that answers three questions about this Omarchy workstation:

1. **What is my system doing right now?** — CPU, memory, GPU, I/O, network at a glance.
2. **Why does it stutter?** — surface kernel pressure (PSI) and correlate spikes with the processes/units active during the spike window.
3. **Which apps error?** — error-rate-by-unit panel + tailed log view tied to the selected time window.

The dashboard is rendered from typed Python (`grafana-foundation-sdk`, V2/Scenes schema) and provisioned via the existing kube-prometheus-stack sidecar loader, deployed through ArgoCD.

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
│  • cli: `grafana-dashboards render host-omarchy --out PATH`       │
│  • just alloy::* (host setup), dash::*, cluster::*                │
│  • uses grafana-foundation-sdk (dashboard_v2alpha1 module)        │
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

V2 schema (`dashboard_v2alpha1`). Default time `now-1h`, refresh `30s`.

**Template variables:** `$DS_PROM`, `$DS_LOKI` (datasource pickers), `$host` (multi-value, default `kettle-omarchy`), `$window` (chip: 1m/5m/15m/1h/6h).

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

`~/git_repos/grafana-dashboards/`:

```
justfile                            # mod alloy / dash / cluster + dev imports
just/{dev,alloy,dash,cluster}.just
pyproject.toml                      # adds grafana-foundation-sdk, click, rich
uv.lock
alloy/
  config.alloy.j2                   # Jinja-rendered to /etc/alloy/config.alloy
  env.example
  alloy.service.example
  nvidia_gpu_exporter.service.example
src/grafana_dashboards/
  cli.py                            # Click: render <name> [--out PATH]; --all
  datasources.py                    # DS_PROM = "${DS_PROM}", DS_LOKI = "${DS_LOKI}"
  labels.py                         # Shared label selectors
  variables.py                      # Template variable builders
  rows.py                           # Compose panels into V2 grid layouts
  panels/
    _common.py                      # thresholds, units, legend defaults
    stat.py                         # stat_psi(), stat_loadavg(), stat_temp(), stat_stutter_count()
    timeseries.py                   # ts_psi_all(), ts_cpu_per_core(), ts_cpu_freq(),
                                    # ts_mem_breakdown(), ts_gpu_util(), ts_gpu_mem(),
                                    # ts_gpu_temp_power(), ts_disk_*, ts_net_*
    tables.py                       # top_cgroup_cpu_table(), top_cgroup_mem_table(),
                                    # top_error_units_table()
    logs.py                         # logs_panel(), error_rate_timeseries()
  dashboards/
    host_omarchy.py                 # exposes DASHBOARD_UID, DASHBOARD_TITLE, build()
  recording_rules/
    host_omarchy.yaml               # PrometheusRule body emitted alongside the dashboard
tests/
  test_render.py
  test_panels.py
```

**Foundation-SDK conventions used:**
- V2 module: `from grafana_foundation_sdk.builders import dashboard_v2alpha1 as dashboard`.
- Panel builder returns `(name: str, builder)`; the dashboard module collects them into the `elements` dict and the `layout.items` array (V2 split layout).
- Datasource refs passed as plain dicts (`{"type": "prometheus", "uid": "${DS_PROM}"}`) per the SDK gotcha.
- Encoder pinned to `JSONEncoder(sort_keys=True, indent=2)`; JSON file written with trailing newline for stable git diffs.
- SDK version pinned with the PyPI epoch form (`grafana-foundation-sdk==<EPOCH>!<BASE>`), resolved at implementation time against the cluster's Grafana version.

**Dashboard discovery:** `cli.py` enumerates `dashboards/*.py` modules; each must expose `DASHBOARD_UID`, `DASHBOARD_TITLE`, `build()`. Adding a new dashboard later = drop a file, run `just dash::render-all`.

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

**`just/dash.just`:**

| Recipe | Action |
|---|---|
| `dash::render NAME` | `uv run grafana-dashboards render {{NAME}}` — writes `chart/dashboards/{{NAME}}.json`, ensures `chart/templates/{{NAME}}.yaml` (ConfigMap wrapper) exists, and writes `chart/templates/{{NAME}}-rules.yaml` if the dashboard declares recording rules. Default `--out` is `~/KettleCluster/home/apps/grafana-dashboards/chart/`. |
| `dash::render-all` | Renders every discovered dashboard. |
| `dash::validate NAME` | Local validator (paren balance, regex escaping, schema sanity); used by pre-commit. |
| `dash::diff NAME` | Render to tempfile, diff vs committed JSON. |

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

- **Render round-trip** (`tests/test_render.py`): each dashboard renders to valid JSON; every element key appears exactly once in `layout.items`; every panel has a non-empty title; every PromQL/LogQL `expr` has balanced parens and balanced backticks; datasource refs use `${DS_PROM}`/`${DS_LOKI}` literals (no hard-coded UIDs).
- **Panel-builder unit tests** (`tests/test_panels.py`): each builder produces the expected datasource type, title shape, and query skeleton. Sanity-only — the SDK's typing carries most of the weight.
- **Pre-commit hook**: ruff + ty + `pytest -q` + `dash::validate` over every rendered JSON.

## Risks and open items

- **Loki service name and port.** The current Loki Helm values are an upstream-defaults stub; the actual Service name (`loki-gateway` vs `loki`) and port depend on whether the chart deploys in SingleBinary or simple-scalable mode. Resolved during implementation by `kubectl -n <ns> get svc`.
- **SealedSecret vs SOPS.** Chart-side secret pattern verified at implementation time; the spec assumes whichever pattern other apps in the cluster already use.
- **Foundation-SDK version.** Pinned to the latest `EPOCH!BASE` that matches the cluster's running Grafana (likely 10.x family per PyPI inventory on 2026-05-28). Resolved at implementation time.
- **GPU exporter packaging.** `nvidia_gpu_exporter` is on AUR; if the package is missing or broken, fall back to running the upstream binary release directly under systemd. Implementation plan should include this fallback.
- **AUR package staleness.** AUR packages can go unmaintained. Implementation plan should record the upstream binary download URLs as a fallback for both Alloy and the GPU exporter.

## Out of scope (will revisit)

- Grafana alerting on PSI / error-rate thresholds.
- Hyprland-specific instrumentation beyond cgroup-level (no viable frame-timing source today).
- Pushing dashboards via the Grafana HTTP API for fast local iteration.
- A second dashboard. The panel-builder library is built so the next one is cheap; it just isn't this PR.
