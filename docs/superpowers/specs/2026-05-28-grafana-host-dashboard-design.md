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

| Source | Alloy component | Yields | Key metric names |
|---|---|---|---|
| `/proc`, `/sys` | `prometheus.exporter.unix` with **pinned collector list** (see below) | CPU per-core (P/E labelled via topology), memory, disk, network, temps, **PSI**, run-queue latency, IRQs/softirqs, per-device IO latency | `node_cpu_seconds_total`, `node_memory_*`, `node_filesystem_*`, `node_disk_*`, `node_network_*`, `node_hwmon_temp_celsius`, `node_cpu_frequency_hertz`, `node_pressure_{cpu,memory,io}_waiting_seconds_total`, `node_pressure_{cpu,memory,io}_stalled_seconds_total`, `node_schedstat_running_seconds_total`, `node_schedstat_waiting_seconds_total`, `node_interrupts_total`, `node_softirqs_total`, `node_disk_io_time_weighted_seconds_total` |
| systemd cgroups (v2) | `prometheus.exporter.cadvisor` — **committed**; not `systemd` collector, which only emits unit state | Per-unit CPU and memory (Hyprland.service, slack.service, etc.) | `container_cpu_usage_seconds_total`, `container_memory_rss`, `container_memory_working_set_bytes` |
| NVIDIA GPU | `prometheus.scrape` against `127.0.0.1:9835` | GPU util %, VRAM, temp, power, clock | `nvidia_smi_utilization_gpu_ratio`, `nvidia_smi_memory_used_bytes`, `nvidia_smi_temperature_gpu`, `nvidia_smi_power_draw_watts`, `nvidia_smi_clocks_current_graphics_clock_hz` |
| journald | `loki.source.journal` (priority ≤ info) | All host logs labelled `unit`, `priority`; `boot_id` as **structured metadata** (Loki ≥3) so reboots don't churn streams |  |

> **cAdvisor on a non-K8s host:** runs under systemd with `--docker_only=false`, `--store_container_labels=false`, and cgroup-v2 access via `--containerd=` left empty + `/sys/fs/cgroup` mounted. Implementation plan must verify the Omarchy host is cgroupv2 (`stat -fc %T /sys/fs/cgroup` → `cgroup2fs`).

**Pinned `prometheus.exporter.unix` collector list** (otherwise the CPU-frequency, temp, schedstat, interrupts, and IO-latency panels silently render empty):

```
cpu, meminfo, loadavg, filesystem, diskstats, netdev, netstat, sockstat,
time, uname, vmstat, hwmon, cpufreq, pressure, schedstat, interrupts, softirqs
```

**External labels** added to every series and log line. OTEL Resource semantic-convention *values* — but in their Prometheus-legal **underscore form** (Prometheus label syntax is `[a-zA-Z_][a-zA-Z0-9_]*`; dots are illegal and silently translated to underscores by the standard OTEL→Prometheus mapping). Future OTLP-emitting apps on this host will land on the same labels via the same translation:

```
host="kettle-omarchy"                       # short alias, used in dashboard filters
host_name="kettle-omarchy"                  # OTEL host.name (underscore form)
host_id="<sha256(/etc/machine-id)[:16]>"    # OTEL host.id — hashed; see Risks
host_arch="amd64"                           # OTEL host.arch
os_type="linux"                             # OTEL os.type
os_description="<PRETTY_NAME from /etc/os-release>"   # baked at configure time
role="workstation"
distro="omarchy"
gpu="nvidia-rtx4090"
```

**Hand-stamp mechanism (one-time setup).** `alloy/config.alloy.j2` is rendered by `just alloy::configure`, which:
1. Reads `/etc/machine-id`, hashes it (`sha256(...)[:16]`), and templates that into `host_id`.
2. Reads `uname -m` for `host_arch`, hardcodes `os_type="linux"`, hardcodes the short aliases.
3. Sources `/etc/os-release` and templates `${PRETTY_NAME}` into `os_description`.
4. Writes the rendered `discovery.relabel` block into `/etc/alloy/config.alloy`.

Alloy then stamps every metric and log line with those labels at startup. Truly one-time — only a distro upgrade (changes `PRETTY_NAME`) or OS reinstall (changes machine-id) requires a `just alloy::configure && systemctl reload alloy` to re-stamp.

**Cardinality controls.**
- Drop uninteresting filesystems (`tmpfs`, `overlay`, container mounts) at relabel time.
- No per-process collector; cgroup granularity only.
- **Browser cgroup collapse.** Chromium, Firefox, Electron, and Steam/Proton spawn per-tab/per-process `.scope` units; left alone they explode the `unit` label. A `discovery.relabel` block collapses common patterns: `chromium-\d+\.scope` → `chromium`; `firefox-.+\.scope` → `firefox`; `app-electron-.+\.scope` → `electron`; `app-org\.proton\..+\.scope` → `proton`. Drop `systemd-udevd` worker scopes entirely.
- Journald: debug priority dropped locally before push. `boot_id` as **structured metadata** (not an index label) so reboots don't churn streams.

**Outbound.**
- Metrics → `prometheus.remote_write` to `https://prometheus-ingest.home.kettle.sh/api/v1/write`.
- Logs → `loki.write` to `https://loki-ingest.home.kettle.sh/loki/api/v1/push`.

**Footprint.** Alloy ~50–100 MB RSS, ~1–2% of a single core on i9-13900K. `nvidia_gpu_exporter` ~10 MB RSS.

### 2. Cluster-side enablement (one-time, idempotent)

- `kube-prometheus-stack`: enable `prometheus.prometheusSpec.enableRemoteWriteReceiver: true` and `prometheus.prometheusSpec.enableFeatures: ["exemplar-storage"]` (cheap; reserves the wiring so future OTLP-emitting apps automatically gain trace-ID exemplars on row 2's PSI panel with no schema change).
- `kube-prometheus-stack`: confirm `grafana.sidecar.dashboards.folderAnnotation: grafana_folder` is set (kube-prometheus-stack default in recent versions, but verify by `helm get values` — the existing `cluster-overview.yaml` template depends on it).
- New Traefik `IngressRoute` for `prometheus-ingest.home.kettle.sh` → `kube-prometheus-stack-prometheus.monitoring.svc:9090`.
- New Traefik `IngressRoute` for `loki-ingest.home.kettle.sh` → `loki-gateway.<ns>.svc:80` (or `loki.<ns>.svc:3100` depending on the chart's mode; resolved at implementation time).
- New Traefik `Middleware`s on both ingest routes:
    1. `basic-auth` — secret rendered through whatever pattern the chart uses for secrets (SOPS or SealedSecrets — verified during implementation).
    2. `rate-limit` — `RateLimit` middleware capping the route at e.g. `average: 5000` / `burst: 10000` req/s. Defense in depth against a runaway agent or credential leak; bounds ingest blast radius.
- These routes do **not** carry the `authentik-forwardauth` middleware. They are machine-only endpoints.
- **External-label enforcement on the Prometheus receiver side.** The basic-auth credential grants writes to *any* series name, including ability to overwrite cluster-side series (`up{job="...."}`, etc.). Add a `writeRelabelConfigs` block on the Prometheus remote-write receiver that enforces `host_name=~"kettle-.*"` AND `role="workstation"` (drops anything whose external labels don't match the expected workstation tenant). Also allowlist `trace_id` as a recognised exemplar label so the reserved exemplar pipeline lights up when OTLP apps appear. Rendered as part of the `cluster::deploy-ingest` recipe.
- **Loki datasource — add `derivedFields` for trace correlation.** In the Grafana datasource provisioning (kube-prometheus-stack `grafana.additionalDataSources` or the Loki datasource ConfigMap), add a derived field matching `trace_id=(?:00-)?([a-f0-9]{32})\b` linking to the Tempo datasource. Anchors length (W3C trace IDs are exactly 32 lowercase hex chars) and accepts the optional `00-` W3C `traceparent` prefix. Does nothing today (no traces yet); harmless until later OTLP-logging apps land and auto-link to Tempo from the dashboard's log panel.

### 3. Dashboard — "Workstation / kettle-omarchy"

Registered slug `host-omarchy`; dashboard UID `kettle-host-omarchy`; output file `kettle-host-omarchy.json`. v2beta1 schema (`dashboardv2beta1` SDK module). Default time `now-1h`, refresh `30s`. Lives in a "Workstation" Grafana folder (set via the ConfigMap annotation `grafana_folder: "Workstation"`, mirroring the existing `cluster-overview.yaml` pattern).

**Template variables** (declared on the dashboard via `v2.DatasourceVariable` / `v2.QueryVariable`, referenced from panels by name): `$ds_prom`, `$ds_loki` (datasource pickers), `$host` (multi-value query variable, default `kettle-omarchy`), `$window` (custom variable: 1m/5m/15m/1h/6h).

**Rows.**

| # | Row | Panels |
|---|---|---|
| 1 | Right-now indicators | PSI CPU/Mem/IO stats (1m %), `load1`, `load5`, `load15` (three separate stats), uptime stat, max-core temp, stutter-events-in-window count |
| 2 | Pressure over time | Single timeseries with all three PSI lines as percentage — the headline graph |
| 3 | CPU detail | Per-core util (repeat-by-core, P/E labelled), CPU frequency, scheduler run-queue wait time, top units by CPU over `$window` (table) |
| 4 | Memory detail | Used/cached/free/swap timeseries, top units by RSS over `$window` (table) |
| 5 | GPU (RTX 4090) | Util %, VRAM, temp + power, clock |
| 6 | I/O & disk | IOPS per device, throughput per device, per-device IO latency p99, I/O wait |
| 7 | IRQ / kernel | `node_interrupts_total` rate by CPU + name (top-N), `node_softirqs_total` rate by type — NVIDIA driver IRQs are a known stutter cause on this hardware |
| 8 | Network | Bytes/s per interface, errors + drops |
| 9 | Errors & logs | Error rate by unit (Loki), top error-emitting units, live error tail panel (with `$ds_loki`'s `derivedFields` auto-linking `trace_id=...` to Tempo when present) |

The page is laid out so that a PSI spike (row 2) sits directly above the cgroup top-talker tables (rows 3–4) and the error rate (row 9) — when you click-drag a spike to zoom, every panel below repaints to that window via `$__from` / `$__to`, exposing "what was burning CPU AND what was erroring during the stutter." Row 7 (IRQ) sits between disk and network so a "NVIDIA IRQ storm during the spike" pattern is one scroll away.

**Recording rules** (delivered as a `PrometheusRule` CRD at `home/apps/grafana-dashboards/chart/templates/host-omarchy-rules.yaml`; naming follows Prometheus `level:metric:operation` convention with no `kettle_` prefix — the `host_name` external label already disambiguates):

```promql
# PSI CPU as a 0–1 ratio of time stalled (counter → rate, fraction).
host:psi_cpu_waiting:ratio1m =
  rate(node_pressure_cpu_waiting_seconds_total[1m])

# Stutter event: a 1m sample with PSI CPU > 30% wait. count_over_time
# with a non-bool comparison only counts truthy samples (the comparison
# drops non-matching points), giving the actual count of stutter
# minutes in the 5m window.
host:psi_cpu_stutter_events:count5m =
  count_over_time(
    (host:psi_cpu_waiting:ratio1m > 0.30)[5m:1m]
  )

# Per-(host_name, name) cgroup CPU aggregate. Topk applied at query time
# in the panel — topk inside a recording rule is unstable (label sets
# drift each evaluation, producing series gaps).
host:cgroup_cpu:sum5m =
  sum by (host_name, name) (
    rate(container_cpu_usage_seconds_total{name!=""}[5m])
  )

# Per-(host_name, name) cgroup RSS aggregate. Same topk-at-query-time
# pattern.
host:cgroup_memory_rss:sum5m =
  sum by (host_name, name) (
    avg_over_time(container_memory_rss[5m])
  )
```

**Panel expressions** referencing these:
- Row 1 PSI CPU stat: `clamp_max(host:psi_cpu_waiting:ratio1m{host_name="$host"} * 100, 100)` with thresholds at 10/30/60 for green/amber/red. The `clamp_max` is defensive — the `full` PSI series can briefly read above 1.0 due to multi-core accounting + clock skew.
- Row 1 stutter-events stat: `host:psi_cpu_stutter_events:count5m{host_name="$host"}` (range 0–5).
- Row 3 top-CPU cgroups table: `topk(10, host:cgroup_cpu:sum5m{host_name="$host"})` — topk evaluated at query time.
- Row 4 top-RSS cgroups table: `topk(10, host:cgroup_memory_rss:sum5m{host_name="$host"})`.

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
    stat.py                         # stat_psi(), stat_load1(), stat_load5(), stat_load15(),
                                    # stat_uptime(), stat_temp(), stat_stutter_count()
    timeseries.py                   # ts_psi_all(), ts_cpu_per_core(), ts_cpu_freq(),
                                    # ts_sched_runqueue(), ts_irqs(), ts_softirqs(),
                                    # ts_mem_breakdown(), ts_gpu_util(), ts_gpu_mem(),
                                    # ts_gpu_temp_power(), ts_disk_iops(),
                                    # ts_disk_throughput(), ts_disk_io_latency_p99(),
                                    # ts_net_bytes(), ts_net_errors()
    tables.py                       # top_cgroup_cpu_table(), top_cgroup_mem_table(),
                                    # top_error_units_table()
    logs.py                         # logs_panel() (with derived-field auto-link),
                                    # error_rate_timeseries()
  rows.py                           # NEW — compose panels into v2 Rows/Grid layout helpers
  variables.py                      # NEW — $ds_prom (DatasourceVariable), $host, $window
  # Recording rules live as a `RECORDING_RULES` list[dict] exported from the dashboard
  # module itself (host_omarchy.py) so panels and rules share imports and stay aligned.
  # `dash::render` reads the constant via attribute lookup and emits the PrometheusRule.
tests/
  test_render.py                    # NEW — host-omarchy round-trip + validator-runs-clean
  test_panels.py                    # NEW — per-builder shape checks
```

**Foundation-SDK / scaffold conventions used (anchored on `service_health.py`):**
- V2 module names are one word: `from grafana_foundation_sdk.builders import dashboardv2beta1 as v2` and `from grafana_foundation_sdk.models.dashboardv2beta1 import ...`.
- Each dashboard module exposes `build() -> DashboardSpec` (a `NamedTuple(uid, builder)`) decorated `@register("<slug>")`. The module path must also be added to `dashboards/__init__.py:_AUTOLOAD` so registry membership stays a property of source, not import order.
- Datasource references go through a `DatasourceVariable` declared on the dashboard (`v2.DatasourceVariable("ds_prom")`, `"ds_loki"`); panels reference it by name via `Dashboardv2beta1DataQueryKindDatasource(name="$ds_prom")`. v2 has no `__inputs` substitution block — the runtime variable replaces it. **Note:** this is the v2beta1 shape — the `name=` field takes the variable expansion. Do **not** confuse this with v1's `{"type": "prometheus", "uid": "${DS_PROM}"}` dict pattern (which is what the `grafana` skill's `dashboard-foundation-sdk.md` documents); v1 dict form does not apply to v2.
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

**Validation order matters.** The validator requires envelope fields (`apiVersion`, `kind`, `metadata`); the ConfigMap-shipped artifact has them stripped. Order is therefore: `kgd generate` writes the envelope-wrapped JSON and validates it → `dash::render` strips to `.spec` for placement in the chart. Never run the validator against the stripped artifact.

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
- **Backslash-over-escape regression** (same file): the `grafana` skill's `dashboard-review.md` calls out a class of bugs where JSON-encoded LogQL regexes end up with `\\\\.` decoding to literal-backslash-then-any-char (misses every dot). Python f-strings make this rare but not impossible; assert `"\\\\\\\\" not in rendered_json` for each rendered dashboard.
- **Panel-builder unit tests** (`tests/test_panels.py`): each builder produces a panel with the expected datasource variable reference (`$ds_prom` / `$ds_loki`), title, query envelope shape, and visualization type. Sanity-only — the SDK's typing carries most of the weight.
- **Pre-commit hook**: ruff + ty + `pytest -q` + `dash::validate-all`.

## Risks and open items

- **Sidecar consumes v2beta1.** The scaffold emits the CRD-shaped envelope (`apiVersion: dashboard.grafana.app/v2beta1`), which natively targets the grafana-operator. Your cluster provisions via the kube-prometheus-stack ConfigMap sidecar, not an operator. The design's chosen path is: **`just dash::render` strips the envelope down to `.spec` before placing the JSON in the chart.** This depends on the Grafana version in `kube-prometheus-stack` being recent enough to parse a v2beta1-shaped `.spec` document directly from a file. Implementation plan must (a) verify the running Grafana version on `grafana.home.kettle.sh`, (b) render one dashboard, drop it in via the sidecar, and confirm it loads. Fallback if it doesn't: install grafana-operator and apply the envelope-wrapped JSON as a CRD instead of via ConfigMap.
- **Loki service name and port.** The current Loki Helm values are an upstream-defaults stub; the actual Service name (`loki-gateway` vs `loki`) and port depend on whether the chart deploys in SingleBinary or simple-scalable mode. Resolved during implementation by `kubectl -n <ns> get svc`.
- **SealedSecret vs SOPS.** Chart-side secret pattern verified at implementation time; the spec assumes whichever pattern other apps in the cluster already use.
- **Foundation-SDK pin.** Already pinned via `git+URL` at commit `a8c311b58` for v2beta1 builders. Implementation may need to bump if upstream gains useful additions; PyPI wheels remain the longer-term target once they track v2 cleanly.
- **GPU exporter packaging.** `nvidia_gpu_exporter` is on AUR; if the package is missing or broken, fall back to running the upstream binary release directly under systemd. Implementation plan should include this fallback.
- **AUR package staleness.** AUR packages can go unmaintained. Implementation plan should record the upstream binary download URLs as a fallback for both Alloy and the GPU exporter.
- **cAdvisor as a 2026 standalone agent.** `google/cadvisor` upstream is on life-support since the Kubernetes deprecation. Alloy's `prometheus.exporter.cadvisor` may or may not still ship in the pinned Alloy release. **Implementation plan must (a) verify the component exists in the chosen Alloy version**, and **(b) pre-stage [`cgroup_exporter`](https://github.com/treydock/cgroup_exporter) as the fallback** — it's cgroupv2-native, actively maintained, emits compatible enough metric names with a small relabel block.
- **`host_id` privacy.** `/etc/machine-id` is a stable cross-service correlator; this design ships `sha256(machine_id)[:16]` rather than the raw value, which is fine for a single-user homelab. If the workstation set ever expands beyond fully-trusted hosts, revisit (consider salting the hash or rotating per-deployment).
- **Cardinality back-of-envelope** (recorded so the next person doesn't redo the math): per-core `node_cpu_seconds_total` × 32 cores × 8 modes ≈ 256 series; `cpufreq` × 32 ≈ 32; `interrupts` per-CPU × ~40 named IRQs × 32 cores ≈ 1.3 k; `softirqs` × 10 types × 32 ≈ 320; cgroups (after browser collapse) ≈ 100–200; hwmon ≈ 50. **~2-3 k active series per host** — well under any kube-prometheus-stack threshold.

## Out of scope (will revisit)

- Grafana alerting on PSI / error-rate thresholds.
- Hyprland-specific instrumentation beyond cgroup-level (no viable frame-timing source today).
- Pushing dashboards via the Grafana HTTP API for fast local iteration.
- A second dashboard. The panel-builder library is built so the next one is cheap; it just isn't this PR.
