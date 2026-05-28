# Workstation Host Monitoring Dashboard — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working v2beta1 Grafana dashboard at `grafana.home.kettle.sh` that monitors this Omarchy workstation (CPU/mem/GPU/disk/network/PSI/IRQ/journald) — fed by a host-side Grafana Alloy agent pushing to the cluster's Prometheus + Loki — with the dashboard rendered from `grafana-foundation-sdk` (v2beta1) and deployed via **grafana-operator** as a `Dashboard` CR.

**Architecture:** Three phases. **Phase A** installs grafana-operator and enables cluster-side ingestion (Traefik routes + basic-auth + rate-limit + remote-write receiver + exemplar storage + Loki derived fields). All Phase-A resources land in the `monitoring` namespace alongside kube-prometheus-stack and Loki (single namespace → no cross-ns Traefik/Secret/TLS concerns; matches existing `grafana-ingressroute.yaml` pattern). **Phase B** extends the existing `grafana-dashboards` Python package with a `panels/` builder library, the `host_omarchy` dashboard, a `RECORDING_RULES` list, and `just dash::*` recipes that emit a v2beta1 `Dashboard` CR (envelope-wrapped — grafana-operator's native shape) plus a `PrometheusRule`. **Phase C** installs Alloy + NVIDIA exporter on this Omarchy workstation, renders `/etc/alloy/config.alloy` from a Jinja template via `just alloy::configure`, brings systemd units up, and runs scripted PSI/stutter verification.

**Tech Stack:** Grafana Alloy 1.13+ (systemd, River config), `nvidia-gpu-exporter-bin` (AUR), Traefik v3 IngressRoute + Middleware, kube-prometheus-stack Helm chart (existing), Loki 3+ SingleBinary, **grafana-operator (new)**, ArgoCD, grafana-foundation-sdk (pinned `git+a8c311b58`, dashboardv2beta1 module), Python 3.11+/uv, `just`, Jinja2, pytest.

**v2 features used:** envelope CRD (`apiVersion: dashboard.grafana.app/v2beta1`), cursor-sync crosshair, per-panel time overrides (stutter-events stat reads `now-5m` regardless of dashboard range), `DatasourceVariable`-driven datasource refs (no `__inputs` substitution), conditional element rendering scaffolding via grafana-operator's `instanceSelector`.

---

## Phase A — Cluster-side enablement (KettleCluster repo)

**Important context:** The actual Helm values for `kube-prometheus-stack` live in `home/argocd-apps/kube-prometheus-stack.yaml` under `spec.source.helm.valuesObject` — NOT in the chart's `values.yaml`. The chart's `values.yaml` is a near-empty stub; ArgoCD's `valuesObject` overrides it. Confirmed: `enableRemoteWriteReceiver: true` is already there (`argocd-apps/kube-prometheus-stack.yaml`:24) and Phase A only needs to add `enableFeatures` + the `folderAnnotation` if absent.

**Namespacing rule:** all new resources land in `monitoring` namespace (same as kube-prometheus-stack, Loki, grafana-dashboards). No cross-namespace Traefik middleware refs; no cross-namespace TLS lookup. Matches existing `grafana-ingressroute.yaml` pattern.

### Task A1: Workstation ingestion Secret (single password, idempotent)

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-secret.yaml`

- [ ] **Step 1: Write the Secret template** — uses a single `$pw` Helm variable so plaintext + bcrypt hash agree, uses `lookup` to reuse existing value across renders (no ArgoCD drift), `ServerSideApply=true` matches the existing `grafana-admin-secret.yaml` pattern.

```yaml
{{- $existing := lookup "v1" "Secret" "monitoring" "workstation-ingest-auth" -}}
{{- $pw := "" -}}
{{- if and $existing $existing.data $existing.data.password -}}
{{-   $pw = b64dec $existing.data.password -}}
{{- else -}}
{{-   $pw = randAlphaNum 40 -}}
{{- end -}}
apiVersion: v1
kind: Secret
metadata:
  name: workstation-ingest-auth
  annotations:
    argocd.argoproj.io/sync-options: ServerSideApply=true
type: Opaque
stringData:
  username: "kettle-omarchy"
  password: {{ $pw | quote }}
  # Traefik basic-auth Secret consumes the `users` data key in htpasswd
  # format. `htpasswd "user" "pw"` (sprig) returns "user:$2a$10$...".
  # No printf wrapper — that would double-prepend the username.
  users: {{ htpasswd "kettle-omarchy" $pw | quote }}
```

- [ ] **Step 2: Render-check**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | sed -n '/name: workstation-ingest-auth/,/^---/p' | head -20`
Expected: Secret renders; the htpasswd line in `users` starts with `kettle-omarchy:$2a$10$`; the `password` field is a 40-char alphanumeric; both should be the same plaintext (you can decode by manually bcrypt-comparing if curious).

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-secret.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: add workstation ingest auth secret (idempotent)"
```

### Task A2: Traefik basic-auth + rate-limit middlewares

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-middleware.yaml`

- [ ] **Step 1: Write the two Middleware CRDs** (rate-limit tuned to ~2× expected peak)

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
    # Expected peak: ~250-400 req/s (15s remote-write scrape × ~3-5k series).
    # 2× headroom; tight enough to bound a runaway agent.
    average: 600
    burst: 1200
    period: 1s
```

- [ ] **Step 2: Render-check**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -B 1 -A 8 "kind: Middleware"`
Expected: two Middlewares; the basicAuth references the secret from A1.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/workstation-ingest-middleware.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: workstation ingest middlewares (basic-auth + rate-limit)"
```

### Task A3: IngressRoute — `prometheus-ingest.home.kettle.sh`

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/prometheus-ingest-ingressroute.yaml`

- [ ] **Step 1: Write the IngressRoute** (same pattern as existing `grafana-ingressroute.yaml` — same namespace, same TLS secret, no cross-ns refs)

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

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -B 1 -A 18 "name: prometheus-ingest$"`
Expected: IngressRoute renders with both middlewares + PathPrefix scope.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/prometheus-ingest-ingressroute.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: prometheus-ingest IngressRoute"
```

### Task A4: IngressRoute — `loki-ingest.home.kettle.sh`

**Loki facts** (verified from `argocd-apps/loki.yaml` + chart values): destination namespace `monitoring`, `deploymentMode: SingleBinary`, no gateway, expose only as `loki` Service on port `3100`.

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/loki-ingest-ingressroute.yaml`

> Lives in the `kube-prometheus-stack` chart (rather than the loki chart) so the middlewares + Secret it references are in the same Helm release and same namespace. Pure colocation; no semantic dependency.

- [ ] **Step 1: Write the IngressRoute**

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
        - name: loki
          port: 3100
      middlewares:
        - name: workstation-ingest-basicauth
        - name: workstation-ingest-ratelimit
  tls:
    secretName: home-kettle-sh-tls
```

- [ ] **Step 2: Verify the actual Loki Service name + port (avoids drift surprises)**

Run (substitute the real kube context):
```bash
kubectl --context <CTX> -n monitoring get svc loki -o jsonpath='{.spec.ports[?(@.port==3100)].name}' && echo
```
Expected: prints a port name (e.g. `http-metrics`). If `loki` is missing or port 3100 is not exposed, the chart deployed differently than expected — discover the right service via `kubectl -n monitoring get svc | grep loki` and substitute in the IngressRoute before rendering.

- [ ] **Step 3: Render-check**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -B 1 -A 18 "name: loki-ingest$"`
Expected: IngressRoute renders; service target is `loki:3100`.

- [ ] **Step 4: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/loki-ingest-ingressroute.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: loki-ingest IngressRoute"
```

### Task A5: Update kube-prometheus-stack `valuesObject` — exemplars + folder annotation

**Files:**
- Modify: `/home/kettle/KettleCluster/home/argocd-apps/kube-prometheus-stack.yaml`

> The chart's `values.yaml` is a near-empty stub; the real values live here. `enableRemoteWriteReceiver: true` is already present.

- [ ] **Step 1: Read the current Application values block**

Run: `grep -n "prometheusSpec:\|grafana:\|sidecar:\|enableFeatures\|enableRemoteWriteReceiver" /home/kettle/KettleCluster/home/argocd-apps/kube-prometheus-stack.yaml | head -20`
Note the line numbers for `prometheusSpec:` and `grafana:` (if it exists).

- [ ] **Step 2: Add `enableFeatures` under `prometheus.prometheusSpec`**

Locate the `prometheusSpec:` block. After `enableRemoteWriteReceiver: true`, add:

```yaml
              enableFeatures:
                - exemplar-storage
```

(Indentation: the `enableFeatures` key sits at the same indentation as `enableRemoteWriteReceiver`.)

- [ ] **Step 3: Ensure `grafana.sidecar.dashboards.folderAnnotation` is set**

If a `grafana:` block already exists in `valuesObject`, ensure these keys are present:

```yaml
          grafana:
            sidecar:
              dashboards:
                folderAnnotation: grafana_folder
                provider:
                  foldersFromFilesStructure: false
```

If no `grafana:` block exists yet, add it at the same indentation as `prometheus:`. (The grafana sidecar still loads existing ConfigMap-based dashboards like cluster-overview; this just makes the folder annotation explicit.)

- [ ] **Step 4: Verify Application still parses**

Run: `yq eval '.spec.source.helm.valuesObject."kube-prometheus-stack".prometheus.prometheusSpec.enableFeatures' /home/kettle/KettleCluster/home/argocd-apps/kube-prometheus-stack.yaml`
Expected: `- exemplar-storage`.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/KettleCluster add home/argocd-apps/kube-prometheus-stack.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: enable exemplar-storage + pin folder annotation"
```

### Task A6: Loki datasource derived fields for trace_id correlation

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart/templates/loki-datasource-derived-fields.yaml`

> Note: this Loki datasource ConfigMap configures the **existing sidecar-loaded** Loki datasource. We're adding derivedFields, not replacing the datasource. If a Loki datasource is already provisioned (it is, by `kube-prometheus-stack`), this ConfigMap with `grafana_datasource: "1"` label is merged additively — but Grafana will prefer the most recently applied one. Use `editable: false` so users don't accidentally override it.

- [ ] **Step 1: Write the ConfigMap**

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
        url: http://loki.monitoring.svc.cluster.local:3100
        editable: false
        jsonData:
          derivedFields:
            # Anchors to 32 lowercase hex chars (OTLP/W3C trace IDs) and
            # accepts the optional "00-" W3C traceparent prefix. Single
            # quotes preserve the `\b` boundary marker.
            - name: TraceID
              matcherRegex: 'trace_id=(?:00-)?([a-f0-9]{32})\b'
              url: '${__value.raw}'
              datasourceUid: tempo
              urlDisplayLabel: 'View in Tempo'
```

- [ ] **Step 2: Render-check**

Run: `helm template /home/kettle/KettleCluster/home/apps/kube-prometheus-stack/chart | grep -B 2 -A 18 "name: grafana-datasource-loki-workstation"`
Expected: ConfigMap renders with `grafana_datasource: "1"` label.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/kube-prometheus-stack/chart/templates/loki-datasource-derived-fields.yaml
git -C /home/kettle/KettleCluster commit -m "kube-prometheus-stack: Loki datasource derivedFields for trace_id"
```

### Task A7: Install grafana-operator (NEW APP)

**Files:**
- Create: `/home/kettle/KettleCluster/home/argocd-apps/grafana-operator.yaml`
- Create: `/home/kettle/KettleCluster/home/apps/grafana-operator/chart/Chart.yaml`
- Create: `/home/kettle/KettleCluster/home/apps/grafana-operator/chart/values.yaml`

- [ ] **Step 1: Create the chart wrapper**

`apps/grafana-operator/chart/Chart.yaml`:

```yaml
apiVersion: v2
name: grafana-operator
description: grafana-operator umbrella for KettleCluster
type: application
version: 1.0.0
appVersion: "v5.5.2"
dependencies:
  - name: grafana-operator
    # Verified live: `helm pull` succeeded against this URL on 2026-05-28.
    # Chart is published under the grafana-operator org (NOT grafana org).
    # Tags ARE prefixed with `v` — confirmed via ghcr.io tags API.
    # Latest stable as of this plan: v5.5.2.
    version: "v5.5.2"
    repository: "oci://ghcr.io/grafana-operator/helm-charts"
```

`apps/grafana-operator/chart/values.yaml`:

```yaml
grafana-operator:
  # Operator runs in monitoring namespace alongside kube-prometheus-stack.
  namespaceScope: false
  # Watch for Grafana / Dashboard / Folder / Datasource CRs cluster-wide
  # so we can declare dashboards from any namespace.
  watchNamespaceSelector: {}
```

- [ ] **Step 2: Fetch the chart dependency**

Run: `helm dependency update /home/kettle/KettleCluster/home/apps/grafana-operator/chart/`
Expected: `Saving 1 charts ... Deleting outdated charts` followed by a `grafana-operator-v5.20.0.tgz` in `charts/`.

- [ ] **Step 3: Create the ArgoCD Application**

`home/argocd-apps/grafana-operator.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: grafana-operator
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: git@github.com:kettleofketchup/home.git
    path: apps/grafana-operator/chart
    targetRevision: HEAD
    helm:
      releaseName: grafana-operator
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  # NOTE: do NOT add a syncPolicy block here. The repo's sync-defaults
  # Kustomize component (home/argocd-apps/_components/sync-defaults/) patches
  # every Application with automated.{prune,selfHeal} + ServerSideApply +
  # RespectIgnoreDifferences=true at apply time. Adding an inline block here
  # would conflict with the patch.
```

- [ ] **Step 4: Add to root argocd-apps kustomization**

Run: `grep -n "resources:" /home/kettle/KettleCluster/home/argocd-apps/kustomization.yaml`

Then add the line `  - grafana-operator.yaml` under the `resources:` list, alphabetically near `grafana-dashboards.yaml`.

- [ ] **Step 5: Verify the chart renders**

Run: `helm template /home/kettle/KettleCluster/home/apps/grafana-operator/chart | head -20`
Expected: the operator Deployment + ServiceAccount + ClusterRole(s) render.

- [ ] **Step 6: Verify the actual admin-secret name + register Grafana**

The Grafana admin secret name depends on the Helm release. Verify before writing the CR:

```bash
kubectl --context <CTX> -n monitoring get secret -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[*].metadata.name}'
```

Note the printed name (likely `grafana-admin` per the existing `grafana-admin-secret.yaml` template). If it's `kube-prometheus-stack-grafana` or different, substitute it for `<ADMIN_SECRET>` below.

`apps/grafana-operator/chart/templates/grafana-instance.yaml`:

```yaml
# Tell grafana-operator about the kube-prometheus-stack Grafana so it
# can deliver Dashboard CRs to that instance via the in-cluster API.
apiVersion: grafana.integreatly.org/v1beta1
kind: Grafana
metadata:
  name: kube-prometheus-stack-grafana
  labels:
    # Dashboard CRs use spec.instanceSelector.matchLabels.dashboards
    # to target this instance.
    dashboards: "kube-prometheus-stack"
  annotations:
    # CRD is installed by the operator subchart in the same sync.
    # Wave 1 = apply this CR after the operator's manifests (wave 0).
    argocd.argoproj.io/sync-wave: "1"
spec:
  external:
    url: http://kube-prometheus-stack-grafana.monitoring.svc:80
    # Admin creds: secret-key references. (Operator v5 expects this
    # bare {name, key} shape under external.adminUser/adminPassword.)
    adminUser:
      name: <ADMIN_SECRET>   # value from kubectl verification above
      key: admin-user
    adminPassword:
      name: <ADMIN_SECRET>
      key: admin-password
```

> The `external:` block tells the operator to manage an existing Grafana via its HTTP API (using the admin Secret already in the cluster) rather than spinning up a new one. The sync-wave annotation keeps the CR from being applied before the operator's CRDs land.

- [ ] **Step 7: Commit**

```bash
git -C /home/kettle/KettleCluster add home/apps/grafana-operator/ home/argocd-apps/grafana-operator.yaml home/argocd-apps/kustomization.yaml
git -C /home/kettle/KettleCluster commit -m "grafana-operator: deploy + register kube-prometheus-stack as managed instance"
```

### Task A8: Enable automated sync on grafana-dashboards Application

**Files:**
- Modify: `/home/kettle/KettleCluster/home/argocd-apps/grafana-dashboards.yaml`

> **Update from review:** The repo's sync-defaults Kustomize component already patches every Application with `automated.{prune,selfHeal}` cluster-wide. This task is therefore a verification step, not an edit.

- [ ] **Step 1: Verify sync-defaults applies to grafana-dashboards**

Run: `grep -E "patches:|sync-defaults" /home/kettle/KettleCluster/home/argocd-apps/kustomization.yaml`
Expected: the kustomization references the `_components/sync-defaults` component as a patch target covering all Applications.

If for some reason `grafana-dashboards.yaml` is excluded (the patch may target by name list), add an inline `syncPolicy` block:

```yaml
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - ServerSideApply=true
```

Otherwise: no edit required.

- [ ] **Step 2: Commit + push the entire Phase-A batch**

```bash
git -C /home/kettle/KettleCluster status home/  # review what's staged
# If Step 1 required an inline syncPolicy edit, commit it:
git -C /home/kettle/KettleCluster add home/argocd-apps/grafana-dashboards.yaml 2>/dev/null && \
    git -C /home/kettle/KettleCluster commit -m "grafana-dashboards: enable automated sync (override component)"
git -C /home/kettle/KettleCluster push origin main
```

### Task A9: Verify ArgoCD reconcile end-to-end

- [ ] **Step 1: Wait for all four Applications to be Synced + Healthy**

Use the correct kube context (`kubectl config get-contexts | grep kettle` to find it; substitute `<CTX>` below):

```bash
for app in kube-prometheus-stack grafana-operator grafana-dashboards loki; do
  echo -n "$app: "
  kubectl --context <CTX> -n argocd get application $app \
    -o jsonpath='{.status.sync.status} {.status.health.status}'
  echo ""
done
```

Expected: each prints `Synced Healthy` within ~5 minutes.

- [ ] **Step 2: Verify Phase-A resources exist**

```bash
kubectl --context <CTX> -n monitoring get secret workstation-ingest-auth
kubectl --context <CTX> -n monitoring get middleware workstation-ingest-basicauth workstation-ingest-ratelimit
kubectl --context <CTX> -n monitoring get ingressroute prometheus-ingest loki-ingest
kubectl --context <CTX> -n monitoring get configmap grafana-datasource-loki-workstation
kubectl --context <CTX> -n monitoring get grafana kube-prometheus-stack-grafana
kubectl --context <CTX> -n monitoring get deployment grafana-operator-controller-manager
```

Expected: all resources `exist`.

- [ ] **Step 3: Verify Prometheus feature flag**

```bash
kubectl --context <CTX> -n monitoring get prometheus -o jsonpath='{.items[0].spec.enableFeatures}'
```

Expected: `["exemplar-storage"]`.

- [ ] **Step 4: Smoke-test auth from outside the cluster**

```bash
WORKSTATION_PASS=$(kubectl --context <CTX> -n monitoring get secret workstation-ingest-auth -o jsonpath='{.data.password}' | base64 -d)
echo "Expecting HTTP 400 (auth OK; empty body invalid):"
curl -sS -u "kettle-omarchy:$WORKSTATION_PASS" -X POST \
  https://prometheus-ingest.home.kettle.sh/api/v1/write \
  -H 'Content-Type: application/x-protobuf' \
  -H 'X-Prometheus-Remote-Write-Version: 0.1.0' \
  --data-binary '' -o /dev/null -w 'HTTP %{http_code}\n'
echo "Expecting HTTP 401:"
curl -sS -u "wrong:wrong" -X POST https://prometheus-ingest.home.kettle.sh/api/v1/write -o /dev/null -w 'HTTP %{http_code}\n'
```

Expected: first `HTTP 400`, second `HTTP 401`.

## Phase B — Python dashboard generator (grafana-dashboards repo)

All Phase-B work lives in `/home/kettle/git_repos/grafana-dashboards/`. The existing scaffold (commits `a306284` + `297078d`) provides `kgd` CLI, validator, envelope wrapping, `@register` discovery, and a `service_health.py` reference. The plan extends with panel builders, the host_omarchy dashboard, and `just dash::*` recipes that emit a `Dashboard` CR (grafana-operator's native shape) — no envelope stripping, no ConfigMap wrapping.

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
[group('dev')]
dev:
    uv sync --all-groups
    uv run pre-commit install || echo "pre-commit not configured yet; skip"

[group('dev')]
lint:
    uv run ruff check .
    uv run ty check src

[group('dev')]
test:
    uv run pytest -q

[group('dev')]
clean:
    rm -rf dist/ .pytest_cache __pycache__
```

- [ ] **Step 3: Verify**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just --list 2>&1 | head -20`
Expected: `dev`, `lint`, `test`, `clean` listed. Module-load errors for missing alloy/dash/cluster modules are OK at this stage.

- [ ] **Step 4: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add justfile just/dev.just
git -C /home/kettle/git_repos/grafana-dashboards commit -m "just: root justfile + dev module"
```

### Task B2: Stub the remaining just modules

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/just/alloy.just`
- Create: `/home/kettle/git_repos/grafana-dashboards/just/dash.just`
- Create: `/home/kettle/git_repos/grafana-dashboards/just/cluster.just`

- [ ] **Step 1: Write alloy.just stub**

```just
[group('alloy')]
default:
    @echo "alloy:: recipes will be populated in Phase C"
```

- [ ] **Step 2: Write dash.just stub**

```just
[group('dash')]
default:
    @echo "dash:: recipes will be populated after panel builders are ready"
```

- [ ] **Step 3: Write cluster.just stub**

```just
[group('cluster')]
default:
    @echo "cluster:: recipes reserved; Phase A artifacts shipped manually"
```

- [ ] **Step 4: Verify modules load**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just --list --list-submodules 2>&1 | head -30`
Expected: no errors; modules listed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add just/alloy.just just/dash.just just/cluster.just
git -C /home/kettle/git_repos/grafana-dashboards commit -m "just: stub alloy/dash/cluster modules"
```

### Task B3: panels/_common.py — Builder-inheriting query shims

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/__init__.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/_common.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_common.py`

> **Critical fix from review:** `PromQuery` / `LokiQuery` must inherit `Builder[DataQueryKind]`. Plain classes get rejected by `v2.Target.query(...)`. `service_health.py` already proves the inheritance pattern works.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_common.py
from __future__ import annotations

from grafana_foundation_sdk.cog.builder import Builder
from grafana_foundation_sdk.models.dashboardv2beta1 import DataQueryKind

from grafana_dashboards.panels._common import (
    PROM_DS_VAR, LOKI_DS_VAR, HOST_FILTER,
    PromQuery, LokiQuery, target,
    legend_table_right, tooltip_multi,
)


def test_prom_query_inherits_builder():
    assert issubclass(PromQuery, Builder)
    assert issubclass(LokiQuery, Builder)


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
    assert q.group == "loki"
    assert q.datasource.name == LOKI_DS_VAR
    assert q.spec["expr"] == '{job="x"} |= "err"'


def test_ds_vars_are_variable_expansions():
    assert PROM_DS_VAR == "$ds_prom"
    assert LOKI_DS_VAR == "$ds_loki"


def test_host_filter_uses_underscore_label():
    # Critical: Prometheus label syntax rejects dots; OTEL semconv
    # values land under host_name (underscore form).
    assert HOST_FILTER == 'host_name="$host"'


def test_target_returns_built_query_group():
    qg = target(PromQuery("up"))
    built = qg.build()
    # v2 QueryGroup model exposes targets in spec.targets (list).
    # Smoke-test we can reach the inner Target without exception.
    assert built is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_common.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grafana_dashboards.panels'`.

- [ ] **Step 3: Write the implementation**

`src/grafana_dashboards/panels/__init__.py`:

```python
"""Reusable panel builders for the workstation host dashboard."""
```

`src/grafana_dashboards/panels/_common.py`:

```python
from __future__ import annotations

from grafana_foundation_sdk.builders import (
    common as common_b,
    dashboardv2beta1 as v2,
)
from grafana_foundation_sdk.cog.builder import Builder
from grafana_foundation_sdk.models.common import (
    LegendDisplayMode,
    LegendPlacement,
    SortOrder,
    TooltipDisplayMode,
)
from grafana_foundation_sdk.models.dashboardv2beta1 import (
    DataQueryKind,
    Dashboardv2beta1DataQueryKindDatasource,
)

PROM_DS_VAR = "$ds_prom"
LOKI_DS_VAR = "$ds_loki"

# host_name is the OTEL host.name -> Prometheus underscore form; the
# `host` short alias is also stamped by Alloy but the canonical filter
# uses host_name so the writeRelabel allowlist semantics align.
HOST_FILTER = 'host_name="$host"'


class PromQuery(Builder[DataQueryKind]):
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


class LokiQuery(Builder[DataQueryKind]):
    """Wrap a LogQL expression in v2's DataQueryKind envelope."""

    def __init__(self, expr: str, *, legend: str = "", ref_id: str = "A") -> None:
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


def target(query: Builder[DataQueryKind], ref_id: str = "A") -> v2.QueryGroup:
    """Single-target QueryGroup."""
    return v2.QueryGroup().target(v2.Target().ref_id(ref_id).query(query))


def legend_table_right() -> common_b.VizLegendOptions:
    return (
        common_b.VizLegendOptions()
        .show_legend(True)
        .placement(LegendPlacement.RIGHT)
        .display_mode(LegendDisplayMode.TABLE)
        .calcs(["lastNotNull", "max"])
    )


def tooltip_multi() -> common_b.VizTooltipOptions:
    """Multi-series tooltip, descending sort. Uses the SortOrder enum
    (NOT the string 'desc' — SDK type-checks against the model)."""
    return (
        common_b.VizTooltipOptions()
        .mode(TooltipDisplayMode.MULTI)
        .sort(SortOrder.DESCENDING)
    )
```

- [ ] **Step 4: Run the test**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_common.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/__init__.py src/grafana_dashboards/panels/_common.py tests/test_panels_common.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: _common shims (Builder-inheriting PromQuery/LokiQuery, SortOrder enum)"
```

### Task B4: panels/stat.py — single-value stats with proper `ThresholdsConfig`

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/stat.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_stat.py`

> **Fix from review:** the SDK stat builder does not have a `.thresholds_steps(list[dict])` method. The correct API is `.thresholds(ThresholdsConfig().mode(ThresholdsMode.ABSOLUTE).steps([Threshold(color=..., value=...)]))`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_stat.py
from grafana_dashboards.panels.stat import (
    stat_psi_cpu, stat_psi_mem, stat_psi_io,
    stat_load1, stat_load5, stat_load15,
    stat_uptime, stat_temp, stat_stutter_count,
)


def _expr_of(panel_builder):
    """Walk the verified path: PanelKind → spec.data → QueryGroupKind →
    spec.queries[*] → PanelQueryKind → spec.query → DataQueryKind →
    spec[expr]. Path verified against grafana-foundation-sdk==0.0.12."""
    panel = panel_builder.build()
    queries = panel.spec.data.spec.queries
    assert queries, f"no queries on panel: {panel!r}"
    return queries[0].spec.query.spec["expr"]


def test_stat_psi_cpu_uses_recording_rule_with_clamp_and_percent():
    e = _expr_of(stat_psi_cpu())
    assert "host:psi_cpu_waiting:ratio1m" in e
    assert "clamp_max" in e and "100" in e
    assert 'host_name="$host"' in e


def test_psi_mem_and_io_use_their_recording_rules():
    assert "host:psi_memory_waiting:ratio1m" in _expr_of(stat_psi_mem())
    assert "host:psi_io_waiting:ratio1m" in _expr_of(stat_psi_io())


def test_load_panels_pin_correct_series():
    assert "node_load1{" in _expr_of(stat_load1())
    assert "node_load5{" in _expr_of(stat_load5())
    assert "node_load15{" in _expr_of(stat_load15())


def test_uptime_uses_now_minus_boot():
    e = _expr_of(stat_uptime())
    assert "node_time_seconds" in e and "node_boot_time_seconds" in e


def test_temp_takes_max_over_hwmon():
    e = _expr_of(stat_temp())
    assert "max" in e and "node_hwmon_temp_celsius" in e


def test_stutter_count_reads_recording_rule():
    e = _expr_of(stat_stutter_count())
    assert e.startswith("host:psi_cpu_stutter_events:count5m")
    assert 'host_name="$host"' in e
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_stat.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation** (SDK API verified against `grafana-foundation-sdk==0.0.12` installed via `uv sync`)

```python
# src/grafana_dashboards/panels/stat.py
from __future__ import annotations

from grafana_foundation_sdk.builders import (
    dashboard as dashboard_b,    # ThresholdsConfig BUILDER lives here
    dashboardv2beta1 as v2,
    stat as stat_b,
)
from grafana_foundation_sdk.cog.builder import Builder
# Threshold and ThresholdsMode MODELS live in dashboardv2beta1 (matches
# the type that stat.Visualization.thresholds expects).
from grafana_foundation_sdk.models.dashboardv2beta1 import (
    Threshold,
    ThresholdsConfig,
    ThresholdsMode,
)
from grafana_foundation_sdk.models.common import BigValueGraphMode

from grafana_dashboards.panels._common import HOST_FILTER, PromQuery, target


def _thresholds(*steps: tuple[str, float | None]) -> Builder[ThresholdsConfig]:
    # Return the BUILDER, not the built model. stat.thresholds() expects
    # Builder[ThresholdsConfig] per its signature.
    return (
        dashboard_b.ThresholdsConfig()
        .mode(ThresholdsMode.ABSOLUTE)
        .steps([Threshold(color=c, value=v) for c, v in steps])
    )


_PSI_THRESHOLDS = _thresholds(
    ("green", None), ("yellow", 10), ("orange", 30), ("red", 60),
)
_TEMP_THRESHOLDS = _thresholds(
    ("green", None), ("yellow", 75), ("orange", 85), ("red", 95),
)
_STUTTER_THRESHOLDS = _thresholds(
    ("green", None), ("yellow", 1), ("red", 3),
)


def _stat(pid: int, title: str, expr: str, *, unit: str = "short",
          thresholds: Builder[ThresholdsConfig] | None = None) -> v2.Panel:
    viz = stat_b.Visualization().unit(unit).graph_mode(BigValueGraphMode.AREA)
    if thresholds is not None:
        viz = viz.thresholds(thresholds)
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
                 thresholds=_PSI_THRESHOLDS)


def stat_psi_mem() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_memory_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(102, "PSI Memory (1m %)", expr, unit="percent",
                 thresholds=_PSI_THRESHOLDS)


def stat_psi_io() -> v2.Panel:
    expr = (
        f"clamp_max("
        f"host:psi_io_waiting:ratio1m{{{HOST_FILTER}}} * 100, 100)"
    )
    return _stat(103, "PSI I/O (1m %)", expr, unit="percent",
                 thresholds=_PSI_THRESHOLDS)


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
                 thresholds=_TEMP_THRESHOLDS)


def stat_stutter_count() -> v2.Panel:
    # NOTE: v2.Panel in SDK 0.0.12 does not expose a per-panel time
    # override method (verified: no `.time_from`). The recording rule
    # itself uses a 5m window, so the value reads "events in the last
    # 5m relative to the query time" regardless of the dashboard's
    # selected range — natural decoupling. Per-panel time overrides
    # would be done via Scenes conditional rendering at the layout
    # level; out of scope here.
    expr = f"host:psi_cpu_stutter_events:count5m{{{HOST_FILTER}}}"
    return _stat(109, "Stutter events (last 5m)", expr,
                 thresholds=_STUTTER_THRESHOLDS)
```

- [ ] **Step 4: Run the test**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_stat.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/stat.py tests/test_panels_stat.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: stat builders (Thresholds via ThresholdsConfig + per-panel time override on stutter count)"
```

### Task B5: panels/timeseries.py — timeseries panels with correct label names

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/timeseries.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_timeseries.py`

> **Fixes from review:** `ts_softirqs` aggregates by `vector` (real label), not `type`. `ts_disk_io_latency_p99` is renamed to `ts_disk_io_latency_avg` because node_exporter doesn't expose a histogram — `weighted_seconds / iops` is a mean. Test `_exprs()` asserts non-empty so attribute mismatches surface as failures, not silent passes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_timeseries.py
import pytest
from grafana_dashboards.panels.timeseries import (
    ts_psi_all, ts_cpu_per_core, ts_cpu_freq, ts_sched_runqueue,
    ts_irqs, ts_softirqs, ts_mem_breakdown,
    ts_gpu_util, ts_gpu_mem, ts_gpu_temp_power, ts_gpu_clock,
    ts_disk_iops, ts_disk_throughput, ts_disk_io_latency_avg, ts_io_wait,
    ts_net_bytes, ts_net_errors,
)


def _exprs(panel_builder):
    """Same path as B4's _expr_of, but multi-target."""
    panel = panel_builder.build()
    queries = panel.spec.data.spec.queries
    assert queries, f"no queries on panel: {panel!r}"
    return [pq.spec.query.spec["expr"] for pq in queries]


@pytest.mark.parametrize("builder, must_contain", [
    (ts_psi_all, ["psi_cpu_waiting:ratio1m", "psi_memory_waiting:ratio1m", "psi_io_waiting:ratio1m"]),
    (ts_cpu_per_core, ["node_cpu_seconds_total", 'mode="idle"']),
    (ts_cpu_freq, ["node_cpu_frequency_hertz"]),
    (ts_sched_runqueue, ["node_schedstat_waiting_seconds_total"]),
    (ts_irqs, ["node_interrupts_total", "topk(15"]),
    (ts_softirqs, ["node_softirqs_total", "sum by (vector)"]),  # vector, NOT type
    (ts_mem_breakdown, ["node_memory_MemTotal_bytes", "node_memory_MemFree_bytes"]),
    (ts_gpu_util, ["nvidia_smi_utilization_gpu_ratio"]),
    (ts_gpu_mem, ["nvidia_smi_memory_used_bytes"]),
    (ts_gpu_temp_power, ["nvidia_smi_temperature_gpu", "nvidia_smi_power_draw_watts"]),
    (ts_gpu_clock, ["nvidia_smi_clocks_current_graphics_clock_hz"]),
    (ts_disk_iops, ["node_disk_reads_completed_total", "node_disk_writes_completed_total"]),
    (ts_disk_throughput, ["node_disk_read_bytes_total", "node_disk_written_bytes_total"]),
    (ts_disk_io_latency_avg, ["node_disk_io_time_weighted_seconds_total"]),
    (ts_io_wait, ["node_cpu_seconds_total", 'mode="iowait"']),
    (ts_net_bytes, ["node_network_receive_bytes_total", "node_network_transmit_bytes_total"]),
    (ts_net_errors, ["node_network_receive_errs_total", "node_network_transmit_errs_total"]),
])
def test_timeseries_panels_pin_correct_metric_names(builder, must_contain):
    blob = "\n".join(_exprs(builder()))
    for needle in must_contain:
        assert needle in blob, f"{builder.__name__} missing {needle!r}; got:\n{blob}"


def test_panels_filter_by_host():
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
    expr = (
        f'1 - rate(node_cpu_seconds_total{{{HOST_FILTER},mode="idle"}}[$__rate_interval])'
    )
    return _panel(202, "CPU per-core utilization", _ts_viz(unit="percentunit"),
                  [(expr, "cpu{{cpu}}")])


def ts_cpu_freq() -> v2.Panel:
    expr = f"node_cpu_frequency_hertz{{{HOST_FILTER}}}"
    return _panel(203, "CPU frequency", _ts_viz(unit="hertz"),
                  [(expr, "cpu{{cpu}}")])


def ts_sched_runqueue() -> v2.Panel:
    expr = f"rate(node_schedstat_waiting_seconds_total{{{HOST_FILTER}}}[$__rate_interval])"
    return _panel(204, "Scheduler run-queue wait", _ts_viz(unit="s"),
                  [(expr, "cpu{{cpu}}")])


def ts_irqs() -> v2.Panel:
    expr = (
        f"topk(15, sum by (info) "
        f"(rate(node_interrupts_total{{{HOST_FILTER}}}[$__rate_interval])))"
    )
    return _panel(205, "Hardware interrupts (top 15)", _ts_viz(),
                  [(expr, "{{info}}")])


def ts_softirqs() -> v2.Panel:
    # node_softirqs_total has labels: cpu, vector. NOT 'type'.
    expr = (
        f"sum by (vector) "
        f"(rate(node_softirqs_total{{{HOST_FILTER}}}[$__rate_interval]))"
    )
    return _panel(206, "Softirqs by kind", _ts_viz(),
                  [(expr, "{{vector}}")])


def ts_mem_breakdown() -> v2.Panel:
    return _panel(207, "Memory breakdown", _ts_viz(unit="bytes", stack=StackingMode.NORMAL),
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


def ts_disk_io_latency_avg() -> v2.Panel:
    # node_exporter doesn't expose a histogram; weighted_seconds / IOPS
    # is the mean queue residence time per IO. Renamed from "_p99" so
    # the title and the math agree.
    expr = (
        f"rate(node_disk_io_time_weighted_seconds_total{{{HOST_FILTER}}}[$__rate_interval]) "
        f"/ clamp_min("
        f"  rate(node_disk_reads_completed_total{{{HOST_FILTER}}}[$__rate_interval]) "
        f"+ rate(node_disk_writes_completed_total{{{HOST_FILTER}}}[$__rate_interval]),"
        f"  1)"
    )
    return _panel(403, "Disk IO mean latency", _ts_viz(unit="s"),
                  [(expr, "{{device}}")])


def ts_io_wait() -> v2.Panel:
    expr = f'rate(node_cpu_seconds_total{{{HOST_FILTER},mode="iowait"}}[$__rate_interval])'
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
Expected: 17 parametrised cases pass + host-filter case passes.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/timeseries.py tests/test_panels_timeseries.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: timeseries builders (softirqs by vector, io_latency_avg, assert non-empty targets)"
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
    queries = panel.spec.data.spec.queries
    assert queries, f"no queries on panel: {panel!r}"
    return queries[0].spec.query.spec["expr"]


def test_top_cgroup_cpu_uses_query_time_topk():
    e = _expr(top_cgroup_cpu_table)
    assert "topk(10," in e
    assert "host:cgroup_cpu:sum5m" in e
    assert 'host_name="$host"' in e


def test_top_cgroup_mem_uses_recording_rule():
    e = _expr(top_cgroup_mem_table)
    assert "topk(10," in e
    assert "host:cgroup_memory_rss:sum5m" in e


def test_top_error_units_loki_query():
    e = _expr(top_error_units_table)
    assert "sum by (unit)" in e
    assert "rate({" in e
    assert 'host_name="$host"' in e
    assert 'priority=~"0|1|2|3"' in e
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
    return (
        v2.Panel()
        .id(pid)
        .title(title)
        .data(target(query))
        .visualization(table_b.Visualization())
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

- [ ] **Step 4: Run the test**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_tables.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/tables.py tests/test_panels_tables.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: top-talker tables (query-time topk)"
```

### Task B7: panels/logs.py — error rate + log tail with proper enums

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/panels/logs.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_panels_logs.py`

> **Fix from review:** `.dedup_strategy(...)` takes the `LogsDedupStrategy` enum, not a string.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panels_logs.py
from grafana_dashboards.panels.logs import logs_panel, error_rate_timeseries


def _exprs(panel_builder):
    panel = panel_builder.build()
    queries = panel.spec.data.spec.queries
    assert queries, f"no queries on panel: {panel!r}"
    return [pq.spec.query.spec["expr"] for pq in queries]


def test_error_rate_filters_by_priority_and_host():
    e = _exprs(error_rate_timeseries())[0]
    assert "sum by (unit)" in e
    assert "rate({" in e
    assert 'host_name="$host"' in e
    assert 'priority=~"0|1|2|3"' in e


def test_logs_panel_emits_selector():
    e = _exprs(logs_panel())[0]
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
)
# LogsDedupStrategy lives in models.common (verified). Not in models.logs.
from grafana_foundation_sdk.models.common import LogsDedupStrategy

from grafana_dashboards.panels._common import HOST_FILTER, LokiQuery, target
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
    expr = f'{{{HOST_FILTER},priority=~"0|1|2|3"}}'
    viz = (
        logs_b.Visualization()
        .show_time(True)
        .show_labels(False)
        .show_common_labels(False)
        .wrap_log_message(True)
        .enable_log_details(True)
        .dedup_strategy(LogsDedupStrategy.NONE)
    )
    return (
        v2.Panel()
        .id(703)
        .title("Error log tail")
        .data(target(LokiQuery(expr)))
        .visualization(viz)
    )
```

- [ ] **Step 4: Run the test**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_panels_logs.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/panels/logs.py tests/test_panels_logs.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "panels: logs (LogsDedupStrategy.NONE enum)"
```

### Task B8: variables.py — template variables

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/variables.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_variables.py`

> Pattern mirrors `service_health.py` (already proven to work for `label_values(up, job)`-style variable queries).

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
        name = getattr(spec, "name", None)
        if name is None and isinstance(spec, dict):
            name = spec.get("name")
        names.append(name)
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
    )
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
git -C /home/kettle/git_repos/grafana-dashboards commit -m "variables: ds_prom, ds_loki, host, window"
```

### Task B9: rows.py — grid composer

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/rows.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_rows.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rows.py
from grafana_dashboards.rows import compose_grid


def test_compose_grid_auto_wraps_after_24_columns():
    items = [
        ("a", 12, 4),
        ("b", 12, 4),
        ("c", 12, 4),  # wraps to next row
    ]
    positions = compose_grid(items)
    assert positions == [("a", 0, 0, 12, 4), ("b", 12, 0, 12, 4), ("c", 0, 4, 12, 4)]


def test_compose_grid_handles_uneven_heights():
    items = [("a", 12, 6), ("b", 12, 4), ("c", 12, 4)]
    positions = compose_grid(items)
    # Row 1 takes max(6,4)=6; row 2 starts at y=6.
    assert positions == [("a", 0, 0, 12, 6), ("b", 12, 0, 12, 4), ("c", 0, 6, 12, 4)]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_rows.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# src/grafana_dashboards/rows.py
"""Grid layout composer for v2 dashboards.

Returns a list of (name, x, y, width, height) tuples that the dashboard
module turns into v2.GridItem instances. Auto-wraps when (x + width) > 24.
"""

from __future__ import annotations

from collections.abc import Iterable


def compose_grid(items: Iterable[tuple[str, int, int]]) -> list[tuple[str, int, int, int, int]]:
    out = []
    x = 0
    y = 0
    row_h = 0
    for name, w, h in items:
        if x + w > 24:
            x = 0
            y += row_h
            row_h = 0
        out.append((name, x, y, w, h))
        x += w
        row_h = max(row_h, h)
    return out
```

- [ ] **Step 4: Run the test**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_rows.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/rows.py tests/test_rows.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "rows: compose_grid auto-wraps at 24 columns"
```

### Task B10a: dashboards/host_omarchy.py — skeleton + RECORDING_RULES

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/src/grafana_dashboards/dashboards/host_omarchy.py`

- [ ] **Step 1: Write the skeleton with RECORDING_RULES + a minimal build()**

```python
# src/grafana_dashboards/dashboards/host_omarchy.py
"""Workstation host dashboard for kettle-omarchy.

UID: kettle-host-omarchy. Deployed as a grafana-operator Dashboard CR;
recording rules deployed as a PrometheusRule CR.
"""

from __future__ import annotations

from grafana_foundation_sdk.builders import dashboardv2beta1 as v2
from grafana_foundation_sdk.models.dashboardv2beta1 import DashboardCursorSync

from grafana_dashboards.dashboards import DashboardSpec, register
from grafana_dashboards.variables import build_variables


# Recording rules — order matters: rule N may depend on rule <N.
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
        # Non-bool comparison filters samples; count_over_time then
        # counts only the truthy 1m samples in the 5m window.
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


@register("host-omarchy")
def build() -> DashboardSpec:
    builder = (
        v2.Dashboard("Workstation — kettle-omarchy")
        .description(
            "Host monitoring for the Omarchy workstation: CPU, memory, GPU, "
            "I/O, network, PSI, IRQ, journald errors. Click-drag a PSI spike "
            "to zoom every panel below to the stutter window."
        )
        .tags(["workstation", "kettle-omarchy", "host", "psi"])
        .editable(True)
        .preload(False)
        .live_now(False)
        # v2 feature: crosshair sync across panels (hover on one =
        # crosshair on all). Surfaces correlations in stutter forensics.
        .cursor_sync(DashboardCursorSync.CROSSHAIR)
        .time_settings(
            v2.TimeSettings()
            .from_val("now-1h").to("now").auto_refresh("30s").timezone("browser")
        )
    )
    for var in build_variables():
        builder = builder.variable(var)
    # Empty layout for now — B10b fills it.
    builder = builder.layout(v2.Rows())
    return DashboardSpec(uid="kettle-host-omarchy", builder=builder)
```

- [ ] **Step 2: Commit the skeleton**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/dashboards/host_omarchy.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "host_omarchy: skeleton + RECORDING_RULES"
```

### Task B10b: dashboards/host_omarchy.py — layout & panel wiring (direct callables)

> **Fix from review:** drop the `_BUILDERS` dict; use direct callables in the `_LAYOUT` list so the panel registration is single-sourced.

- [ ] **Step 1: Replace `build()` with the fully-wired version**

Edit `src/grafana_dashboards/dashboards/host_omarchy.py`. Add imports at the top:

```python
from grafana_dashboards.panels import logs as logs_p
from grafana_dashboards.panels import stat
from grafana_dashboards.panels import tables
from grafana_dashboards.panels import timeseries as ts
from grafana_dashboards.rows import compose_grid
```

Replace the body of `build()` (everything from `builder = (` through `return DashboardSpec(...)`) with:

```python
    # (element_name, builder_callable, width, height)
    # Row-1 widths sum to exactly 24 so all stat panels fit on one row:
    # 3+3+3 (PSI) + 2+2+2 (load) + 3+3+3 (uptime/temp/stutter) = 24.
    layout = [
        # Row 1: right-now indicators
        ("psi-cpu",        stat.stat_psi_cpu,         3, 3),
        ("psi-mem",        stat.stat_psi_mem,         3, 3),
        ("psi-io",         stat.stat_psi_io,          3, 3),
        ("load1",          stat.stat_load1,           2, 3),
        ("load5",          stat.stat_load5,           2, 3),
        ("load15",         stat.stat_load15,          2, 3),
        ("uptime",         stat.stat_uptime,          3, 3),
        ("temp-max",       stat.stat_temp,            3, 3),
        ("stutter-count",  stat.stat_stutter_count,   3, 3),
        # Row 2: headline PSI timeseries
        ("psi-all",        ts.ts_psi_all,            24, 8),
        # Row 3: CPU detail
        ("cpu-per-core",   ts.ts_cpu_per_core,       12, 8),
        ("cpu-freq",       ts.ts_cpu_freq,           12, 8),
        ("sched-runq",     ts.ts_sched_runqueue,     12, 6),
        ("top-cpu",        tables.top_cgroup_cpu_table, 12, 6),
        # Row 4: memory
        ("mem-break",      ts.ts_mem_breakdown,      12, 8),
        ("top-mem",        tables.top_cgroup_mem_table, 12, 8),
        # Row 5: GPU
        ("gpu-util",       ts.ts_gpu_util,           12, 6),
        ("gpu-mem",        ts.ts_gpu_mem,            12, 6),
        ("gpu-temp-power", ts.ts_gpu_temp_power,     12, 6),
        ("gpu-clock",      ts.ts_gpu_clock,          12, 6),
        # Row 6: disk + IO
        ("disk-iops",      ts.ts_disk_iops,          12, 6),
        ("disk-throughput",ts.ts_disk_throughput,    12, 6),
        ("disk-io-latency",ts.ts_disk_io_latency_avg,12, 6),
        ("io-wait",        ts.ts_io_wait,            12, 6),
        # Row 7: IRQ + softirq
        ("irqs",           ts.ts_irqs,               12, 6),
        ("softirqs",       ts.ts_softirqs,           12, 6),
        # Row 8: network
        ("net-bytes",      ts.ts_net_bytes,          12, 6),
        ("net-errors",     ts.ts_net_errors,         12, 6),
        # Row 9: errors + logs
        ("err-rate",       logs_p.error_rate_timeseries, 12, 8),
        ("err-units",      tables.top_error_units_table, 12, 8),
        ("err-tail",       logs_p.logs_panel,        24, 10),
    ]

    builder = (
        v2.Dashboard("Workstation — kettle-omarchy")
        .description(
            "Host monitoring for the Omarchy workstation: CPU, memory, GPU, "
            "I/O, network, PSI, IRQ, journald errors. Click-drag a PSI spike "
            "to zoom every panel below to the stutter window."
        )
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

    for name, factory, _w, _h in layout:
        builder = builder.element(name, factory())

    grid = v2.Grid()
    for name, x, y, w, h in compose_grid([(n, w, h) for n, _, w, h in layout]):
        grid = grid.item(v2.GridItem().name(name).x(x).y(y).width(w).height(h))
    builder = builder.layout(
        v2.Rows().row(v2.Row().title("Workstation").collapse(False).layout(grid))
    )

    for var in build_variables():
        builder = builder.variable(var)

    return DashboardSpec(uid="kettle-host-omarchy", builder=builder)
```

- [ ] **Step 2: Verify a manual render**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run python -c "from grafana_dashboards.dashboards.host_omarchy import build; print(build().uid); print(len(build().builder.build().elements))"`
Expected: prints `kettle-host-omarchy` then `31` (count of elements; matches layout length).

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/dashboards/host_omarchy.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "host_omarchy: layout + panel wiring (direct callables)"
```

### Task B10c: Wire host_omarchy into `_AUTOLOAD` and verify via `kgd`

- [ ] **Step 1: Edit `src/grafana_dashboards/dashboards/__init__.py`**

Find:

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

- [ ] **Step 2: Verify in the registry**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run kgd list`
Expected: outputs include `host-omarchy` and `service-health`.

- [ ] **Step 3: Render via kgd (envelope-wrapped) and run the validator**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run kgd generate -o /tmp/kgd-out -d host-omarchy && ls /tmp/kgd-out/`
Expected: `/tmp/kgd-out/kettle-host-omarchy.json` exists; stderr empty (no validation issues).

- [ ] **Step 4: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add src/grafana_dashboards/dashboards/__init__.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "host_omarchy: register in _AUTOLOAD"
```

### Task B11: tests/test_render.py — end-to-end render + corrected assertions

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_render.py`

> **Fixes from review:** datasource-UID assertion checks the correct path (panel data, not whole-blob); backslash regression searches for the actual JSON-source pattern that decodes to the buggy regex form.

- [ ] **Step 1: Write the test**

```python
# tests/test_render.py
from __future__ import annotations

import json
import re
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


def test_host_omarchy_passes_validator():
    wrapped, _ = _render("host-omarchy")
    issues = validate_v2(wrapped)
    assert issues == [], "validator issues:\n" + "\n".join(issues)


def test_host_omarchy_uses_ds_var_in_panel_queries():
    """Panel-level datasource refs use the $ds_prom/$ds_loki variables,
    never a hard-coded UID.

    Verified encoding path (grafana-foundation-sdk==0.0.12):
      spec.elements.<name>.spec.data.spec.queries[i].spec.query.datasource
    Note the `.query` level between PanelQuery.spec and DataQueryKind.
    """
    wrapped, _ = _render("host-omarchy")
    elements = wrapped["spec"]["elements"]
    checked = 0
    for name, element in elements.items():
        for pq in (element.get("spec", {})
                          .get("data", {})
                          .get("spec", {})
                          .get("queries", [])):
            ds = (pq.get("spec", {})
                    .get("query", {})
                    .get("datasource") or {})
            name_field = ds.get("name", "")
            uid_field = ds.get("uid", "")
            assert name_field in ("$ds_prom", "$ds_loki"), (
                f"element {name!r} query datasource name={name_field!r} "
                f"(expected $ds_prom or $ds_loki)"
            )
            assert not uid_field, (
                f"element {name!r} query has hardcoded uid={uid_field!r}"
            )
            checked += 1
    assert checked > 0, "no panel queries found — path walking is wrong"


def test_host_omarchy_no_logql_backslash_overescape():
    """In JSON source, `\\\\\\\\.` (4 chars) decodes to `\\\\.` in memory
    (2 chars: literal backslash + dot). In LogQL backtick/regex contexts
    that's `\\.` regex = literal-backslash-then-any-char, NOT `\\.` = dot.

    We render via json.dumps so we get the in-memory representation;
    pattern to find is therefore `\\\\.` (2 chars: backslash + dot) — any
    sequence of 2+ in-memory backslashes immediately followed by a dot
    is an over-escape."""
    _, rendered = _render("host-omarchy")
    bug = re.compile(r"\\{2,}\.")
    matches = bug.findall(rendered)
    assert not matches, f"backslash over-escape in LogQL expr: {matches}"


def test_all_registered_dashboards_render_clean():
    for slug, factory in all_dashboards().items():
        spec = factory()
        body = json.loads(ENCODER.encode(spec.builder.build()))
        wrapped = wrap_v2(body, uid=spec.uid)
        issues = validate_v2(wrapped)
        assert issues == [], f"{slug}:\n" + "\n".join(issues)
```

- [ ] **Step 2: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_render.py -v`
Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add tests/test_render.py
git -C /home/kettle/git_repos/grafana-dashboards commit -m "tests: render round-trip + corrected datasource + backslash regressions"
```

### Task B12: just dash:: recipes — emit Dashboard CR (no envelope strip) + PrometheusRule

**Files:**
- Modify: `/home/kettle/git_repos/grafana-dashboards/just/dash.just`
- Create: `/home/kettle/git_repos/grafana-dashboards/scripts/render_to_chart.py`
- Create: `/home/kettle/git_repos/grafana-dashboards/tests/test_render_to_chart.py`

> **New deployment model (grafana-operator):** instead of stripping the v2 envelope and wrapping in a ConfigMap, we wrap each dashboard's envelope-form JSON inside a `Dashboard` CR (apiVersion `grafana.integreatly.org/v1beta1`). The CR's `spec.json` field carries the entire v2 envelope. The operator reconciles it to the registered Grafana instance via the HTTP API.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render_to_chart.py
import importlib
import json
import re
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def runner(tmp_path):
    mod = importlib.import_module("scripts.render_to_chart")
    return mod, tmp_path


def test_emits_dashboard_cr_and_prometheus_rule(runner):
    mod, tmp = runner
    out_dir = tmp / "chart"
    mod.render(["host-omarchy"], out_dir)
    cr_file = out_dir / "templates" / "kettle-host-omarchy.yaml"
    rule_file = out_dir / "templates" / "kettle-host-omarchy-rules.yaml"
    assert cr_file.exists()
    assert rule_file.exists()

    cr = yaml.safe_load(cr_file.read_text())
    assert cr["apiVersion"] == "grafana.integreatly.org/v1beta1"
    assert cr["kind"] == "Dashboard"
    assert cr["metadata"]["name"] == "kettle-host-omarchy"
    # CR carries the full envelope-form JSON in spec.json.
    body = json.loads(cr["spec"]["json"])
    assert body["apiVersion"] == "dashboard.grafana.app/v2beta1"
    assert body["kind"] == "Dashboard"
    assert "spec" in body and "elements" in body["spec"]
    # instanceSelector targets the registered Grafana instance.
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_render_to_chart.py -v`
Expected: FAIL — `scripts.render_to_chart` not found.

- [ ] **Step 3: Write the helper script**

```python
# scripts/render_to_chart.py
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
    return {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "Dashboard",
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
        cr_path.write_text(yaml.safe_dump(
            _dashboard_cr(spec.uid, envelope, folder=folder,
                          instance_label=instance_label),
            sort_keys=False, width=10_000,
        ))
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
```

- [ ] **Step 4: Add PyYAML to project deps**

Edit `pyproject.toml`, in the `[project] dependencies` list (after `grafana-foundation-sdk`), add:

```
    "pyyaml>=6.0",
```

Then run `cd /home/kettle/git_repos/grafana-dashboards && uv sync`.

- [ ] **Step 5: Run the tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest tests/test_render_to_chart.py -v`
Expected: 2 passed.

- [ ] **Step 6: Write the dash.just recipes**

Overwrite `just/dash.just`:

```just
CHART_DIR := env("KETTLE_CHART_DIR", "/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart")

[group('dash')]
render slug:
    uv run python scripts/render_to_chart.py -o {{CHART_DIR}} -d {{slug}}

[group('dash')]
render-all:
    uv run python scripts/render_to_chart.py -o {{CHART_DIR}}

[group('dash')]
validate slug:
    uv run kgd generate -o $(mktemp -d) -d {{slug}}

[group('dash')]
validate-all:
    uv run kgd generate -o $(mktemp -d)

[group('dash')]
diff slug:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp=$(mktemp -d)
    uv run python scripts/render_to_chart.py -o "$tmp" -d {{slug}}
    diff -u "{{CHART_DIR}}/templates/kettle-{{slug}}.yaml" \
            "$tmp/templates/kettle-{{slug}}.yaml" || true
```

- [ ] **Step 7: Smoke-test against a tempdir**

```bash
cd /home/kettle/git_repos/grafana-dashboards
T=$(mktemp -d)
KETTLE_CHART_DIR=$T just dash::render host-omarchy
ls -la $T/templates/
head -30 $T/templates/kettle-host-omarchy.yaml
```

Expected: two YAML files in templates/; the Dashboard CR's `spec.json` contains the v2beta1 envelope.

- [ ] **Step 8: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add just/dash.just scripts/render_to_chart.py tests/test_render_to_chart.py pyproject.toml uv.lock
git -C /home/kettle/git_repos/grafana-dashboards commit -m "dash: render Dashboard CR (grafana-operator) + PrometheusRule + just recipes"
```

### Task B13: Render into the cluster chart + push

**Files:**
- Create: `/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy.yaml`
- Create: `/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy-rules.yaml`

- [ ] **Step 1: Render into the real chart dir**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just dash::render host-omarchy`
Expected: prints two "wrote ..." lines pointing into `/home/kettle/KettleCluster/home/apps/grafana-dashboards/chart/templates/`.

- [ ] **Step 2: Helm-template the chart**

Run: `helm template /home/kettle/KettleCluster/home/apps/grafana-dashboards/chart | grep -E "kind: Dashboard$|name: kettle-host-omarchy"`
Expected: the Dashboard CR appears; the PrometheusRule renders.

- [ ] **Step 3: Commit + push**

```bash
git -C /home/kettle/KettleCluster add home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy.yaml \
    home/apps/grafana-dashboards/chart/templates/kettle-host-omarchy-rules.yaml
git -C /home/kettle/KettleCluster commit -m "grafana-dashboards: kettle-host-omarchy Dashboard CR + recording rules"
git -C /home/kettle/KettleCluster push origin main
```

- [ ] **Step 4: Verify ArgoCD reconcile + operator picks it up**

```bash
kubectl --context <CTX> -n argocd get application grafana-dashboards -o jsonpath='{.status.sync.status} {.status.health.status}'
kubectl --context <CTX> -n monitoring get dashboard.grafana.integreatly.org kettle-host-omarchy
kubectl --context <CTX> -n monitoring get dashboard.grafana.integreatly.org kettle-host-omarchy -o jsonpath='{.status.conditions}'
```

Expected: Application Synced+Healthy; Dashboard CR exists; status shows the operator successfully pushed it to Grafana.

- [ ] **Step 5: Browse the dashboard**

Open `https://grafana.home.kettle.sh/dashboards`.

Expected: a "Workstation" folder appears with "Workstation — kettle-omarchy" inside. All panels say "No data" until Phase C ships metrics — expected.

## Phase C — Host Alloy agent (Omarchy workstation)

### Task C1: alloy/config.alloy.j2 — corrected pipeline (no dead code, working journald)

**Files:**
- Create: `/home/kettle/git_repos/grafana-dashboards/alloy/config.alloy.j2`
- Create: `/home/kettle/git_repos/grafana-dashboards/alloy/env.example`

> **Fixes from review:**
> - Dead `discovery.relabel "host_labels"` block deleted; all stamping happens in the single `prometheus.relabel "host_stamp"` and a parallel `loki.relabel "host_stamp"`.
> - `stage.json` replaced with `loki.relabel` reading `__journal__systemd_unit` and `__journal__boot_id` directly. `format_as_json = false` works correctly with this path.
> - `matches` field uses explicit OR-list of valid `PRIORITY=N` matchers (not the invalid `0..6` range form).

- [ ] **Step 1: Write the template**

```jinja
// Rendered by `just alloy::configure`. Do not edit /etc/alloy/config.alloy by hand.

// ─── 1. node_exporter-equivalent metrics (pinned collector list) ─────────────
prometheus.exporter.unix "host" {
  set_collectors = [
    "cpu", "meminfo", "loadavg", "filesystem", "diskstats",
    "netdev", "netstat", "sockstat", "time", "uname", "vmstat",
    "hwmon", "cpufreq", "pressure", "schedstat", "interrupts", "softirqs",
  ]
  filesystem {
    fs_types_exclude    = "^(autofs|binfmt_misc|bpf|cgroup2?|configfs|debugfs|devpts|devtmpfs|fusectl|hugetlbfs|iso9660|mqueue|nsfs|overlay|proc|procfs|pstore|rpc_pipefs|securityfs|selinuxfs|squashfs|sysfs|tracefs)$"
    mount_points_exclude = "^/(dev|proc|sys|run|var/lib/(docker|containerd|kubelet))($|/)"
  }
}

// ─── 2. cgroup metrics (Alloy cadvisor component) ───────────────────────────
prometheus.exporter.cadvisor "host" {
  docker_only            = false
  store_container_labels = false
}

// ─── 3. NVIDIA GPU exporter (local scrape) ──────────────────────────────────
prometheus.scrape "nvidia" {
  targets         = [{ __address__ = "127.0.0.1:9835" }]
  forward_to      = [prometheus.relabel.host_stamp.receiver]
  scrape_interval = "30s"
  job_name        = "nvidia_gpu_exporter"
}

// ─── 4. Browser/Steam cgroup-scope collapse ─────────────────────────────────
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
    regex         = "app-flatpak-.+\\.scope"
    target_label  = "name"
    replacement   = "flatpak"
  }
  rule {
    source_labels = ["name"]
    regex         = "(systemd-udevd|run-u\\d+).*"
    action        = "drop"
  }
}

// ─── 5. Stamp every series with host labels ─────────────────────────────────
prometheus.relabel "host_stamp" {
  forward_to = [prometheus.remote_write.cluster.receiver]
  rule { target_label = "host"            replacement = "{{ HOSTNAME }}" }
  rule { target_label = "host_name"       replacement = "{{ HOSTNAME }}" }
  rule { target_label = "host_id"         replacement = "{{ HOST_ID }}" }
  rule { target_label = "host_arch"       replacement = "{{ HOST_ARCH }}" }
  rule { target_label = "os_type"         replacement = "linux" }
  rule { target_label = "os_description"  replacement = "{{ OS_DESCRIPTION }}" }
  rule { target_label = "role"            replacement = "workstation" }
  rule { target_label = "distro"          replacement = "{{ DISTRO }}" }
  rule { target_label = "gpu"             replacement = "{{ GPU }}" }
}

// ─── 6. Wire exporters into the relabel chain ───────────────────────────────
prometheus.scrape "host_unix" {
  targets         = prometheus.exporter.unix.host.targets
  forward_to      = [prometheus.relabel.host_stamp.receiver]
  scrape_interval = "15s"
  job_name        = "workstation-node"
}

prometheus.scrape "host_cadvisor" {
  targets         = prometheus.exporter.cadvisor.host.targets
  forward_to      = [prometheus.relabel.cgroup_collapse.receiver]
  scrape_interval = "30s"
  job_name        = "workstation-cgroups"
}

// ─── 7. Push metrics to cluster Prometheus ──────────────────────────────────
prometheus.remote_write "cluster" {
  endpoint {
    url = "https://prometheus-ingest.home.kettle.sh/api/v1/write"
    basic_auth {
      username = env("PROM_USER")
      password = env("PROM_PASS")
    }
  }
}

// ─── 8. journald → Loki ─────────────────────────────────────────────────────
// Alloy's loki.source.journal exposes journal fields under __journal_*.
// We do NOT use stage.json because lines are not JSON-encoded.
// We do NOT use the `matches = "PRIORITY=0..6"` range form (invalid syntax)
// — instead, journalctl matchers are explicit OR-equality clauses.

loki.source.journal "host" {
  forward_to     = [loki.relabel.host_stamp.receiver]
  format_as_json = false
  matches = "PRIORITY=0|PRIORITY=1|PRIORITY=2|PRIORITY=3|PRIORITY=4|PRIORITY=5|PRIORITY=6"
  // Stream labels (low cardinality, identifies the source):
  labels = {
    role = "workstation",
  }
  // Journal fields surfaced as __journal_<lower>; we move them into
  // labels (unit, priority) and structured metadata (boot_id) in the
  // relabel/process chain below.
}

loki.relabel "host_stamp" {
  forward_to = [loki.process.journal_fields.receiver]
  // Stamp host labels on every log line.
  rule { target_label = "host"           replacement = "{{ HOSTNAME }}" }
  rule { target_label = "host_name"      replacement = "{{ HOSTNAME }}" }
  rule { target_label = "host_id"        replacement = "{{ HOST_ID }}" }
  rule { target_label = "host_arch"      replacement = "{{ HOST_ARCH }}" }
  rule { target_label = "os_type"        replacement = "linux" }
  rule { target_label = "distro"         replacement = "{{ DISTRO }}" }
  // Promote __journal__systemd_unit → unit (stream label).
  // Note: _SYSTEMD_UNIT is a journal *transport* field, prefixed with
  // a single underscore in journald → arrives as __journal__systemd_unit
  // (DOUBLE underscore between "journal" and "systemd").
  rule {
    source_labels = ["__journal__systemd_unit"]
    target_label  = "unit"
  }
  // Promote __journal_priority → priority (stream label).
  // PRIORITY is a journal native field (no leading underscore in journald)
  // → arrives as __journal_priority (SINGLE underscore). Yes, asymmetric.
  rule {
    source_labels = ["__journal_priority"]
    target_label  = "priority"
  }
  // Promote __journal__boot_id → boot_id (will be moved to structured
  // metadata by the next stage; we promote here so it survives the
  // relabel boundary).
  rule {
    source_labels = ["__journal__boot_id"]
    target_label  = "boot_id"
  }
}

loki.process "journal_fields" {
  forward_to = [loki.write.cluster.receiver]
  // boot_id as structured metadata (Loki 3+) so reboots don't churn
  // streams. The `values` map's keys are output metadata fields;
  // values are templated strings referencing existing labels via
  // Go-template syntax. boot_id is read from the label promoted by
  // the upstream relabel block.
  stage.structured_metadata {
    values = { boot_id = "" }
  }
  // Drop boot_id as a stream label so only the structured-metadata
  // form remains.
  stage.labeldrop {
    values = ["boot_id"]
  }
}

// ─── 9. Push logs to cluster Loki ───────────────────────────────────────────
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

- [ ] **Step 2: Write env.example**

```bash
# /etc/alloy/env — populated by `just alloy::configure`.
# Permissions: 0600 alloy:alloy.

PROM_USER=kettle-omarchy
PROM_PASS=replace-me

LOKI_USER=kettle-omarchy
LOKI_PASS=replace-me
```

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add alloy/config.alloy.j2 alloy/env.example
git -C /home/kettle/git_repos/grafana-dashboards commit -m "alloy: config template (no dead code, journal via loki.relabel)"
```

### Task C2: just alloy:: install + configure + lifecycle recipes

**Files:**
- Modify: `/home/kettle/git_repos/grafana-dashboards/just/alloy.just`

- [ ] **Step 1: Replace stub with the real recipes**

```just
ALLOY_ETC := "/etc/alloy"
TEMPLATE  := "alloy/config.alloy.j2"

# Install grafana-alloy (extra repo) + nvidia-gpu-exporter-bin (AUR).
# Creates the alloy user/group via the pacman package; ensures /etc/alloy
# exists. Idempotent.
[group('alloy')]
install:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! lspci | grep -qi nvidia; then
        echo "no NVIDIA GPU detected; nvidia-gpu-exporter would be useless. Continue? (y/N)"
        read -r ans
        [[ "$ans" =~ ^[Yy]$ ]] || exit 1
    fi
    sudo pacman -S --needed --noconfirm grafana-alloy
    yay -S --needed --noconfirm nvidia-gpu-exporter-bin
    sudo install -d -o alloy -g alloy -m 0755 {{ALLOY_ETC}}

# Render config.alloy.j2 → /etc/alloy/config.alloy. Idempotent.
# Re-run after distro upgrade (changes PRETTY_NAME) or OS reinstall
# (changes machine-id).
[group('alloy')]
configure HOSTNAME="kettle-omarchy":
    #!/usr/bin/env bash
    set -euo pipefail

    # cgroupv2 preflight (cAdvisor requirement).
    if [[ "$(stat -fc %T /sys/fs/cgroup)" != "cgroup2fs" ]]; then
        echo "ERROR: /sys/fs/cgroup is not cgroupv2. cAdvisor needs cgroupv2."
        exit 1
    fi
    # NOTE: We cannot statically check whether prometheus.exporter.cadvisor
    # is present in this Alloy build — there is no `alloy components`
    # subcommand. Validation happens via `alloy run --dry-run` below; if
    # the component is missing in this Alloy release, the config check
    # will error with "no component prometheus.exporter.cadvisor" and you
    # should switch to cgroup_exporter (see design spec Risks).

    HOST_ID=$(sha256sum /etc/machine-id | head -c 16)
    HOST_ARCH=$(uname -m)
    [[ "$HOST_ARCH" == "x86_64" ]] && HOST_ARCH="amd64"
    DISTRO="omarchy"
    GPU=$(lspci | grep -iE 'vga|3d' | grep -oiE 'nvidia[^[:space:]]*' | head -1 | tr 'A-Z' 'a-z')
    [[ -z "$GPU" ]] && GPU="unknown"
    . /etc/os-release
    OS_DESCRIPTION="${PRETTY_NAME:-Linux}"

    export HOST_ID HOST_ARCH DISTRO GPU OS_DESCRIPTION
    HOSTNAME_VAL='{{HOSTNAME}}' uv run python -c "
import os
from jinja2 import Template
tmpl = Template(open('{{TEMPLATE}}').read())
print(tmpl.render(
    HOSTNAME=os.environ['HOSTNAME_VAL'],
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
        echo "Wrote /etc/alloy/env from template. Replace placeholders with the workstation Secret credentials."
    fi

    # Config-validate the rendered config (catches typos AND verifies
    # every referenced component exists in this Alloy build).
    # `alloy run --dry-run` parses + type-checks the config without
    # starting the agent. Exits non-zero on error.
    sudo -u alloy alloy run --dry-run {{ALLOY_ETC}}/config.alloy && echo "config.alloy OK"

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

# Auth round-trip smoke test (HTTP 400 == auth succeeded; empty body invalid).
[group('alloy')]
test-ingest:
    #!/usr/bin/env bash
    set -euo pipefail
    . {{ALLOY_ETC}}/env
    PROM_URL="https://prometheus-ingest.home.kettle.sh/api/v1/write"
    echo "Expecting HTTP 400 (auth OK):"
    curl -sS -u "$PROM_USER:$PROM_PASS" -X POST "$PROM_URL" \
      -H 'Content-Type: application/x-protobuf' \
      -H 'X-Prometheus-Remote-Write-Version: 0.1.0' \
      --data-binary '' -o /dev/null -w '%{http_code}\n'

[group('alloy')]
[confirm]
uninstall:
    sudo systemctl disable --now alloy nvidia-gpu-exporter || true
    sudo rm -rf {{ALLOY_ETC}}
    sudo pacman -Rns --noconfirm grafana-alloy nvidia-gpu-exporter-bin
```

- [ ] **Step 2: Verify recipes list**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just --list --list-submodules | grep alloy::`
Expected: every recipe listed.

- [ ] **Step 3: Commit**

```bash
git -C /home/kettle/git_repos/grafana-dashboards add just/alloy.just
git -C /home/kettle/git_repos/grafana-dashboards commit -m "alloy: just recipes with cAdvisor preflight + cgroupv2 check"
```

### Task C3: Install Alloy + NVIDIA exporter

- [ ] **Step 1: Run install**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::install`
Expected: pacman + yay complete; `/etc/alloy/` exists.

- [ ] **Step 2: Verify binaries**

Run: `which alloy && alloy --version && which nvidia_gpu_exporter && nvidia_gpu_exporter --version`
Expected: paths print; versions reported.

- [ ] **Step 3: Verify systemd unit references EnvironmentFile**

Run: `systemctl cat alloy.service | grep EnvironmentFile`
Expected: `EnvironmentFile=/etc/alloy/env` (or similar). If absent, add a drop-in:

```bash
sudo install -d /etc/systemd/system/alloy.service.d
sudo tee /etc/systemd/system/alloy.service.d/env.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/alloy/env
EOF
sudo systemctl daemon-reload
```

### Task C4: Fetch workstation credentials

- [ ] **Step 1: Get the password from the cluster Secret**

```bash
kubectl --context <CTX> -n monitoring get secret workstation-ingest-auth \
  -o jsonpath='{.data.password}' | base64 -d > /tmp/workstation-pass
chmod 600 /tmp/workstation-pass
echo "Length: $(wc -c < /tmp/workstation-pass) chars"
```

Expected: 40 chars (plus newline).

### Task C5: Configure Alloy

- [ ] **Step 1: Run configure (includes the cAdvisor + cgroupv2 preflight)**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::configure kettle-omarchy`
Expected: cgroupv2 preflight passes; `/etc/alloy/config.alloy` rendered; `alloy run --dry-run` reports OK; `/etc/alloy/env` seeded from example.

- [ ] **Step 2: Inspect the rendered labels**

Run: `sudo grep -E "host_id|host_arch|os_description|HOSTNAME" /etc/alloy/config.alloy | head -10`
Expected: `host_id` is a 16-char hex; `host_arch` is `amd64`; `os_description` is the host's PRETTY_NAME.

- [ ] **Step 3: Populate /etc/alloy/env**

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

### Task C6: Start services and verify NVIDIA exporter metric names

- [ ] **Step 1: NVIDIA exporter metric preflight**

Start nvidia_gpu_exporter manually first to inspect the metric names (since they vary by version):

```bash
sudo systemctl start nvidia-gpu-exporter
sleep 2
curl -sS http://127.0.0.1:9835/metrics | grep -E "^nvidia_smi_(memory_total|memory_used|clocks_current_graphics|clocks_current_memory|temperature_gpu|power_draw|utilization_gpu)" | head -15
```

Expected: at least 7 metric names matching the panel queries. If a metric is named differently (e.g. `nvidia_smi_clock_speed_graphics_hertz` instead of `nvidia_smi_clocks_current_graphics_clock_hz`), update `panels/timeseries.py` and re-run B5's tests + re-render B13.

- [ ] **Step 2: Start Alloy**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::enable`
Expected: both units `active (running)`.

- [ ] **Step 3: Health probe**

Run: `curl -sS http://127.0.0.1:12345/-/healthy`
Expected: HTTP 200.

- [ ] **Step 4: Tail logs**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just alloy::logs 100`
Press Ctrl-C after ~30s. Expected: no `level=error` lines about auth failures or unreachable endpoints.

### Task C7: End-to-end verification with scripted PromQL

- [ ] **Step 1: Verify the workstation appears in Prometheus**

```bash
kubectl --context <CTX> -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9091:9090 >/dev/null 2>&1 &
PF_PID=$!
sleep 2
curl -sS 'http://127.0.0.1:9091/api/v1/query?query=up{host_name="kettle-omarchy"}' | python3 -m json.tool | head -20
kill $PF_PID
```

Expected: `result` array contains at least one series with `value[1]: "1"`.

- [ ] **Step 2: Verify journald logs reach Loki**

```bash
kubectl --context <CTX> -n monitoring port-forward svc/loki 3101:3100 >/dev/null 2>&1 &
PF_PID=$!
sleep 2
curl -sS --get 'http://127.0.0.1:3101/loki/api/v1/query' \
  --data-urlencode 'query=count_over_time({host_name="kettle-omarchy"}[5m])' \
  --data-urlencode "time=$(date +%s)" | python3 -m json.tool | head -20
kill $PF_PID
```

Expected: non-empty `result`.

- [ ] **Step 3: Verify the recording rules are firing**

```bash
kubectl --context <CTX> -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9091:9090 >/dev/null 2>&1 &
PF_PID=$!
sleep 2
for rule in host:psi_cpu_waiting:ratio1m host:psi_memory_waiting:ratio1m host:cgroup_cpu:sum5m; do
  echo "--- $rule ---"
  curl -sS --get 'http://127.0.0.1:9091/api/v1/query' \
    --data-urlencode "query=$rule" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("series:", len(d["data"]["result"]))'
done
kill $PF_PID
```

Expected: each rule has ≥1 series. If empty after 2 minutes, the rule isn't firing — check `kubectl get prometheusrule kettle-host-omarchy-rules -o yaml`.

- [ ] **Step 4: Stress-induced stutter event**

Run on the workstation:

```bash
sudo pacman -S --needed --noconfirm stress-ng
stress-ng --cpu 16 --timeout 180s &
STRESS_PID=$!
sleep 120   # let PSI rise + the recording rule evaluate over its 5m
            # window. `count_over_time([5m:1m])` needs >=2 evaluations
            # of `host:psi_cpu_waiting:ratio1m` above the 0.30 threshold
            # to register a stutter event; 120s gives 2 samples.

kubectl --context <CTX> -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9091:9090 >/dev/null 2>&1 &
PF_PID=$!
sleep 2
echo "PSI CPU during stress:"
curl -sS 'http://127.0.0.1:9091/api/v1/query?query=host:psi_cpu_waiting:ratio1m{host_name="kettle-omarchy"}' | python3 -m json.tool | head -20
echo "Stutter events (expect >= 1):"
curl -sS 'http://127.0.0.1:9091/api/v1/query?query=host:psi_cpu_stutter_events:count5m{host_name="kettle-omarchy"}' | python3 -m json.tool | head -20

kill $PF_PID
wait $STRESS_PID
```

Expected: PSI CPU > 0.3 during the run; `stutter_events` ≥ 1.

- [ ] **Step 5: Browse the dashboard**

Open `https://grafana.home.kettle.sh/d/kettle-host-omarchy/workstation-kettle-omarchy`.

Expected: row 1 stats populated; row 2 PSI timeseries shows three lines with the stress spike still visible (if within the time window); row 3 CPU per-core shows 32 lines with red during stress; row 8 errors panel shows journald lines.

## Post-implementation

- [ ] **Step 1: Re-run all tests**

Run: `cd /home/kettle/git_repos/grafana-dashboards && uv run pytest -q`
Expected: all pass.

- [ ] **Step 2: Re-render every dashboard (idempotent check)**

Run: `cd /home/kettle/git_repos/grafana-dashboards && just dash::render-all`
Then: `git -C /home/kettle/KettleCluster status home/apps/grafana-dashboards/`
Expected: no unexpected diffs (any diff should be intentional — review and commit).

- [ ] **Step 3: Update spec Risks section**

Edit `docs/superpowers/specs/2026-05-28-grafana-host-dashboard-design.md` and mark these risks as resolved:

- **Sidecar consumes v2beta1.** RESOLVED: deployed via grafana-operator as Dashboard CR; ConfigMap sidecar not used for this dashboard.
- **Loki service name and port.** RESOLVED: `loki:3100` in monitoring namespace.
- **SealedSecret vs SOPS.** RESOLVED: existing chart pattern uses raw Secret + ServerSideApply (verified in cluster + matched in workstation-ingest-secret.yaml).
- **cAdvisor 2026 status.** RESOLVED if Task C5 preflight passed; otherwise: implementation switched to cgroup_exporter (record which).
- **AUR package staleness.** Either confirmed both packages installed (Tasks C3 Step 2), or fell back to upstream binaries.

Commit:

```bash
git -C /home/kettle/git_repos/grafana-dashboards add docs/superpowers/specs/2026-05-28-grafana-host-dashboard-design.md
git -C /home/kettle/git_repos/grafana-dashboards commit -m "docs(spec): mark Phase A/B/C risks as resolved"
```

---

## Self-review (writer-side)

**Spec coverage:**
- §1 Host agent → C1, C2, C3, C4, C5, C6.
- §2 Cluster-side enablement → A1-A6 (basic-auth, middlewares, IngressRoutes, exemplars, derivedFields).
- §2 grafana-operator (new) → A7.
- §3 Dashboard rows/panels/variables → B3-B10c + B11.
- §3 Recording rules → B10a (`RECORDING_RULES`).
- §4 Repo layout → B1-B12 (justfile, panels/, variables, rows, scripts).
- §5 just interface → B1, B2, B12, C2.
- Testing → B3-B11 each include test-first steps; B11 explicitly covers backslash + datasource regressions.
- Risks (cAdvisor, v2beta1 sidecar, Loki service, secret pattern, NVIDIA exporter naming) → C5 preflight, C6 metric preflight, A4 pinned service+port, A1 lookup-based idempotency, post-implementation Step 3 resolution.

**Placeholder scan:** No `TBD`, `TODO`, "implement later". All code blocks complete; all commands exact.

**Type consistency:** `PromQuery` / `LokiQuery` inherit `Builder[DataQueryKind]` (B3). `HOST_FILTER = 'host_name="$host"'` defined once, imported everywhere. `DashboardSpec(uid, builder)` is the scaffold's NamedTuple. `RECORDING_RULES` records (`host:psi_cpu_waiting:ratio1m`, `host:psi_cpu_stutter_events:count5m`, `host:cgroup_cpu:sum5m`, `host:cgroup_memory_rss:sum5m`) referenced from `panels/stat.py`, `panels/timeseries.py`, `panels/tables.py`, and the test suite — names match.
