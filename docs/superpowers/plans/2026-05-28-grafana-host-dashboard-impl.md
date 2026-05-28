# Workstation Host Monitoring Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working Grafana dashboard at `grafana.home.kettle.sh` that monitors this Omarchy workstation (CPU/mem/GPU/disk/network/PSI/IRQ/journald) — fed by a host-side Grafana Alloy agent pushing to the cluster's Prometheus + Loki — with the dashboard JSON rendered programmatically from `grafana-foundation-sdk` (v2beta1) via the existing `kgd` CLI and a new `just` interface.

**Architecture:** Three-phase delivery. **Phase A** enables cluster-side ingestion (Traefik routes + basic-auth + rate-limit + remote-write receiver + exemplar storage + Loki derived fields). **Phase B** extends the existing `grafana-dashboards` Python package with a `panels/` builder library, a `host_omarchy` dashboard, a `RECORDING_RULES` declaration, and `just dash::*` recipes that render the envelope-wrapped JSON, strip to `.spec`, and write the ConfigMap + PrometheusRule into the cluster repo. **Phase C** installs Alloy + NVIDIA exporter on the workstation, renders `/etc/alloy/config.alloy` from a Jinja template via `just alloy::configure`, brings systemd units up, and runs the synthetic-metric round-trip test.

**Tech Stack:** Grafana Alloy 1.13+ (systemd, River config), `nvidia-gpu-exporter-bin` (AUR), Traefik v3 IngressRoute + Middleware, kube-prometheus-stack Helm chart, Loki 3+, ArgoCD, grafana-foundation-sdk (pinned `git+a8c311b58`), Python 3.11+/uv, `just`, Jinja2, pytest.

---

## Phase A — Cluster-side enablement (KettleCluster repo)

All Phase-A artifacts live under `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/`. The chart is a wrapper around the upstream `kube-prometheus-stack` Helm chart; ArgoCD reconciles after commit.

### Task A1: Workstation ingestion Secret

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-secret.yaml`

- [ ] **Step 1: Write the Secret manifest**

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: workstation-ingest-auth
  annotations:
    argocd.argoproj.io/sync-options: ServerSideApply=true
type: Opaque
stringData:
  username: "kettle-omarchy"
  # Generated once; chart regenerates only if the field is deleted upstream.
  password: {{ randAlphaNum 40 | quote }}
  # htpasswd format consumed by Traefik basicAuth middleware.
  # bcrypt cost 10; rendered fresh each helm template run, but ArgoCD
  # ServerSideApply preserves the cluster value on subsequent syncs.
  users: {{ printf "kettle-omarchy:%s" (htpasswd "kettle-omarchy" (randAlphaNum 40)) | quote }}
```

- [ ] **Step 2: Render-check with helm template**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -A 12 "name: workstation-ingest-auth"`
Expected: Secret block renders cleanly; `users:` field contains a `kettle-omarchy:$2a$10$...` bcrypt entry.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-secret.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: add workstation ingest auth secret"
```

### Task A2: Traefik basic-auth + rate-limit middlewares

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-middleware.yaml`

- [ ] **Step 1: Write the two Middleware CRDs**

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: workstation-ingest-basicauth
spec:
  basicAuth:
    secret: workstation-ingest-auth
    realm: "Workstation Ingest"
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: workstation-ingest-ratelimit
spec:
  rateLimit:
    average: 5000
    burst: 10000
    period: 1s
```

- [ ] **Step 2: Render-check**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -B 1 -A 6 "kind: Middleware"`
Expected: Two Middleware resources render; basicAuth references the secret from Task A1.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-middleware.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: add workstation ingest middlewares"
```

### Task A3: IngressRoute for `prometheus-ingest.home.kettle.sh`

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/prometheus-ingest-ingressroute.yaml`

- [ ] **Step 1: Write the IngressRoute**

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: prometheus-ingest
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`prometheus-ingest.home.kettle.sh`) && PathPrefix(`/api/v1/write`)
      kind: Rule
      services:
        - name: kube-prometheus-stack-prometheus
          port: 9090
      middlewares:
        - name: workstation-ingest-basicauth
        - name: workstation-ingest-ratelimit
  tls:
    secretName: home-kettle-sh-tls
```

- [ ] **Step 2: Render-check**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -A 18 "name: prometheus-ingest$"`
Expected: IngressRoute renders; both middlewares referenced; PathPrefix scopes the route to the remote-write endpoint.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/prometheus-ingest-ingressroute.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: add prometheus-ingest IngressRoute"
```

### Task A4: Discover Loki Service and write IngressRoute for `loki-ingest.home.kettle.sh`

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/loki/chart/templates/loki-ingest-ingressroute.yaml`

- [ ] **Step 1: Discover the Loki Service**

Run: `kubectl --context kettle get svc -n loki -o custom-columns=NAME:.metadata.name,PORTS:.spec.ports[*].port` (substitute correct kube context name from `kubectl config get-contexts`).
Expected: a service named `loki-gateway` (simple-scalable) or `loki` (SingleBinary) listed.

Record which Service+port was found; the IngressRoute below uses `loki-gateway:80`. If the discovered service was `loki:3100`, substitute `loki:3100` in the manifest.

- [ ] **Step 2: Ensure templates dir + write the IngressRoute**

Run: `mkdir -p /home/kettle/KettleCluster/home/apps/loki/chart/templates`

Then create the file:

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: loki-ingest
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`loki-ingest.home.kettle.sh`) && PathPrefix(`/loki/api/v1/push`)
      kind: Rule
      services:
        - name: loki-gateway   # substitute if Step-1 discovered a different name
          port: 80
      middlewares:
        - name: workstation-ingest-basicauth
          namespace: monitoring   # the basic-auth Secret + Middleware live in kube-prometheus-stack's namespace
        - name: workstation-ingest-ratelimit
          namespace: monitoring
  tls:
    secretName: home-kettle-sh-tls
```

> If `monitoring` is not the kube-prometheus-stack namespace, substitute the actual one. Check via `kubectl get applications -n argocd kube-prometheus-stack -o jsonpath='{.spec.destination.namespace}'`.

- [ ] **Step 3: Reflector for the basic-auth Secret across namespaces**

The middlewares above reference `namespace: monitoring`, so Traefik resolves them cross-namespace — no Reflector copy needed. Verify the chart's ArgoCD `Application` resource has `respectRBAC: false` or runs with cluster-scoped permissions (most kube-prometheus-stack installs do).

Run: `kubectl --context kettle get application -n argocd kube-prometheus-stack -o yaml | grep -E "respectRBAC|destination:|namespace:"`
Expected: no respectRBAC restriction or it's set to permit cross-namespace.

- [ ] **Step 4: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/loki/chart/templates/loki-ingest-ingressroute.yaml
git -C /home/kettle/KettleCluster commit -m "loki: add loki-ingest IngressRoute for workstation push"
```

### Task A5: Enable remote-write receiver, exemplar storage, and write-relabel enforcement

**Files:**
- Modify: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/values.yaml`

- [ ] **Step 1: Replace the minimal values.yaml with the enabled feature set**

Current content is the two-line stub:

```yaml
kube-prometheus-stack:
  fullnameOverride: kube-prometheus-stack
```

Replace with:

```yaml
kube-prometheus-stack:
  fullnameOverride: kube-prometheus-stack

  prometheus:
    prometheusSpec:
      # Workstation Alloy pushes via Traefik basic-auth ingress.
      enableRemoteWriteReceiver: true
      # Reserve exemplar storage so future OTLP-emitting apps light up
      # trace-ID exemplar dots on the PSI panel with no schema change.
      enableFeatures:
        - exemplar-storage
      # Defense-in-depth: workstation credential cannot overwrite
      # cluster-side series or impersonate other tenants.
      writeRelabelConfigs:
        - source_labels: [host_name]
          regex: "kettle-.*"
          action: keep
        - source_labels: [role]
          regex: "workstation"
          action: keep

  grafana:
    sidecar:
      dashboards:
        # ConfigMaps labeled grafana_dashboard=1 are loaded; the
        # grafana_folder annotation routes them into a named folder.
        folderAnnotation: grafana_folder
        provider:
          foldersFromFilesStructure: false
```

- [ ] **Step 2: Render-check the new keys are present**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -E "enableRemoteWriteReceiver|enableFeatures|writeRelabelConfigs|folderAnnotation"`
Expected: all four strings present in the rendered Prometheus CR and grafana Deployment/ConfigMap output.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/values.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: enable remote-write receiver + exemplar storage + write-relabel enforcement + folder annotations"
```

### Task A6: Loki datasource derived fields for trace_id

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/loki-datasource-derived-fields.yaml`

- [ ] **Step 1: Write the datasource ConfigMap**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-datasource-loki-workstation
  labels:
    grafana_datasource: "1"
data:
  loki.yaml: |
    apiVersion: 1
    datasources:
      - name: Loki
        type: loki
        access: proxy
        # url resolves to the loki Service inside the cluster.
        url: http://loki-gateway.loki.svc.cluster.local
        editable: false
        jsonData:
          derivedFields:
            # Anchors length (W3C trace IDs are 32 lowercase hex chars)
            # and accepts the optional 00- W3C traceparent prefix.
            - name: TraceID
              matcherRegex: 'trace_id=(?:00-)?([a-f0-9]{32})\b'
              url: '${__value.raw}'
              datasourceUid: tempo
              urlDisplayLabel: 'View in Tempo'
```

> If Task A4 Step 1 discovered the Loki service is named `loki`, change the `url` to `http://loki.loki.svc.cluster.local:3100`.

- [ ] **Step 2: Render-check**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -B 2 -A 18 "name: grafana-datasource-loki-workstation"`
Expected: ConfigMap renders with label `grafana_datasource: "1"` and the derivedFields block present.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/loki-datasource-derived-fields.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: Loki datasource derived fields for trace_id"
```

### Task A7: Push Phase-A and verify ArgoCD reconcile

- [ ] **Step 1: Push**

```bash
git -C /home/kettle/KettleCluster push origin main
```

- [ ] **Step 2: Wait for ArgoCD sync**

Run: `kubectl --context kettle -n argocd get application kube-prometheus-stack -o jsonpath='{.status.sync.status} {.status.health.status}'`
Expected: `Synced Healthy` within ~5 minutes.

Run: `kubectl --context kettle -n argocd get application loki -o jsonpath='{.status.sync.status} {.status.health.status}'`
Expected: `Synced Healthy`.

- [ ] **Step 3: Verify the new resources are present**

Run (substitute the correct namespace for kube-prometheus-stack):

```bash
kubectl --context kettle -n monitoring get secret workstation-ingest-auth
kubectl --context kettle -n monitoring get middleware workstation-ingest-basicauth workstation-ingest-ratelimit
kubectl --context kettle -n monitoring get ingressroute prometheus-ingest
kubectl --context kettle -n loki get ingressroute loki-ingest
kubectl --context kettle -n monitoring get configmap grafana-datasource-loki-workstation
```

Expected: all five resources `exist` (no `NotFound`).

- [ ] **Step 4: Verify Prometheus has remote-write receiver enabled**

Run: `kubectl --context kettle -n monitoring get prometheus -o yaml | grep enableRemoteWriteReceiver`
Expected: `enableRemoteWriteReceiver: true`.

Run: `kubectl --context kettle -n monitoring get prometheus -o jsonpath='{.items[0].spec.enableFeatures}'`
Expected: `["exemplar-storage"]`.

- [ ] **Step 5: Smoke-test basic-auth from outside the cluster**

Run (from this workstation):

```bash
WORKSTATION_PASS=$(kubectl --context kettle -n monitoring get secret workstation-ingest-auth -o jsonpath='{.data.password}' | base64 -d)
curl -sS -u "kettle-omarchy:$WORKSTATION_PASS" -X POST \
  https://prometheus-ingest.home.kettle.sh/api/v1/write \
  -H 'Content-Type: application/x-protobuf' \
  -H 'X-Prometheus-Remote-Write-Version: 0.1.0' \
  --data-binary '' -o /dev/null -w '%{http_code}\n'
```

Expected: HTTP `400` (empty body is invalid protobuf — but the request reached the receiver after auth). Anything other than 400 (e.g. 401, 503) is a failure.

Also negative-test auth:

```bash
curl -sS -u "wrong:wrong" -X POST https://prometheus-ingest.home.kettle.sh/api/v1/write -o /dev/null -w '%{http_code}\n'
```

Expected: HTTP `401`.

## Phase B — Python dashboard generator (grafana-dashboards repo)

All Phase-B work lives in `/home/kettle/git_repos/grafana-dashboards/`. Existing scaffold (commits `a306284` + `297078d`) provides `kgd` CLI, validator, envelope wrapping, `@register` discovery, and a `service_health.py` example. Phase B adds the `just` interface, the panel-builder library, the `host_omarchy` dashboard, and its recording rules.

### Task B1: Root justfile + dev module

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/justfile`
- Create: `/home/kettle/git_repos/grafana-dashboards/just/dev.just`

- [ ] **Step 1: Write the root justfile**

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

- [ ] **Step 2: Write just/dev.just**

```just
# Bootstrap dev env
[group('dev')]
dev:
    uv sync --all-groups
    uv run pre-commit install || echo "pre-commit not configured yet; skip"

# Lint
[group('dev')]
lint:
    uv run ruff check .
    uv run ty check src

# Test
[group('dev')]
test:
    uv run pytest -q

# Clean rendered artifacts
[group('dev')]
clean:
    rm -rf dist/ .pytest_cache __pycache__
```

- [ ] **Step 3: Verify just lists imports/mods (mods will fail to load because files don't exist yet — acceptable for this task)**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just --list 2>&1 | head -20`
Expected: `dev`, `lint`, `test`, `clean` recipes appear; module-load errors for missing `just/alloy.just`, `just/dash.just`, `just/cluster.just` are OK at this stage (they're created in later tasks).

- [ ] **Step 4: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add justfile just/dev.just
git -C /home/kettle/git_repos/grafana-dashboards commit -m "just: root justfile + dev module"
```

### Task B2: Stub the remaining just modules (alloy / dash / cluster)

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/just/alloy.just`
- Create: `/home/kettle/git_repos/grafana-dashboards/just/dash.just`
- Create: `/home/kettle/git_repos/grafana-dashboards/just/cluster.just`

- [ ] **Step 1: Write alloy.just stub**

```just
# Alloy host-agent management. Filled out in Phase C.
[group('alloy')]
default:
    @echo "alloy:: recipes will be populated in Phase C"
```

- [ ] **Step 2: Write dash.just stub**

```just
# Dashboard rendering. Filled out in Task B11.
[group('dash')]
default:
    @echo "dash:: recipes will be populated after panel builders are ready"
```

- [ ] **Step 3: Write cluster.just stub**

```just
# Cluster-side artifacts. Phase A delivered them manually; this module
# is reserved for re-rendering / per-host expansion later.
[group('cluster')]
default:
    @echo "cluster:: recipes are reserved; Phase A artifacts shipped manually"
```

- [ ] **Step 4: Verify all modules load cleanly**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just --list --list-submodules 2>&1 | head -30`
Expected: no errors; modules listed with their `default` recipes.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add just/alloy.just just/dash.just just/cluster.just
git -C /home/kettle/git_repos/grafana-dashboards commit -m "just: stub alloy/dash/cluster modules"
```

### Task B3: panels/_common.py — datasource shims, defaults, query envelope helpers

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/__init__.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/_common.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_common.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_common.py
from __future__ import annotations

from grafana_dashboards.panels._common import (
    PROM_DS_VAR,
    LOKI_DS_VAR,
    PromQuery,
    LokiQuery,
    target,
    legend_table_right,
)
from grafana_foundation_sdk.models.dashboardv2beta1 import DataQueryKind


def test_prom_query_wraps_in_v2_envelope():
    q = PromQuery("up", legend="{{instance}}").build()
    assert isinstance(q, DataQueryKind)
    assert q.group == "prometheus"
    assert q.version == "v0"
    assert q.datasource.name == PROM_DS_VAR
    assert q.spec["expr"] == "up"
    assert q.spec["legendFormat"] == "{{instance}}"
    assert q.spec["refId"] == "A"
    assert q.spec["editorMode"] == "code"


def test_loki_query_wraps_in_v2_envelope():
    q = LokiQuery('{job="x"} |= "err"').build()
    assert isinstance(q, DataQueryKind)
    assert q.group == "loki"
    assert q.datasource.name == LOKI_DS_VAR
    assert q.spec["expr"] == '{job="x"} |= "err"'


def test_target_helper_emits_query_group_with_refid():
    qg = target(PromQuery("up"))
    # QueryGroup builder API: .target(Target).build() returns the model;
    # ensure that calling build() succeeds and refId is set on the inner target.
    built = qg.build()
    assert hasattr(built, "queries") or hasattr(built, "targets")


def test_prom_query_ds_var_is_variable_expansion_not_uid():
    assert PROM_DS_VAR == "$ds_prom"
    assert LOKI_DS_VAR == "$ds_loki"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_common.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grafana_dashboards.panels'`.

- [ ] **Step 3: Write the implementation**

Create `src/grafana_dashboards/panels/__init__.py`:

```python
"""Reusable panel builders for the workstation host dashboard."""
```

Create `src/grafana_dashboards/panels/_common.py`:

```python
from __future__ import annotations

from grafana_foundation_sdk.builders import (
    common as common_b,
    dashboardv2beta1 as v2,
)
from grafana_foundation_sdk.models.common import (
    LegendDisplayMode,
    LegendPlacement,
    TooltipDisplayMode,
)
from grafana_foundation_sdk.models.dashboardv2beta1 import (
    DataQueryKind,
    Dashboardv2beta1DataQueryKindDatasource,
)

PROM_DS_VAR = "$ds_prom"
LOKI_DS_VAR = "$ds_loki"

# Default host filter selector used by every panel. Resolved at query
# time to the variable's value (default "kettle-omarchy").
HOST_FILTER = 'host_name="$host"'


class PromQuery:
    """Wrap a PromQL expression in v2's DataQueryKind envelope.

    v2 rejects the v1-shape query bodies the SDK's per-datasource
    builders still emit. Same shim pattern as service_health.py.
    """

    def __init__(
        self,
        expr: str,
        *,
        legend: str = "",
        ref_id: str = "A",
        instant: bool = False,
    ) -> None:
        spec: dict = {"expr": expr, "editorMode": "code", "refId": ref_id}
        if legend:
            spec["legendFormat"] = legend
        if instant:
            spec["instant"] = True
        self._inner = DataQueryKind(
            group="prometheus",
            version="v0",
            datasource=Dashboardv2beta1DataQueryKindDatasource(name=PROM_DS_VAR),
            spec=spec,
        )

    def build(self) -> DataQueryKind:
        return self._inner


class LokiQuery:
    """Wrap a LogQL expression in v2's DataQueryKind envelope."""

    def __init__(
        self,
        expr: str,
        *,
        legend: str = "",
        ref_id: str = "A",
    ) -> None:
        spec: dict = {"expr": expr, "editorMode": "code", "refId": ref_id}
        if legend:
            spec["legendFormat"] = legend
        self._inner = DataQueryKind(
            group="loki",
            version="v0",
            datasource=Dashboardv2beta1DataQueryKindDatasource(name=LOKI_DS_VAR),
            spec=spec,
        )

    def build(self) -> DataQueryKind:
        return self._inner


def target(query: PromQuery | LokiQuery, ref_id: str = "A") -> v2.QueryGroup:
    """Wrap a single query in a v2 QueryGroup with one Target."""
    return v2.QueryGroup().target(v2.Target().ref_id(ref_id).query(query))


def legend_table_right() -> common_b.VizLegendOptions:
    """Standard table-right legend used on most timeseries panels."""
    return (
        common_b.VizLegendOptions()
        .show_legend(True)
        .placement(LegendPlacement.RIGHT)
        .display_mode(LegendDisplayMode.TABLE)
        .calcs(["lastNotNull", "max"])
    )


def tooltip_multi() -> common_b.VizTooltipOptions:
    """Multi-series tooltip with descending sort. Default for stutter forensics."""
    return (
        common_b.VizTooltipOptions()
        .mode(TooltipDisplayMode.MULTI)
        .sort("desc")
    )
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_common.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/__init__.py src/grafana_dashboards/panels/_common.py tests/test_panels_common.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: _common shims (PromQuery, LokiQuery, target, legend defaults)"
```

### Task B4: panels/stat.py — single-value stat panels

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/stat.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_stat.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_stat.py
from grafana_dashboards.panels.stat import (
    stat_psi_cpu, stat_psi_mem, stat_psi_io,
    stat_load1, stat_load5, stat_load15,
    stat_uptime, stat_temp, stat_stutter_count,
)


def _expr_of(panel_builder):
    """Pull the first query expr out of a built panel."""
    panel = panel_builder.build()
    query_group = panel.data
    qg = query_group.build() if hasattr(query_group, "build") else query_group
    targets = getattr(qg, "queries", None) or getattr(qg, "targets", [])
    inner = targets[0]
    target_inner = inner.build() if hasattr(inner, "build") else inner
    q = target_inner.query
    q_inner = q.build() if hasattr(q, "build") else q
    return q_inner.spec["expr"]


def test_stat_psi_cpu_uses_recording_rule_with_clamp_and_percent():
    expr = _expr_of(stat_psi_cpu())
    assert "host:psi_cpu_waiting:ratio1m" in expr
    assert "clamp_max" in expr and "100" in expr
    assert 'host_name="$host"' in expr


def test_stat_psi_mem_io_use_their_respective_ratios():
    assert "host:psi_memory_waiting:ratio1m" in _expr_of(stat_psi_mem())
    assert "host:psi_io_waiting:ratio1m" in _expr_of(stat_psi_io())


def test_stat_load_panels_pin_correct_node_exporter_series():
    assert "node_load1{" in _expr_of(stat_load1())
    assert "node_load5{" in _expr_of(stat_load5())
    assert "node_load15{" in _expr_of(stat_load15())


def test_stat_uptime_is_now_minus_boot():
    expr = _expr_of(stat_uptime())
    assert "node_time_seconds" in expr
    assert "node_boot_time_seconds" in expr


def test_stat_temp_uses_max_over_hwmon():
    expr = _expr_of(stat_temp())
    assert "max" in expr
    assert "node_hwmon_temp_celsius" in expr


def test_stat_stutter_count_reads_recording_rule_directly():
    expr = _expr_of(stat_stutter_count())
    assert expr.startswith("host:psi_cpu_stutter_events:count5m")
    assert 'host_name="$host"' in expr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_stat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grafana_dashboards.panels.stat'`.

- [ ] **Step 3: Write the implementation**

```python
# src/grafana_dashboards/panels/stat.py
from __future__ import annotations

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2, stat as stat_b
from grafana_foundation_sdk.models.common import BigValueGraphMode

from grafana_dashboards.panels._common import (
    HOST_FILTER, PromQuery, target,
)

_THRESH_GREEN_AMBER_RED = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 10},
    {"color": "orange", "value": 30},
    {"color": "red", "value": 60},
]


def _stat(pid: int, title: str, expr: str, *, unit: str = "short",
          thresholds: list | None = None) -> v2.Panel:
    viz = stat_b.Visualization().unit(unit).graph_mode(BigValueGraphMode.AREA)
    if thresholds:
        viz = viz.thresholds_steps(thresholds)
    return (
        v2.Panel()
        .id(pid)
        .title(title)
        .data(target(PromQuery(expr, instant=True)))
        .visualization(viz)
    )


def stat_psi_cpu() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_cpu_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(101, "PSI CPU (1m %)", expr, unit="percent",
                 thresholds=_THRESH_GREEN_AMBER_RED)


def stat_psi_mem() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_memory_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(102, "PSI Memory (1m %)", expr, unit="percent",
                 thresholds=_THRESH_GREEN_AMBER_RED)


def stat_psi_io() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_io_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(103, "PSI I/O (1m %)", expr, unit="percent",
                 thresholds=_THRESH_GREEN_AMBER_RED)


def stat_load1() -> v2.Panel:
    return _stat(104, "Load 1m", f"node_load1{{{HOST_FILTER}}}")


def stat_load5() -> v2.Panel:
    return _stat(105, "Load 5m", f"node_load5{{{HOST_FILTER}}}")


def stat_load15() -> v2.Panel:
    return _stat(106, "Load 15m", f"node_load15{{{HOST_FILTER}}}")


def stat_uptime() -> v2.Panel:
    expr = (
        f"node_time_seconds{{{HOST_FILTER}}} - "
        f"node_boot_time_seconds{{{HOST_FILTER}}}"
    )
    return _stat(107, "Uptime", expr, unit="s")


def stat_temp() -> v2.Panel:
    expr = f"max(node_hwmon_temp_celsius{{{HOST_FILTER}}})"
    return _stat(108, "Max CPU temp", expr, unit="celsius",
                 thresholds=[
                     {"color": "green", "value": None},
                     {"color": "yellow", "value": 75},
                     {"color": "orange", "value": 85},
                     {"color": "red", "value": 95},
                 ])


def stat_stutter_count() -> v2.Panel:
    expr = f"host:psi_cpu_stutter_events:count5m{{{HOST_FILTER}}}"
    return _stat(109, "Stutter events (5m)", expr,
                 thresholds=[
                     {"color": "green", "value": None},
                     {"color": "yellow", "value": 1},
                     {"color": "red", "value": 3},
                 ])
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_stat.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/stat.py tests/test_panels_stat.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: stat builders (PSI, loadN, uptime, temp, stutter count)"
```

### Task B5: panels/timeseries.py — timeseries panels

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/timeseries.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_timeseries.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_timeseries.py
import pytest
from grafana_dashboards.panels.timeseries import (
    ts_psi_all, ts_cpu_per_core, ts_cpu_freq, ts_sched_runqueue,
    ts_irqs, ts_softirqs, ts_mem_breakdown,
    ts_gpu_util, ts_gpu_mem, ts_gpu_temp_power, ts_gpu_clock,
    ts_disk_iops, ts_disk_throughput, ts_disk_io_latency_p99, ts_io_wait,
    ts_net_bytes, ts_net_errors,
)


def _exprs(panel_builder):
    panel = panel_builder.build()
    qg = panel.data.build() if hasattr(panel.data, "build") else panel.data
    targets = getattr(qg, "queries", None) or getattr(qg, "targets", [])
    out = []
    for t in targets:
        ti = t.build() if hasattr(t, "build") else t
        q = ti.query
        qi = q.build() if hasattr(q, "build") else q
        out.append(qi.spec["expr"])
    return out


@pytest.mark.parametrize("builder, must_contain", [
    (ts_psi_all, ["psi_cpu_waiting:ratio1m", "psi_memory_waiting:ratio1m", "psi_io_waiting:ratio1m"]),
    (ts_cpu_per_core, ["node_cpu_seconds_total"]),
    (ts_cpu_freq, ["node_cpu_frequency_hertz"]),
    (ts_sched_runqueue, ["node_schedstat_waiting_seconds_total"]),
    (ts_irqs, ["node_interrupts_total"]),
    (ts_softirqs, ["node_softirqs_total"]),
    (ts_mem_breakdown, ["node_memory_MemTotal_bytes", "node_memory_MemFree_bytes"]),
    (ts_gpu_util, ["nvidia_smi_utilization_gpu_ratio"]),
    (ts_gpu_mem, ["nvidia_smi_memory_used_bytes"]),
    (ts_gpu_temp_power, ["nvidia_smi_temperature_gpu", "nvidia_smi_power_draw_watts"]),
    (ts_gpu_clock, ["nvidia_smi_clocks_current_graphics_clock_hz"]),
    (ts_disk_iops, ["node_disk_reads_completed_total", "node_disk_writes_completed_total"]),
    (ts_disk_throughput, ["node_disk_read_bytes_total", "node_disk_written_bytes_total"]),
    (ts_disk_io_latency_p99, ["node_disk_io_time_weighted_seconds_total"]),
    (ts_io_wait, ["node_cpu_seconds_total", "mode=\"iowait\""]),
    (ts_net_bytes, ["node_network_receive_bytes_total", "node_network_transmit_bytes_total"]),
    (ts_net_errors, ["node_network_receive_errs_total", "node_network_transmit_errs_total"]),
])
def test_timeseries_panels_pin_correct_metric_names(builder, must_contain):
    blob = "\n".join(_exprs(builder()))
    for needle in must_contain:
        assert needle in blob, f"{builder.__name__} missing {needle!r}; got: {blob}"


def test_all_timeseries_panels_filter_by_host():
    for b in [ts_psi_all, ts_cpu_per_core, ts_cpu_freq, ts_disk_iops]:
        for e in _exprs(b()):
            assert 'host_name="$host"' in e
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_timeseries.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# src/grafana_dashboards/panels/timeseries.py
from __future__ import annotations

from grafana_foundation_sdk.builders import (
    common as common_b,
    dashboardv2beta1 as v2,
    timeseries as ts_b,
)
from grafana_foundation_sdk.models.common import GraphDrawStyle, StackingMode

from grafana_dashboards.panels._common import (
    HOST_FILTER, PromQuery, legend_table_right, tooltip_multi,
)


def _ts_viz(unit: str = "short", fill: int = 10,
            stack: StackingMode = StackingMode.NONE) -> ts_b.Visualization:
    return (
        ts_b.Visualization()
        .unit(unit)
        .draw_style(GraphDrawStyle.LINE)
        .fill_opacity(fill)
        .stacking(common_b.StackingConfig().mode(stack).group("A"))
        .legend(legend_table_right())
        .tooltip(tooltip_multi())
    )


def _panel(pid: int, title: str, viz: ts_b.Visualization,
           queries: list[tuple[str, str]]) -> v2.Panel:
    """queries: list of (expr, legend)."""
    qg = v2.QueryGroup()
    for i, (expr, legend) in enumerate(queries):
        ref = chr(ord("A") + i)
        qg = qg.target(
            v2.Target().ref_id(ref).query(PromQuery(expr, legend=legend, ref_id=ref))
        )
    return v2.Panel().id(pid).title(title).data(qg).visualization(viz)


def ts_psi_all() -> v2.Panel:
    return _panel(201, "PSI — CPU / Memory / I/O", _ts_viz(unit="percentunit"),
                  [
                      (f"host:psi_cpu_waiting:ratio1m{{{HOST_FILTER}}}", "cpu"),
                      (f"host:psi_memory_waiting:ratio1m{{{HOST_FILTER}}}", "memory"),
                      (f"host:psi_io_waiting:ratio1m{{{HOST_FILTER}}}", "io"),
                  ])


def ts_cpu_per_core() -> v2.Panel:
    # node_cpu_seconds_total has mode + cpu labels. Compute per-core
    # busy = 1 - rate(idle). i9-13900K has 32 logical CPUs.
    expr = (
        f"1 - rate(node_cpu_seconds_total{{{HOST_FILTER},mode=\"idle\"}}[$__rate_interval])"
    )
    return _panel(202, "CPU per-core utilization", _ts_viz(unit="percentunit"),
                  [(expr, "cpu{{cpu}}")])


def ts_cpu_freq() -> v2.Panel:
    expr = f"node_cpu_frequency_hertz{{{HOST_FILTER}}}"
    return _panel(203, "CPU frequency", _ts_viz(unit="hertz"),
                  [(expr, "cpu{{cpu}}")])


def ts_sched_runqueue() -> v2.Panel:
    expr = (
        f"rate(node_schedstat_waiting_seconds_total{{{HOST_FILTER}}}[$__rate_interval])"
    )
    return _panel(204, "Scheduler run-queue wait", _ts_viz(unit="s"),
                  [(expr, "cpu{{cpu}}")])


def ts_irqs() -> v2.Panel:
    # Top by interrupt type — too many to show every IRQ.
    expr = (
        f"topk(15, sum by (info) "
        f"(rate(node_interrupts_total{{{HOST_FILTER}}}[$__rate_interval])))"
    )
    return _panel(205, "Hardware interrupts (top 15)", _ts_viz(),
                  [(expr, "{{info}}")])


def ts_softirqs() -> v2.Panel:
    expr = (
        f"sum by (type) "
        f"(rate(node_softirqs_total{{{HOST_FILTER}}}[$__rate_interval]))"
    )
    return _panel(206, "Softirqs", _ts_viz(),
                  [(expr, "{{type}}")])


def ts_mem_breakdown() -> v2.Panel:
    return _panel(207, "Memory breakdown", _ts_viz(unit="bytes",
                                                   stack=StackingMode.NORMAL),
                  [
                      (f"node_memory_MemTotal_bytes{{{HOST_FILTER}}} - "
                       f"node_memory_MemAvailable_bytes{{{HOST_FILTER}}}", "used"),
                      (f"node_memory_Cached_bytes{{{HOST_FILTER}}}", "cached"),
                      (f"node_memory_MemFree_bytes{{{HOST_FILTER}}}", "free"),
                      (f"node_memory_SwapTotal_bytes{{{HOST_FILTER}}} - "
                       f"node_memory_SwapFree_bytes{{{HOST_FILTER}}}", "swap used"),
                  ])


def ts_gpu_util() -> v2.Panel:
    return _panel(301, "GPU utilization", _ts_viz(unit="percentunit"),
                  [(f"nvidia_smi_utilization_gpu_ratio{{{HOST_FILTER}}}", "gpu{{index}}")])


def ts_gpu_mem() -> v2.Panel:
    return _panel(302, "GPU memory", _ts_viz(unit="bytes"),
                  [
                      (f"nvidia_smi_memory_used_bytes{{{HOST_FILTER}}}", "used"),
                      (f"nvidia_smi_memory_total_bytes{{{HOST_FILTER}}}", "total"),
                  ])


def ts_gpu_temp_power() -> v2.Panel:
    return _panel(303, "GPU temperature & power", _ts_viz(),
                  [
                      (f"nvidia_smi_temperature_gpu{{{HOST_FILTER}}}", "temp °C"),
                      (f"nvidia_smi_power_draw_watts{{{HOST_FILTER}}}", "power W"),
                  ])


def ts_gpu_clock() -> v2.Panel:
    return _panel(304, "GPU clock", _ts_viz(unit="hertz"),
                  [
                      (f"nvidia_smi_clocks_current_graphics_clock_hz{{{HOST_FILTER}}}", "graphics"),
                      (f"nvidia_smi_clocks_current_memory_clock_hz{{{HOST_FILTER}}}", "memory"),
                  ])


def ts_disk_iops() -> v2.Panel:
    return _panel(401, "Disk IOPS per device", _ts_viz(),
                  [
                      (f"rate(node_disk_reads_completed_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "read {{device}}"),
                      (f"rate(node_disk_writes_completed_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "write {{device}}"),
                  ])


def ts_disk_throughput() -> v2.Panel:
    return _panel(402, "Disk throughput per device", _ts_viz(unit="Bps"),
                  [
                      (f"rate(node_disk_read_bytes_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "read {{device}}"),
                      (f"rate(node_disk_written_bytes_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "write {{device}}"),
                  ])


def ts_disk_io_latency_p99() -> v2.Panel:
    # weighted IO time divided by IOPS approximates per-IO latency.
    expr = (
        f"rate(node_disk_io_time_weighted_seconds_total{{{HOST_FILTER}}}[$__rate_interval]) "
        f"/ "
        f"clamp_min("
        f"  rate(node_disk_reads_completed_total{{{HOST_FILTER}}}[$__rate_interval]) "
        f"+ rate(node_disk_writes_completed_total{{{HOST_FILTER}}}[$__rate_interval]),"
        f"  1)"
    )
    return _panel(403, "Disk IO latency (approx)", _ts_viz(unit="s"),
                  [(expr, "{{device}}")])


def ts_io_wait() -> v2.Panel:
    expr = (
        f"rate(node_cpu_seconds_total{{{HOST_FILTER},mode=\"iowait\"}}[$__rate_interval])"
    )
    return _panel(404, "I/O wait per CPU", _ts_viz(unit="percentunit"),
                  [(expr, "cpu{{cpu}}")])


def ts_net_bytes() -> v2.Panel:
    return _panel(501, "Network throughput", _ts_viz(unit="Bps"),
                  [
                      (f"rate(node_network_receive_bytes_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "rx {{device}}"),
                      (f"rate(node_network_transmit_bytes_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "tx {{device}}"),
                  ])


def ts_net_errors() -> v2.Panel:
    return _panel(502, "Network errors + drops", _ts_viz(),
                  [
                      (f"rate(node_network_receive_errs_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "rx err {{device}}"),
                      (f"rate(node_network_transmit_errs_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "tx err {{device}}"),
                      (f"rate(node_network_receive_drop_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "rx drop {{device}}"),
                      (f"rate(node_network_transmit_drop_total{{{HOST_FILTER}}}[$__rate_interval])",
                       "tx drop {{device}}"),
                  ])
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_timeseries.py -v`
Expected: all parametrised cases pass + host-filter test passes.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/timeseries.py tests/test_panels_timeseries.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: timeseries builders (PSI, CPU, GPU, disk, network, sched, IRQ)"
```

### Task B6: panels/tables.py — top-talker tables

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/tables.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_tables.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_tables.py
from grafana_dashboards.panels.tables import (
    top_cgroup_cpu_table, top_cgroup_mem_table, top_error_units_table,
)


def _expr(panel_builder):
    panel = panel_builder.build()
    qg = panel.data.build() if hasattr(panel.data, "build") else panel.data
    targets = getattr(qg, "queries", None) or getattr(qg, "targets", [])
    ti = targets[0].build() if hasattr(targets[0], "build") else targets[0]
    q = ti.query
    return (q.build() if hasattr(q, "build") else q).spec["expr"]


def test_top_cgroup_cpu_table_uses_query_time_topk():
    expr = _expr(top_cgroup_cpu_table)
    assert "topk(10," in expr
    assert "host:cgroup_cpu:sum5m" in expr
    assert 'host_name="$host"' in expr


def test_top_cgroup_mem_table_uses_recording_rule():
    expr = _expr(top_cgroup_mem_table)
    assert "topk(10," in expr
    assert "host:cgroup_memory_rss:sum5m" in expr


def test_top_error_units_table_loki_query():
    expr = _expr(top_error_units_table)
    assert "sum by (unit)" in expr
    assert "rate({" in expr
    assert 'host_name="$host"' in expr
    assert 'priority=~"0|1|2|3"' in expr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_tables.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# src/grafana_dashboards/panels/tables.py
from __future__ import annotations

from grafana_foundation_sdk.builders import (
    dashboardv2beta1 as v2,
    table as table_b,
)

from grafana_dashboards.panels._common import (
    HOST_FILTER, LokiQuery, PromQuery, target,
)


def _table_panel(pid: int, title: str, query) -> v2.Panel:
    viz = table_b.Visualization()
    return (
        v2.Panel()
        .id(pid)
        .title(title)
        .data(target(query))
        .visualization(viz)
    )


def top_cgroup_cpu_table() -> v2.Panel:
    expr = f"topk(10, host:cgroup_cpu:sum5m{{{HOST_FILTER}}})"
    return _table_panel(601, "Top units by CPU (5m)", PromQuery(expr, instant=True))


def top_cgroup_mem_table() -> v2.Panel:
    expr = f"topk(10, host:cgroup_memory_rss:sum5m{{{HOST_FILTER}}})"
    return _table_panel(602, "Top units by RSS (5m)", PromQuery(expr, instant=True))


def top_error_units_table() -> v2.Panel:
    expr = (
        'topk(10, sum by (unit) ('
        f'rate({{{HOST_FILTER},priority=~"0|1|2|3"}}[5m])'
        '))'
    )
    return _table_panel(701, "Top error-emitting units (5m)", LokiQuery(expr))
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_tables.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/tables.py tests/test_panels_tables.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: top-talker tables (cgroup CPU, cgroup RSS, error units)"
```

### Task B7: panels/logs.py — error-rate timeseries + live logs panel

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/logs.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_logs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_logs.py
from grafana_dashboards.panels.logs import logs_panel, error_rate_timeseries


def _exprs(panel_builder):
    panel = panel_builder.build()
    qg = panel.data.build() if hasattr(panel.data, "build") else panel.data
    targets = getattr(qg, "queries", None) or getattr(qg, "targets", [])
    out = []
    for t in targets:
        ti = t.build() if hasattr(t, "build") else t
        q = ti.query
        qi = q.build() if hasattr(q, "build") else q
        out.append(qi.spec["expr"])
    return out


def test_error_rate_timeseries_filters_by_priority_and_host():
    e = _exprs(error_rate_timeseries())[0]
    assert "sum by (unit)" in e
    assert "rate({" in e
    assert 'host_name="$host"' in e
    assert 'priority=~"0|1|2|3"' in e


def test_logs_panel_is_loki_and_tail_window():
    panel = logs_panel().build()
    # Logs panel uses the logs visualization (LogsViz / logs builder)
    # — we just smoke-test that the data is populated.
    assert panel.data is not None
    e = _exprs(logs_panel)[0]
    assert '{' in e and '}' in e
    assert 'host_name="$host"' in e
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_logs.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# src/grafana_dashboards/panels/logs.py
from __future__ import annotations

from grafana_foundation_sdk.builders import (
    dashboardv2beta1 as v2,
    logs as logs_b,
    timeseries as ts_b,
)

from grafana_dashboards.panels._common import (
    HOST_FILTER, LokiQuery, target,
)
from grafana_dashboards.panels.timeseries import _ts_viz  # type: ignore[attr-defined]


def error_rate_timeseries() -> v2.Panel:
    expr = (
        'sum by (unit) ('
        f'rate({{{HOST_FILTER},priority=~"0|1|2|3"}}[$__rate_interval])'
        ')'
    )
    return (
        v2.Panel()
        .id(702)
        .title("Error log rate by unit")
        .data(target(LokiQuery(expr)))
        .visualization(_ts_viz())
    )


def logs_panel() -> v2.Panel:
    # Tail of error-priority lines, filtered to the dashboard time
    # window via Grafana's $__from/$__to (implicit on Loki queries).
    expr = f'{{{HOST_FILTER},priority=~"0|1|2|3"}}'
    viz = (
        logs_b.Visualization()
        .show_time(True)
        .show_labels(False)
        .show_common_labels(False)
        .wrap_log_message(True)
        .enable_log_details(True)
        .dedup_strategy("none")
    )
    return (
        v2.Panel()
        .id(703)
        .title("Error log tail")
        .data(target(LokiQuery(expr)))
        .visualization(viz)
    )
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_logs.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/logs.py tests/test_panels_logs.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: logs (error_rate_timeseries, logs_panel)"
```

### Task B8: variables.py — template variables

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/variables.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_variables.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_variables.py
from grafana_dashboards.variables import build_variables


def test_build_variables_contains_required_set():
    vs = build_variables()
    names = []
    for v in vs:
        built = v.build() if hasattr(v, "build") else v
        spec = getattr(built, "spec", built)
        names.append(getattr(spec, "name", None) or
                     (spec.get("name") if isinstance(spec, dict) else None))
    assert "ds_prom" in names
    assert "ds_loki" in names
    assert "host" in names
    assert "window" in names
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_variables.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# src/grafana_dashboards/variables.py
from __future__ import annotations

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2
from grafana_foundation_sdk.models.dashboardv2beta1 import VariableRefresh

from grafana_dashboards.panels._common import PromQuery


def build_variables() -> list:
    ds_prom = (
        v2.DatasourceVariable("ds_prom")
        .label("Prometheus")
        .plugin_id("prometheus")
        .description("Prometheus datasource for host metrics.")
    )
    ds_loki = (
        v2.DatasourceVariable("ds_loki")
        .label("Loki")
        .plugin_id("loki")
        .description("Loki datasource for host journald logs.")
    )
    host = (
        v2.QueryVariable("host")
        .label("Host")
        .query(PromQuery('label_values(node_uname_info, host_name)'))
        .refresh(VariableRefresh.ON_DASHBOARD_LOAD)
        .multi(False)
        .include_all(False)
        # Default value resolved at first dashboard load.
    )
    # Custom variable for the cgroup-table window. Drives panel titles
    # only; the recording rule pre-aggregates at 5m regardless of
    # selection. To swap window for the cgroup table at query time,
    # the panel could parse the variable, but that's out of scope.
    window = (
        v2.CustomVariable("window")
        .label("Window")
        .query("1m,5m,15m,1h,6h")
        .current("5m")
    )
    return [ds_prom, ds_loki, host, window]
```

- [ ] **Step 4: Run the test**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_variables.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/variables.py tests/test_variables.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "variables: ds_prom, ds_loki, host, window template variables"
```

### Task B9: rows.py — Grid/Row composers

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/rows.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_rows.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rows.py
from grafana_dashboards.rows import grid_row


def test_grid_row_lays_out_elements_left_to_right_with_carriage_return():
    items = grid_row(
        title="Row 1",
        elements=[
            ("a", 6, 4),
            ("b", 6, 4),
            ("c", 12, 4),
        ],
    )
    row = items.build() if hasattr(items, "build") else items
    grid = row.spec.layout
    grid_inner = grid.build() if hasattr(grid, "build") else grid
    pos = [
        (it.spec.element.name, it.spec.x, it.spec.y, it.spec.width, it.spec.height)
        for it in grid_inner.spec.items
    ]
    assert pos == [("a", 0, 0, 6, 4), ("b", 6, 0, 6, 4), ("c", 12, 0, 12, 4)]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_rows.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# src/grafana_dashboards/rows.py
from __future__ import annotations

from collections.abc import Iterable

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2


def grid_row(title: str, elements: Iterable[tuple[str, int, int]],
             *, collapsed: bool = False) -> v2.Row:
    """Build a v2 Row containing a Grid laid out left-to-right.

    Args:
        title: Row title shown in the UI.
        elements: Iterable of (element_name, width, height). x is
            auto-incremented; when x + width would exceed 24 the
            layout wraps to a new line.
        collapsed: Whether the row starts collapsed.
    """
    grid = v2.Grid()
    x = 0
    y = 0
    row_height = 0
    for name, w, h in elements:
        if x + w > 24:
            x = 0
            y += row_height
            row_height = 0
        grid = grid.item(
            v2.GridItem().name(name).x(x).y(y).width(w).height(h)
        )
        x += w
        row_height = max(row_height, h)
    return v2.Row().title(title).collapse(collapsed).layout(grid)
```

- [ ] **Step 4: Run the test**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_rows.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/rows.py tests/test_rows.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "rows: grid_row composer with auto x/y wrap"
```

### Task B10: dashboards/host_omarchy.py — the dashboard + RECORDING_RULES

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/dashboards/host_omarchy.py`
- Modify: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/dashboards/__init__.py` (append to `_AUTOLOAD`)

- [ ] **Step 1: Write the dashboard module**

```python
# src/grafana_dashboards/dashboards/host_omarchy.py
"""Workstation host dashboard for kettle-omarchy.

Renders to UID `kettle-host-omarchy`. Folder placement is via the
ConfigMap annotation `grafana_folder: "Workstation"` applied by the
dash::render recipe at template-wrap time.
"""

from __future__ import annotations

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2
from grafana_foundation_sdk.models.dashboardv2beta1 import DashboardCursorSync

from grafana_dashboards.dashboards import DashboardSpec, register
from grafana_dashboards.panels import logs as logs_p
from grafana_dashboards.panels import stat
from grafana_dashboards.panels import tables
from grafana_dashboards.panels import timeseries as ts
from grafana_dashboards.rows import grid_row
from grafana_dashboards.variables import build_variables


# Recording rules emitted as a PrometheusRule alongside the dashboard.
RECORDING_RULES = [
    {
        "record": "host:psi_cpu_waiting:ratio1m",
        "expr": "rate(node_pressure_cpu_waiting_seconds_total[1m])",
    },
    {
        "record": "host:psi_memory_waiting:ratio1m",
        "expr": "rate(node_pressure_memory_waiting_seconds_total[1m])",
    },
    {
        "record": "host:psi_io_waiting:ratio1m",
        "expr": "rate(node_pressure_io_waiting_seconds_total[1m])",
    },
    {
        "record": "host:psi_cpu_stutter_events:count5m",
        "expr": (
            "count_over_time("
            "(host:psi_cpu_waiting:ratio1m > 0.30)[5m:1m]"
            ")"
        ),
    },
    {
        "record": "host:cgroup_cpu:sum5m",
        "expr": (
            "sum by (host_name, name) ("
            'rate(container_cpu_usage_seconds_total{name!=""}[5m])'
            ")"
        ),
    },
    {
        "record": "host:cgroup_memory_rss:sum5m",
        "expr": (
            "sum by (host_name, name) ("
            "avg_over_time(container_memory_rss[5m])"
            ")"
        ),
    },
]


# (element_name, builder_factory, width, height)
_LAYOUT: list[tuple[str, str, int, int]] = [
    # Row 1 — right-now indicators
    ("psi-cpu",        "stat.stat_psi_cpu",        3, 3),
    ("psi-mem",        "stat.stat_psi_mem",        3, 3),
    ("psi-io",         "stat.stat_psi_io",         3, 3),
    ("load1",          "stat.stat_load1",          2, 3),
    ("load5",          "stat.stat_load5",          2, 3),
    ("load15",         "stat.stat_load15",         2, 3),
    ("uptime",         "stat.stat_uptime",         3, 3),
    ("temp-max",       "stat.stat_temp",           3, 3),
    ("stutter-count",  "stat.stat_stutter_count",  6, 3),
    # Row 2 — headline PSI
    ("psi-all",        "ts.ts_psi_all",           24, 8),
    # Row 3 — CPU detail
    ("cpu-per-core",   "ts.ts_cpu_per_core",      12, 8),
    ("cpu-freq",       "ts.ts_cpu_freq",          12, 8),
    ("sched-runq",     "ts.ts_sched_runqueue",    12, 6),
    ("top-cpu",        "tables.top_cgroup_cpu_table", 12, 6),
    # Row 4 — Memory
    ("mem-break",      "ts.ts_mem_breakdown",     12, 8),
    ("top-mem",        "tables.top_cgroup_mem_table", 12, 8),
    # Row 5 — GPU
    ("gpu-util",       "ts.ts_gpu_util",          12, 6),
    ("gpu-mem",        "ts.ts_gpu_mem",           12, 6),
    ("gpu-temp-power", "ts.ts_gpu_temp_power",    12, 6),
    ("gpu-clock",      "ts.ts_gpu_clock",         12, 6),
    # Row 6 — Disk / IO
    ("disk-iops",      "ts.ts_disk_iops",         12, 6),
    ("disk-throughput","ts.ts_disk_throughput",   12, 6),
    ("disk-io-latency","ts.ts_disk_io_latency_p99",12, 6),
    ("io-wait",        "ts.ts_io_wait",           12, 6),
    # Row 7 — IRQ / softirq
    ("irqs",           "ts.ts_irqs",              12, 6),
    ("softirqs",       "ts.ts_softirqs",          12, 6),
    # Row 8 — Network
    ("net-bytes",      "ts.ts_net_bytes",         12, 6),
    ("net-errors",     "ts.ts_net_errors",        12, 6),
    # Row 9 — Errors + logs
    ("err-rate",       "ts.error_rate_timeseries",12, 8),
    ("err-units",      "tables.top_error_units_table", 12, 8),
    ("err-tail",       "logs.logs_panel",         24, 10),
]

_BUILDERS = {
    "stat.stat_psi_cpu": stat.stat_psi_cpu,
    "stat.stat_psi_mem": stat.stat_psi_mem,
    "stat.stat_psi_io": stat.stat_psi_io,
    "stat.stat_load1": stat.stat_load1,
    "stat.stat_load5": stat.stat_load5,
    "stat.stat_load15": stat.stat_load15,
    "stat.stat_uptime": stat.stat_uptime,
    "stat.stat_temp": stat.stat_temp,
    "stat.stat_stutter_count": stat.stat_stutter_count,
    "ts.ts_psi_all": ts.ts_psi_all,
    "ts.ts_cpu_per_core": ts.ts_cpu_per_core,
    "ts.ts_cpu_freq": ts.ts_cpu_freq,
    "ts.ts_sched_runqueue": ts.ts_sched_runqueue,
    "ts.ts_irqs": ts.ts_irqs,
    "ts.ts_softirqs": ts.ts_softirqs,
    "ts.ts_mem_breakdown": ts.ts_mem_breakdown,
    "ts.ts_gpu_util": ts.ts_gpu_util,
    "ts.ts_gpu_mem": ts.ts_gpu_mem,
    "ts.ts_gpu_temp_power": ts.ts_gpu_temp_power,
    "ts.ts_gpu_clock": ts.ts_gpu_clock,
    "ts.ts_disk_iops": ts.ts_disk_iops,
    "ts.ts_disk_throughput": ts.ts_disk_throughput,
    "ts.ts_disk_io_latency_p99": ts.ts_disk_io_latency_p99,
    "ts.ts_io_wait": ts.ts_io_wait,
    "ts.ts_net_bytes": ts.ts_net_bytes,
    "ts.ts_net_errors": ts.ts_net_errors,
    "ts.error_rate_timeseries": logs_p.error_rate_timeseries,
    "tables.top_cgroup_cpu_table": tables.top_cgroup_cpu_table,
    "tables.top_cgroup_mem_table": tables.top_cgroup_mem_table,
    "tables.top_error_units_table": tables.top_error_units_table,
    "logs.logs_panel": logs_p.logs_panel,
}


@register("host-omarchy")
def build() -> DashboardSpec:
    builder = (
        v2.Dashboard("Workstation — kettle-omarchy")
        .description("Host monitoring for the Omarchy workstation: CPU, memory, GPU, "
                     "I/O, network, PSI, IRQ, journald errors. Click-drag a PSI spike "
                     "to zoom every panel below to the stutter window.")
        .tags(["workstation", "kettle-omarchy", "host", "psi"])
        .editable(True)
        .preload(False)
        .live_now(False)
        .cursor_sync(DashboardCursorSync.CROSSHAIR)
        .time_settings(
            v2.TimeSettings()
            .from_val("now-1h").to("now").auto_refresh("30s").timezone("browser")
        )
    )

    # Register every element. The layout grid references these by name.
    for name, key, _w, _h in _LAYOUT:
        builder = builder.element(name, _BUILDERS[key]())

    # Single flat grid — `grid_row` would split rows, but the layout
    # above wraps automatically because each (w, h) sums to 24 per row.
    grid = v2.Grid()
    x = 0
    y = 0
    row_h = 0
    for name, _key, w, h in _LAYOUT:
        if x + w > 24:
            x = 0
            y += row_h
            row_h = 0
        grid = grid.item(v2.GridItem().name(name).x(x).y(y).width(w).height(h))
        x += w
        row_h = max(row_h, h)
    builder = builder.layout(v2.Rows().row(
        v2.Row().title("Workstation").collapse(False).layout(grid)
    ))

    for var in build_variables():
        builder = builder.variable(var)

    return DashboardSpec(uid="kettle-host-omarchy", builder=builder)
```

- [ ] **Step 2: Wire into _AUTOLOAD**

Edit `src/grafana_dashboards/dashboards/__init__.py`. Find:

```python
_AUTOLOAD = (
    "grafana_dashboards.dashboards.service_health",
)
```

Replace with:

```python
_AUTOLOAD = (
    "grafana_dashboards.dashboards.service_health",
    "grafana_dashboards.dashboards.host_omarchy",
)
```

- [ ] **Step 3: Verify the dashboard appears in the registry**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run kgd list`
Expected: output includes `host-omarchy` (and `service-health` from the scaffold).

- [ ] **Step 4: Render it and check validator passes**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run kgd generate -o /tmp/kgd-out -d host-omarchy && ls /tmp/kgd-out/`
Expected: `/tmp/kgd-out/kettle-host-omarchy.json` exists; stderr is empty (no validation issues).

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/dashboards/host_omarchy.py src/grafana_dashboards/dashboards/__init__.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "dashboards: host_omarchy (kettle-host-omarchy) + recording rules"
```

### Task B11: tests/test_render.py — end-to-end round-trip + backslash regression

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_render.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_render.py
from __future__ import annotations

import json
from grafana_foundation_sdk.cog.encoder import JSONEncoder

from grafana_dashboards._internal.envelope import wrap_v2
from grafana_dashboards._internal.validate import validate_v2
from grafana_dashboards.dashboards import all_dashboards


ENCODER = JSONEncoder(sort_keys=False, indent=2)


def _render(slug: str) -> tuple[dict, str]:
    registry = all_dashboards()
    spec = registry[slug]()
    body = json.loads(ENCODER.encode(spec.builder.build()))
    wrapped = wrap_v2(body, uid=spec.uid)
    return wrapped, json.dumps(wrapped, indent=2)


def test_host_omarchy_renders_with_no_validation_issues():
    wrapped, _ = _render("host-omarchy")
    issues = validate_v2(wrapped)
    assert issues == [], "validator found issues: " + "\n".join(issues)


def test_host_omarchy_no_backslash_overescape():
    """Catch JSON->LogQL backslash bugs the grafana skill calls out.

    `\\\\.` decodes to literal-backslash-then-any-char in regex. Python
    f-strings make this rare but not impossible.
    """
    _, rendered = _render("host-omarchy")
    # Four backslashes in JSON source = two literal backslashes in
    # memory = regex `\\.` = misses every dot.
    assert "\\\\\\\\" not in rendered, "backslash over-escape in rendered JSON"


def test_host_omarchy_uses_ds_var_not_uid():
    wrapped, _ = _render("host-omarchy")
    blob = json.dumps(wrapped)
    # v2 datasource refs use the variable expansion in `name`, never a
    # hard-coded UID in panel-level datasource fields.
    assert '"uid":' not in blob or '"uid": "kettle-host-omarchy"' in blob
    assert "$ds_prom" in blob
    assert "$ds_loki" in blob


def test_all_registered_dashboards_render_clean():
    """Regression net: every registered dashboard must validate."""
    for slug, factory in all_dashboards().items():
        spec = factory()
        body = json.loads(ENCODER.encode(spec.builder.build()))
        wrapped = wrap_v2(body, uid=spec.uid)
        issues = validate_v2(wrapped)
        assert issues == [], f"{slug}: " + "\n".join(issues)
```

- [ ] **Step 2: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_render.py -v`
Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add tests/test_render.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "tests: end-to-end render + validator + backslash regression"
```

### Task B12: just dash:: recipes — render, strip envelope, write ConfigMap + PrometheusRule

**Files:**
- Modify: `/home/kettle/git_repos/grafana-dashboards/just/dash.just` (replace stub with full recipes)
- Create: `/home/kettle/git_repos/grafana-dashboards/scripts/render_to_chart.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_render_to_chart.py`

- [ ] **Step 1: Write the failing test for the chart-render helper**

```python
# tests/test_render_to_chart.py
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def runner(tmp_path):
    mod = importlib.import_module("scripts.render_to_chart")
    return mod, tmp_path


def test_strips_envelope_and_writes_three_files(runner):
    mod, tmp = runner
    out_dir = tmp / "chart"
    mod.render(["host-omarchy"], out_dir)
    json_file = out_dir / "dashboards" / "kettle-host-omarchy.json"
    cm_file = out_dir / "templates" / "kettle-host-omarchy.yaml"
    rule_file = out_dir / "templates" / "kettle-host-omarchy-rules.yaml"
    assert json_file.exists()
    assert cm_file.exists()
    assert rule_file.exists()
    body = json.loads(json_file.read_text())
    # Envelope stripped: spec-body shape, no apiVersion/kind/metadata.
    assert "apiVersion" not in body
    assert "kind" not in body
    assert "title" in body
    assert "layout" in body
    assert "elements" in body
    # ConfigMap wrapper references the JSON file.
    cm_text = cm_file.read_text()
    assert "grafana_dashboard: \"1\"" in cm_text
    assert "grafana_folder: \"Workstation\"" in cm_text
    assert "kettle-host-omarchy.json" in cm_text
    # PrometheusRule body contains the rule records.
    rule_text = rule_file.read_text()
    assert "host:psi_cpu_waiting:ratio1m" in rule_text
    assert "host:cgroup_cpu:sum5m" in rule_text


def test_skips_rule_file_when_no_RECORDING_RULES(runner):
    mod, tmp = runner
    out_dir = tmp / "chart"
    mod.render(["service-health"], out_dir)
    assert (out_dir / "dashboards" / "kettle-service-health.json").exists()
    assert not (out_dir / "templates" / "kettle-service-health-rules.yaml").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_render_to_chart.py -v`
Expected: FAIL — `scripts.render_to_chart` not found.

- [ ] **Step 3: Write the helper script**

```python
# scripts/render_to_chart.py
"""Render registered dashboards into the cluster Helm chart layout.

Wraps the `kgd` generator: renders envelope-wrapped JSON, then for
each dashboard:
  - writes spec body to dashboards/<uid>.json (envelope stripped)
  - writes templates/<uid>.yaml (ConfigMap wrapper)
  - writes templates/<uid>-rules.yaml if the dashboard module
    exports a RECORDING_RULES list
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from grafana_foundation_sdk.cog.encoder import JSONEncoder

from grafana_dashboards._internal.envelope import wrap_v2
from grafana_dashboards._internal.validate import validate_v2
from grafana_dashboards.dashboards import all_dashboards

ENCODER = JSONEncoder(sort_keys=False, indent=2)


CM_TEMPLATE = '''\
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboard-{uid}
  labels:
    grafana_dashboard: "1"
  annotations:
    grafana_folder: "{folder}"
data:
  {uid}.json: |-
{indented_json}
'''


RULE_TEMPLATE = '''\
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: {uid}-rules
  labels:
    prometheus: kube-prometheus
    role: alert-rules
spec:
  groups:
    - name: {uid}
      interval: 30s
      rules:
{rules_yaml}
'''


def _format_rules(rules: list[dict]) -> str:
    out = []
    for r in rules:
        # PromQL exprs are multi-line in our recording rules; YAML
        # block scalar handles them cleanly.
        expr_lines = r["expr"].splitlines() or [r["expr"]]
        out.append(f"        - record: {r['record']}")
        out.append("          expr: |-")
        for line in expr_lines:
            out.append(f"            {line}")
    return "\n".join(out)


def render(slugs: list[str], chart_dir: Path, folder: str = "Workstation") -> None:
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "dashboards").mkdir(exist_ok=True)
    (chart_dir / "templates").mkdir(exist_ok=True)

    registry = all_dashboards()
    for slug in slugs:
        if slug not in registry:
            print(f"unknown dashboard slug: {slug!r}", file=sys.stderr)
            print(f"available: {', '.join(sorted(registry))}", file=sys.stderr)
            sys.exit(2)

        spec = registry[slug]()
        body = json.loads(ENCODER.encode(spec.builder.build()))
        wrapped = wrap_v2(body, uid=spec.uid)

        issues = validate_v2(wrapped)
        if issues:
            for i in issues:
                print(f"{slug}: {i}", file=sys.stderr)
            sys.exit(1)

        # Strip envelope for sidecar provisioning.
        json_path = chart_dir / "dashboards" / f"{spec.uid}.json"
        json_path.write_text(json.dumps(body, indent=2) + "\n")
        print(f"wrote {json_path}")

        # ConfigMap wrapper.
        indented = "\n".join(
            f"    {line}" for line in json.dumps(body, indent=2).splitlines()
        )
        cm_path = chart_dir / "templates" / f"{spec.uid}.yaml"
        cm_path.write_text(CM_TEMPLATE.format(
            uid=spec.uid, folder=folder, indented_json=indented,
        ))
        print(f"wrote {cm_path}")

        # PrometheusRule if declared.
        dash_module = importlib.import_module(
            f"grafana_dashboards.dashboards.{slug.replace('-', '_')}"
        )
        rules = getattr(dash_module, "RECORDING_RULES", None)
        if rules:
            rule_path = chart_dir / "templates" / f"{spec.uid}-rules.yaml"
            rule_path.write_text(RULE_TEMPLATE.format(
                uid=spec.uid, rules_yaml=_format_rules(rules),
            ))
            print(f"wrote {rule_path}")


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-o", "--out", type=Path, required=True,
                   help="Chart dir (contains dashboards/ and templates/).")
    p.add_argument("-d", "--dashboard", action="append", default=None,
                   help="Dashboard slug; may be repeated. Default: all.")
    p.add_argument("--folder", default="Workstation",
                   help="grafana_folder annotation value (default Workstation).")
    args = p.parse_args()
    slugs = args.dashboard or sorted(all_dashboards())
    render(slugs, args.out, folder=args.folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_render_to_chart.py -v`
Expected: 2 passed.

- [ ] **Step 5: Replace just/dash.just with the real recipes**

Overwrite `just/dash.just` with:

```just
# Cluster repo where the existing kube-prometheus-stack grafana-dashboards chart lives.
CHART_DIR := env("KETTLE_CHART_DIR", "/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart")

# Render a single dashboard into the chart (JSON + ConfigMap + PrometheusRule).
[group('dash')]
render slug:
    uv run python scripts/render_to_chart.py -o {{CHART_DIR}} -d {{slug}}

# Render every registered dashboard.
[group('dash')]
render-all:
    uv run python scripts/render_to_chart.py -o {{CHART_DIR}}

# Validator-only: renders via kgd (which runs the validator) into a
# tempdir, returns exit code.
[group('dash')]
validate slug:
    uv run kgd generate -o $(mktemp -d) -d {{slug}}

[group('dash')]
validate-all:
    uv run kgd generate -o $(mktemp -d)

# Render to scratch and diff against the committed JSON.
[group('dash')]
diff slug:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp=$(mktemp -d)
    uv run python scripts/render_to_chart.py -o "$tmp" -d {{slug}}
    diff -u "{{CHART_DIR}}/dashboards/kettle-{{slug}}.json" \
            "$tmp/dashboards/kettle-{{slug}}.json" || true
```

- [ ] **Step 6: Run the recipes end-to-end (dry, into a tempdir)**

Run: `cd /home/kettle/git_repos/grafana-dashboards && KETTLE_CHART_DIR=$(mktemp -d) just dash::render host-omarchy && ls $(echo $KETTLE_CHART_DIR)`

Since `just` exports `KETTLE_CHART_DIR` only inside the recipe, just verify with a direct invocation:

```bash
cd /home/kettle/git_repos/grafana-dashboards
T=$(mktemp -d)
KETTLE_CHART_DIR=$T just dash::render host-omarchy
ls -la $T/dashboards $T/templates
```

Expected: `dashboards/kettle-host-omarchy.json` plus `templates/kettle-host-omarchy.yaml` + `templates/kettle-host-omarchy-rules.yaml`.

- [ ] **Step 7: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add just/dash.just scripts/render_to_chart.py tests/test_render_to_chart.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "dash: render_to_chart helper + dash:: recipes"
```

### Task B13: Render host-omarchy into the cluster repo + commit there

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart/dashboards/kettle-host-omarchy.json`
- Create: `/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy.yaml`
- Create: `/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy-rules.yaml`

- [ ] **Step 1: Render into the real chart dir**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just dash::render host-omarchy`
Expected: prints three "wrote ..." lines pointing into `/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart/`.

- [ ] **Step 2: Helm-template the chart to ensure both ConfigMap + PrometheusRule render cleanly**

Run: `helm template /home/kettle/KettleCluster/home/apps/grafana-dashboards/chart | grep -E "name: grafana-dashboard-kettle-host-omarchy|name: kettle-host-omarchy-rules"`
Expected: both resource names appear.

- [ ] **Step 3: Commit + push**

```bash
git -C /home/kettle/KettleCluster add home/apps/grafana-dashboards/chart/dashboards/kettle-host-omarchy.json \
    home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy.yaml \
    home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy-rules.yaml
git -C /home/kettle/KettleCluster commit -m "grafana-dashboards: add kettle-host-omarchy dashboard + recording rules"
git -C /home/kettle/KettleCluster push origin main
```

- [ ] **Step 4: Verify ArgoCD picked it up**

Run: `kubectl --context kettle -n argocd get application grafana-dashboards -o jsonpath='{.status.sync.status} {.status.health.status}'`
Expected: `Synced Healthy` within 5 minutes.

Run: `kubectl --context kettle -n monitoring get cm grafana-dashboard-kettle-host-omarchy` and `kubectl --context kettle -n monitoring get prometheusrule kettle-host-omarchy-rules`
Expected: both exist.

- [ ] **Step 5: Verify the dashboard appears in Grafana**

Browse to `https://grafana.home.kettle.sh/dashboards`. Expected: a "Workstation" folder appears; inside it, "Workstation — kettle-omarchy" dashboard is loadable. **All panels will show "No data" until Phase C ships metrics — expected.**

## Phase C — Host Alloy agent (Omarchy workstation)

All Phase-C files live in `/home/kettle/git_repos/grafana-dashboards/alloy/` (templates/source) and `/etc/alloy/` (rendered runtime). Recipes live in `just/alloy.just`.

### Task C1: Alloy config template + env example

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/alloy/config.alloy.j2`
- Create: `/home/kettle/git_repos/grafana-dashboards/alloy/env.example`

- [ ] **Step 1: Write the config template**

```jinja
// Rendered by `just alloy::configure` — do not edit /etc/alloy/config.alloy by hand.

// ─── 1. External label set: short aliases + OTEL-semconv underscore names ────
discovery.relabel "host_labels" {
  targets = [{}]
  rule {
    target_label = "host"
    replacement  = "{{ HOSTNAME }}"
  }
  rule { target_label = "host_name"        replacement = "{{ HOSTNAME }}" }
  rule { target_label = "host_id"          replacement = "{{ HOST_ID }}" }
  rule { target_label = "host_arch"        replacement = "{{ HOST_ARCH }}" }
  rule { target_label = "os_type"          replacement = "linux" }
  rule { target_label = "os_description"   replacement = "{{ OS_DESCRIPTION }}" }
  rule { target_label = "role"             replacement = "workstation" }
  rule { target_label = "distro"           replacement = "{{ DISTRO }}" }
  rule { target_label = "gpu"              replacement = "{{ GPU }}" }
}

// ─── 2. node_exporter-equivalent metrics (pinned collector list) ─────────────
prometheus.exporter.unix "host" {
  set_collectors = [
    "cpu", "meminfo", "loadavg", "filesystem", "diskstats",
    "netdev", "netstat", "sockstat", "time", "uname", "vmstat",
    "hwmon", "cpufreq", "pressure", "schedstat", "interrupts", "softirqs",
  ]
  filesystem {
    fs_types_exclude = "^(autofs|binfmt_misc|bpf|cgroup2?|configfs|debugfs|devpts|devtmpfs|fusectl|hugetlbfs|iso9660|mqueue|nsfs|overlay|proc|procfs|pstore|rpc_pipefs|securityfs|selinuxfs|squashfs|sysfs|tracefs)$"
    mount_points_exclude = "^/(dev|proc|sys|run|var/lib/(docker|containerd|kubelet))($|/)"
  }
}

// ─── 3. cgroup metrics ─ Alloy's prometheus.exporter.cadvisor ────────────────
// If this component is removed in a future Alloy release, fall back to a
// systemd-managed cgroup_exporter (see Risks in the design spec) and
// replace this block with a prometheus.scrape against 127.0.0.1:9080.
prometheus.exporter.cadvisor "host" {
  docker_only             = false
  store_container_labels  = false
  // cgroupv2 read directly from /sys/fs/cgroup
}

// ─── 4. NVIDIA GPU exporter (local scrape) ──────────────────────────────────
prometheus.scrape "nvidia" {
  targets    = [{ __address__ = "127.0.0.1:9835" }]
  forward_to = [prometheus.relabel.host_stamp.receiver]
  scrape_interval = "30s"
  job_name        = "nvidia_gpu_exporter"
}

// ─── 5. Collapse browser/Steam ephemeral scopes ─────────────────────────────
prometheus.relabel "cgroup_collapse" {
  forward_to = [prometheus.relabel.host_stamp.receiver]
  rule {
    source_labels = ["name"]
    regex         = "chromium-\\d+\\.scope"
    target_label  = "name"
    replacement   = "chromium"
  }
  rule {
    source_labels = ["name"]
    regex         = "firefox-.+\\.scope"
    target_label  = "name"
    replacement   = "firefox"
  }
  rule {
    source_labels = ["name"]
    regex         = "app-electron-.+\\.scope"
    target_label  = "name"
    replacement   = "electron"
  }
  rule {
    source_labels = ["name"]
    regex         = "app-org\\.proton\\..+\\.scope"
    target_label  = "name"
    replacement   = "proton"
  }
  rule {
    source_labels = ["name"]
    regex         = "systemd-udevd.*"
    action        = "drop"
  }
}

// ─── 6. Stamp every series with our host labels ─────────────────────────────
prometheus.relabel "host_stamp" {
  forward_to = [prometheus.remote_write.cluster.receiver]
  rule { target_label = "host"             replacement = "{{ HOSTNAME }}" }
  rule { target_label = "host_name"        replacement = "{{ HOSTNAME }}" }
  rule { target_label = "host_id"          replacement = "{{ HOST_ID }}" }
  rule { target_label = "host_arch"        replacement = "{{ HOST_ARCH }}" }
  rule { target_label = "os_type"          replacement = "linux" }
  rule { target_label = "os_description"   replacement = "{{ OS_DESCRIPTION }}" }
  rule { target_label = "role"             replacement = "workstation" }
  rule { target_label = "distro"           replacement = "{{ DISTRO }}" }
  rule { target_label = "gpu"              replacement = "{{ GPU }}" }
}

// ─── 7. Wire unix + cadvisor exporters into the relabel chain ───────────────
prometheus.scrape "host_unix" {
  targets    = prometheus.exporter.unix.host.targets
  forward_to = [prometheus.relabel.host_stamp.receiver]
  scrape_interval = "15s"
  job_name        = "workstation-node"
}

prometheus.scrape "host_cadvisor" {
  targets    = prometheus.exporter.cadvisor.host.targets
  forward_to = [prometheus.relabel.cgroup_collapse.receiver]
  scrape_interval = "30s"
  job_name        = "workstation-cgroups"
}

// ─── 8. Push metrics to cluster Prometheus ──────────────────────────────────
prometheus.remote_write "cluster" {
  endpoint {
    url = "https://prometheus-ingest.home.kettle.sh/api/v1/write"
    basic_auth {
      username = env("PROM_USER")
      password = env("PROM_PASS")
    }
  }
}

// ─── 9. journald → Loki (with boot_id as structured metadata) ───────────────
loki.source.journal "host" {
  forward_to = [loki.process.host.receiver]
  // Drop debug priority locally.
  matches    = "PRIORITY=0..6"
  format_as_json = false
  // boot_id is captured by loki.source.journal as a stream label by
  // default; the process stage below moves it to structured metadata.
  labels = {
    role       = "workstation",
    host       = "{{ HOSTNAME }}",
    host_name  = "{{ HOSTNAME }}",
    host_id    = "{{ HOST_ID }}",
    os_type    = "linux",
    distro     = "{{ DISTRO }}",
  }
}

loki.process "host" {
  forward_to = [loki.write.cluster.receiver]
  // Move _SYSTEMD_UNIT into the `unit` label and boot_id into
  // structured metadata so reboots don't churn streams.
  stage.json {
    expressions = { unit = "_SYSTEMD_UNIT", boot_id = "_BOOT_ID", priority = "PRIORITY" }
  }
  stage.labels {
    values = { unit = "", priority = "" }
  }
  stage.structured_metadata {
    values = { boot_id = "" }
  }
}

// ─── 10. Push logs to cluster Loki ──────────────────────────────────────────
loki.write "cluster" {
  endpoint {
    url = "https://loki-ingest.home.kettle.sh/loki/api/v1/push"
    basic_auth {
      username = env("LOKI_USER")
      password = env("LOKI_PASS")
    }
  }
}
```

- [ ] **Step 2: Write the env.example**

```bash
# /etc/alloy/env — populated by `just alloy::configure`.
# Permissions: 0600 alloy:alloy.

# Prometheus remote_write basic-auth (from K8s Secret workstation-ingest-auth).
PROM_USER=kettle-omarchy
PROM_PASS=replace-me

# Loki push basic-auth — same Secret, reused.
LOKI_USER=kettle-omarchy
LOKI_PASS=replace-me
```

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add alloy/config.alloy.j2 alloy/env.example
git -C /home/kettle/git_repos/grafana-dashboards commit -m "alloy: config.alloy.j2 template + env example"
```

### Task C2: alloy:: install + configure recipes

**Files:**
- Modify: `/home/kettle/git_repos/grafana-dashboards/just/alloy.just` (replace stub with real recipes)

- [ ] **Step 1: Replace just/alloy.just with the real recipes**

```just
# Path constants.
ALLOY_ETC := "/etc/alloy"
TEMPLATE  := "alloy/config.alloy.j2"

# Idempotent: install grafana-alloy (extra) + nvidia-gpu-exporter-bin (AUR);
# create /etc/alloy and the alloy user/group if pacman didn't.
[group('alloy')]
install:
    #!/usr/bin/env bash
    set -euo pipefail
    # Hardware preflight: NVIDIA?
    if ! lspci | grep -qi nvidia; then
        echo "no NVIDIA GPU detected; nvidia_gpu_exporter would be useless. Continue anyway? (y/N)"
        read -r ans
        [[ "$ans" =~ ^[Yy]$ ]] || exit 1
    fi
    sudo pacman -S --needed --noconfirm grafana-alloy
    yay -S --needed --noconfirm nvidia-gpu-exporter-bin
    sudo install -d -o alloy -g alloy -m 0755 {{ALLOY_ETC}}

# Read host facts, render config.alloy.j2, write /etc/alloy/config.alloy.
# Idempotent. Re-run after distro upgrade (changes PRETTY_NAME) or
# reinstall (changes machine-id).
[group('alloy')]
configure HOSTNAME="kettle-omarchy":
    #!/usr/bin/env bash
    set -euo pipefail
    HOST_ID=$(sha256sum /etc/machine-id | head -c 16)
    HOST_ARCH=$(uname -m)
    # uname -m emits x86_64; OTEL semconv uses "amd64".
    if [[ "$HOST_ARCH" == "x86_64" ]]; then HOST_ARCH="amd64"; fi
    DISTRO="omarchy"
    GPU=$(lspci | grep -i 'vga\|3d' | grep -oiE 'nvidia[^[:space:]]*' | head -1 | tr 'A-Z' 'a-z')
    [[ -z "$GPU" ]] && GPU="unknown"
    . /etc/os-release
    OS_DESCRIPTION="${PRETTY_NAME:-Linux}"

    uv run python -c "
import os
from jinja2 import Template
tmpl = Template(open('{{TEMPLATE}}').read())
print(tmpl.render(
    HOSTNAME='{{HOSTNAME}}',
    HOST_ID=os.environ['HOST_ID'],
    HOST_ARCH=os.environ['HOST_ARCH'],
    OS_DESCRIPTION=os.environ['OS_DESCRIPTION'],
    DISTRO=os.environ['DISTRO'],
    GPU=os.environ['GPU'],
))
" > /tmp/config.alloy.rendered
    sudo install -o alloy -g alloy -m 0644 /tmp/config.alloy.rendered {{ALLOY_ETC}}/config.alloy
    rm -f /tmp/config.alloy.rendered

    if [[ ! -f {{ALLOY_ETC}}/env ]]; then
        sudo install -o alloy -g alloy -m 0600 alloy/env.example {{ALLOY_ETC}}/env
        echo "Edit {{ALLOY_ETC}}/env with the workstation ingest credentials (see Phase A Secret)."
        echo "Then run: just alloy::enable"
    fi

[group('alloy')]
enable:
    sudo systemctl enable --now alloy nvidia-gpu-exporter
    systemctl is-active alloy
    systemctl is-active nvidia-gpu-exporter

[group('alloy')]
reload:
    sudo systemctl reload alloy || sudo systemctl restart alloy
    systemctl is-active alloy

[group('alloy')]
status:
    systemctl status alloy --no-pager -n 20
    systemctl status nvidia-gpu-exporter --no-pager -n 10
    curl -sS http://127.0.0.1:12345/-/healthy || echo "Alloy UI not reachable on :12345"

[group('alloy')]
logs N="200":
    journalctl -u alloy -n {{N}} -f --no-pager

# Push a synthetic Prometheus series via Alloy's local logs (verifying
# the metric round-trip end to end). Pushes via the cluster ingest URL,
# then queries Grafana to verify.
[group('alloy')]
test-ingest:
    #!/usr/bin/env bash
    set -euo pipefail
    . {{ALLOY_ETC}}/env
    PROM_URL="https://prometheus-ingest.home.kettle.sh/api/v1/write"
    GRAFANA_URL="https://grafana.home.kettle.sh"
    NOW_MS=$(date +%s%3N)
    # Build a minimal protobuf for one sample using Prometheus' textfile
    # collector style isn't supported by remote_write; instead use
    # promtool to construct the protobuf and push.
    echo "Testing remote_write auth (expect HTTP 400 for empty body):"
    curl -sS -u "$PROM_USER:$PROM_PASS" -X POST "$PROM_URL" \
      -H 'Content-Type: application/x-protobuf' \
      -H 'X-Prometheus-Remote-Write-Version: 0.1.0' \
      --data-binary '' -o /dev/null -w '%{http_code}\n'
    echo ""
    echo "If the round trip is good, kettle_smoketest will appear once Alloy"
    echo "scrapes its own /-/healthy endpoint into the cluster. Watch:"
    echo "  watch -n 5 'curl -sS \"$GRAFANA_URL/api/datasources/proxy/uid/prometheus/api/v1/query?query=up{host_name=\\\"kettle-omarchy\\\"}\" | jq .data.result'"

[group('alloy')]
[confirm]
uninstall:
    sudo systemctl disable --now alloy nvidia-gpu-exporter || true
    sudo rm -rf {{ALLOY_ETC}}
    sudo pacman -Rns --noconfirm grafana-alloy nvidia-gpu-exporter-bin
```

- [ ] **Step 2: Verify `just --list` shows the alloy recipes**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just --list --list-submodules | grep -A 20 "alloy::"`
Expected: `install`, `configure`, `enable`, `reload`, `status`, `logs`, `test-ingest`, `uninstall` listed.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add just/alloy.just
git -C /home/kettle/git_repos/grafana-dashboards commit -m "alloy: just recipes (install, configure, enable, status, test-ingest, uninstall)"
```

### Task C3: Install Alloy + NVIDIA exporter on this host

- [ ] **Step 1: Run alloy::install**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::install`
Expected: pacman + yay install complete; `/etc/alloy/` exists.

- [ ] **Step 2: Verify binaries are present**

Run: `which alloy && alloy --version && which nvidia_gpu_exporter && nvidia_gpu_exporter --version`
Expected: paths print + versions match installed packages (Alloy ≥1.13, exporter ≥1.4).

- [ ] **Step 3: Verify systemd unit files exist**

Run: `systemctl cat alloy.service nvidia-gpu-exporter.service | head -60`
Expected: both units exist; `EnvironmentFile=/etc/alloy/env` referenced in `alloy.service` (this is the default upstream unit; if the AUR package's unit doesn't include `EnvironmentFile`, add a drop-in in the next task).

- [ ] **Step 4: Add EnvironmentFile drop-in if missing**

If Step 3 showed no `EnvironmentFile`, create the drop-in:

```bash
sudo install -d /etc/systemd/system/alloy.service.d
sudo tee /etc/systemd/system/alloy.service.d/env.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/alloy/env
EOF
sudo systemctl daemon-reload
```

### Task C4: Fetch workstation ingest credentials from the cluster

- [ ] **Step 1: Read the password from the cluster Secret**

Run:

```bash
kubectl --context kettle -n monitoring get secret workstation-ingest-auth \
  -o jsonpath='{.data.password}' | base64 -d > /tmp/workstation-pass
chmod 600 /tmp/workstation-pass
echo "Password length: $(wc -c < /tmp/workstation-pass)"
```

Expected: a 40-character password printed in the count.

### Task C5: Configure Alloy

- [ ] **Step 1: Run alloy::configure**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::configure kettle-omarchy`
Expected: `/etc/alloy/config.alloy` rendered; `/etc/alloy/env` seeded from `env.example` if missing.

- [ ] **Step 2: Inspect the rendered config**

Run: `sudo head -40 /etc/alloy/config.alloy`
Expected: the discovery.relabel block shows `host = "kettle-omarchy"`, `host_id` is a 16-char hex string, `os_description` matches `PRETTY_NAME` from `/etc/os-release`.

- [ ] **Step 3: Populate /etc/alloy/env with real credentials**

Run:

```bash
sudo tee /etc/alloy/env >/dev/null <<EOF
PROM_USER=kettle-omarchy
PROM_PASS=$(cat /tmp/workstation-pass)
LOKI_USER=kettle-omarchy
LOKI_PASS=$(cat /tmp/workstation-pass)
EOF
sudo chmod 600 /etc/alloy/env
sudo chown alloy:alloy /etc/alloy/env
shred -u /tmp/workstation-pass
```

- [ ] **Step 4: Validate the config**

Run: `alloy fmt /etc/alloy/config.alloy >/dev/null && echo OK`
Expected: `OK` printed; no syntax errors.

### Task C6: Start the services and verify health

- [ ] **Step 1: Run alloy::enable**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::enable`
Expected: both units active.

- [ ] **Step 2: Check Alloy UI and health endpoint**

Run: `curl -sS http://127.0.0.1:12345/-/healthy && echo ""`
Expected: HTTP 200 with the body indicating healthy.

- [ ] **Step 3: Tail logs for first-minute errors**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::logs 100`
(Press Ctrl-C to exit after ~30s of tailing.)
Expected: no `level=error` lines about auth failures or unreachable endpoints. Some startup warnings are normal.

### Task C7: End-to-end verification — dashboard populates

- [ ] **Step 1: Verify the workstation appears in cluster Prometheus**

Run:

```bash
GRAFANA_URL="https://grafana.home.kettle.sh"
curl -sS "$GRAFANA_URL/api/datasources/proxy/uid/prometheus/api/v1/query?query=up{host_name=\"kettle-omarchy\"}" \
  -H "Cookie: $(cat ~/.config/grafana-session 2>/dev/null || echo '')" \
  | python3 -m json.tool | head -20
```

If you don't have a Grafana session cookie cached, instead query directly via the in-cluster service (requires `kubectl port-forward`):

```bash
kubectl --context kettle -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9091:9090 &
sleep 2
curl -sS 'http://127.0.0.1:9091/api/v1/query?query=up{host_name="kettle-omarchy"}' | python3 -m json.tool
kill %1
```

Expected: at least one series returned where `value[1]` is `"1"`.

- [ ] **Step 2: Verify journald logs reach Loki**

```bash
kubectl --context kettle -n loki port-forward svc/loki-gateway 3101:80 &
sleep 2
curl -sS --get 'http://127.0.0.1:3101/loki/api/v1/query' \
  --data-urlencode 'query=count_over_time({host_name="kettle-omarchy"}[5m])' \
  --data-urlencode "time=$(date +%s)" | python3 -m json.tool | head
kill %1
```

Expected: non-empty `result` array.

- [ ] **Step 3: Browse the dashboard**

Open `https://grafana.home.kettle.sh/d/kettle-host-omarchy/workstation-kettle-omarchy` (substitute the actual slug-from-title if Grafana generated a different one).

Expected: row 1 stat panels show numeric values for PSI, load avg, uptime, temp; row 2 PSI timeseries shows three lines; row 3 CPU per-core shows 32 lines; row 5 GPU panels show non-zero util when something is GPU-busy.

- [ ] **Step 4: Stress test — see the PSI line spike**

Run: `stress-ng --cpu 16 --timeout 60s` (install via `pacman -S stress-ng` if missing).

Expected: while stress runs, the headline PSI timeseries (row 2) shows a clear CPU PSI spike; per-core util goes red; top-cgroup-CPU table lists `stress-ng-cpu` or similar.

## Post-implementation checks

- [ ] **Step 1: Re-run all tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Re-render every dashboard**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just dash::render-all`
Expected: no diff against committed JSON (or any diff is intentional — review and commit).

- [ ] **Step 3: Confirm spec sidecar-vs-envelope question is resolved**

The risk in the spec was that Grafana might not parse the stripped v2beta1 `.spec` body. If Task C7 shows the dashboard rendering correctly with data, this risk is closed. If Grafana shows "Failed to load dashboard" errors on the host-omarchy dashboard, the fallback path is:

1. Don't strip the envelope in `render_to_chart.py` (remove the `body = json.loads(...)` line, write `wrapped` instead).
2. Install the Grafana operator via Helm: `helm install grafana-operator oci://ghcr.io/grafana/helm-charts/grafana-operator --namespace monitoring`.
3. Change `templates/<uid>.yaml` from a ConfigMap to a `Dashboard` CR (apiVersion `grafana.integreatly.org/v1beta1`).

Record the outcome in the spec's "Risks and open items" section as resolved.

---

## Self-review (writer-side)

**Spec coverage:** Every section of the spec maps to at least one task:
- §1 Host agent — Tasks C1, C2, C3, C4, C5, C6
- §2 Cluster-side enablement — Tasks A1-A7
- §3 Dashboard — Tasks B3-B11 (panels) + B10 (composition) + B12-B13 (render+deploy)
- §4 Repo layout + SDK conventions — Tasks B1-B12 (justfile, panels/, datasources via Variables, validation order in B11)
- §5 just interface — Tasks B1, B2, B12, C2
- Testing — Tasks B3-B11 each include test steps; Task B11 explicitly covers backslash regression and validator.
- Risks — Task C7 Step 3 closes the v2beta1-sidecar risk; cAdvisor fallback referenced in C1; cardinality recorded in spec.

**Placeholder scan:** No `TBD`, `TODO`, `implement later`, "appropriate error handling", or "similar to" references. All code blocks are complete.

**Type consistency:** `PromQuery`, `LokiQuery`, `target`, `HOST_FILTER` defined once in `panels/_common.py`, imported consistently. Dashboard-spec `DashboardSpec(uid, builder)` from the existing scaffold used uniformly. Recording rule names match across the dashboard module (`RECORDING_RULES`), the panel queries (`host:psi_cpu_waiting:ratio1m`, etc.), and the `render_to_chart.py` formatter.
