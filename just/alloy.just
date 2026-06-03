# Grafana Alloy workstation host-agent provisioning.
#
# Installs + configures Alloy on the LOCAL workstation (Omarchy/Arch) so it
# ships host metrics + journald logs to the cluster's Prometheus/Loki via the
# prometheus-ingest / loki-ingest Traefik routes. Run from the home/ repo root:
# `just alloy::install`, `just alloy::configure`, etc.
#
# Arch packaging note: the `grafana-alloy` package uses the name
# `grafana-alloy` for the user/group/service/binary (NOT `alloy`), config dir
# `/etc/grafana-alloy`, and validates via `grafana-alloy validate`. The sysusers
# hook creates the `grafana-alloy` user automatically.
#
# The dashboard that consumes these metrics is generated separately by the
# grafana-dashboards repo (kgd / `just dash::*`); this module only handles the
# host-side agent.

ALLOY_USER  := "grafana-alloy"
ALLOY_SVC   := "grafana-alloy"
ALLOY_BIN   := "grafana-alloy"
NVIDIA_SVC  := "nvidia-gpu-exporter"
ALLOY_DIR   := "/etc/grafana-alloy"
CONFIG_FILE := "/etc/grafana-alloy/config.alloy"
ENV_FILE    := "/etc/grafana-alloy/env"
DROPIN      := "/etc/systemd/system/grafana-alloy.service.d/10-kettle.conf"
TEMPLATE    := "workstation/alloy/config.alloy.j2"
ENV_EXAMPLE := "workstation/alloy/env.example"

# Install grafana-alloy (extra repo) + nvidia-gpu-exporter-bin (AUR).
# The package's sysusers hook creates the grafana-alloy user; we only need to
# create the config dir. Idempotent. Needs your sudo password + yay (AUR).
[group('alloy')]
[no-cd]
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
    # The grafana-alloy package's sysusers.d hook creates the grafana-alloy
    # user; just ensure the config dir exists (CONFIG_FILE=/etc/grafana-alloy
    # per /etc/default/grafana-alloy).
    sudo install -d -o {{ALLOY_USER}} -g {{ALLOY_USER}} -m 0755 {{ALLOY_DIR}}
    # loki.source.journal needs read access to the systemd journal; the
    # grafana-alloy user must be in the systemd-journal group or the component
    # is unhealthy and no host logs reach Loki. Idempotent.
    sudo usermod -aG systemd-journal {{ALLOY_USER}}
    echo "Installed. Next: just alloy::configure"

# Render config.alloy.j2 -> /etc/grafana-alloy/config.alloy and wire a systemd
# drop-in so the service reads /etc/grafana-alloy/env (for the basic-auth
# creds + stability level). Idempotent. Re-run after distro upgrade
# (changes PRETTY_NAME) or OS reinstall (changes machine-id).
[group('alloy')]
[no-cd]
configure HOSTNAME="kettle-omarchy":
    #!/usr/bin/env bash
    set -euo pipefail

    # cgroupv2 preflight (cAdvisor requirement).
    if [[ "$(stat -fc %T /sys/fs/cgroup)" != "cgroup2fs" ]]; then
        echo "ERROR: /sys/fs/cgroup is not cgroupv2. cAdvisor needs cgroupv2."
        exit 1
    fi

    HOST_ID=$(sha256sum /etc/machine-id | head -c 16)
    HOST_ARCH=$(uname -m)
    [[ "$HOST_ARCH" == "x86_64" ]] && HOST_ARCH="amd64"
    DISTRO="omarchy"
    GPU=$(lspci | grep -iE 'vga|3d' | grep -oiE 'nvidia[^[:space:]]*' | head -1 | tr 'A-Z' 'a-z')
    [[ -z "$GPU" ]] && GPU="unknown"
    . /etc/os-release
    OS_DESCRIPTION="${PRETTY_NAME:-Linux}"

    export HOST_ID HOST_ARCH DISTRO GPU OS_DESCRIPTION
    export HOSTNAME_VAL='{{HOSTNAME}}'
    export TEMPLATE_PATH='{{TEMPLATE}}'
    # home/ has no uv project env; render the Jinja template in an ephemeral env.
    uv run --with jinja2 python -c "
    import os
    from jinja2 import Template
    tmpl = Template(open(os.environ['TEMPLATE_PATH']).read())
    print(tmpl.render(
        HOSTNAME=os.environ['HOSTNAME_VAL'],
        HOST_ID=os.environ['HOST_ID'],
        HOST_ARCH=os.environ['HOST_ARCH'],
        OS_DESCRIPTION=os.environ['OS_DESCRIPTION'],
        DISTRO=os.environ['DISTRO'],
        GPU=os.environ['GPU'],
    ))
    " > /tmp/config.alloy.rendered
    sudo install -d -o {{ALLOY_USER}} -g {{ALLOY_USER}} -m 0755 {{ALLOY_DIR}}
    sudo install -o {{ALLOY_USER}} -g {{ALLOY_USER}} -m 0644 /tmp/config.alloy.rendered {{CONFIG_FILE}}
    rm -f /tmp/config.alloy.rendered

    # Seed the env file (creds + stability level) if absent. Components like
    # prometheus.exporter.cadvisor / loki.source.journal may be public-preview,
    # so run with a permissive stability level via CUSTOM_ARGS (overrides the
    # empty default in /etc/default/grafana-alloy).
    if [[ ! -f {{ENV_FILE}} ]]; then
        sudo install -o {{ALLOY_USER}} -g {{ALLOY_USER}} -m 0600 {{ENV_EXAMPLE}} {{ENV_FILE}}
        echo 'CUSTOM_ARGS=--stability.level=experimental' | sudo tee -a {{ENV_FILE}} >/dev/null
        echo "Wrote {{ENV_FILE}} from template. Run `just alloy::fetch-creds` to fill the password."
    fi

    # systemd drop-in: add the env file as a second EnvironmentFile so the
    # service picks up PROM_USER/PASS/LOKI_USER/PASS + CUSTOM_ARGS.
    # AmbientCapabilities=CAP_SYS_PTRACE lets the (non-root grafana-alloy)
    # process read /proc/<pid>/smaps for processes owned by OTHER users — needed
    # so process-exporter's PSS (proportionalResident) is populated for your
    # apps (chrome/claude/Hyprland run as your user, not grafana-alloy).
    # cap_sys_ptrace is already in the unit's bounding set and NoNewPrivileges=no,
    # so granting it ambiently just works.
    sudo install -d /etc/systemd/system/grafana-alloy.service.d
    sudo tee {{DROPIN}} >/dev/null <<'EOF'
    [Service]
    EnvironmentFile=/etc/grafana-alloy/env
    AmbientCapabilities=CAP_SYS_PTRACE
    EOF
    sudo systemctl daemon-reload

    # Validate (catches typos + verifies every referenced component exists in
    # this Alloy build at the chosen stability level).
    {{ALLOY_BIN}} validate --stability.level=experimental {{CONFIG_FILE}} && echo "config OK"

    # Restart so a running agent picks up the new config — `systemctl reload`
    # (SIGHUP) does NOT re-read the EnvironmentFile and River component graph
    # reliably. Only restart if already active; first-time configure (before
    # `enable`) skips this so it doesn't error.
    if systemctl is-active --quiet {{ALLOY_SVC}}; then
        sudo systemctl restart {{ALLOY_SVC}}
        echo "restarted {{ALLOY_SVC}} (picked up new config)"
    else
        echo "{{ALLOY_SVC}} not running yet — start it with: just alloy::enable"
    fi

# One-shot: fetch creds + write env + enable both services in a single sudo
# prompt. Use after `install` + `configure`. CTX is the kettle kube context
# (e.g. admin@KettleCluster). This is fetch-creds + enable collapsed so you
# only type your sudo password once.
[group('alloy')]
[no-cd]
setup CTX="admin@KettleCluster":
    #!/usr/bin/env bash
    set -euo pipefail
    PW=$(kubectl --context {{CTX}} -n monitoring get secret workstation-ingest-credentials -o jsonpath='{.data.password}' | base64 -d)
    [[ -z "$PW" ]] && { echo "ERROR: empty password (wrong context? is it {{CTX}}?)"; exit 1; }
    echo "Fetched 40-char ingest password; writing env + enabling services (one sudo prompt)..."
    sudo bash -c "cat > {{ENV_FILE}} <<EOF
    PROM_USER=kettle-omarchy
    PROM_PASS=$PW
    LOKI_USER=kettle-omarchy
    LOKI_PASS=$PW
    CUSTOM_ARGS=--stability.level=experimental
    EOF
    chmod 600 {{ENV_FILE}}
    chown {{ALLOY_USER}}:{{ALLOY_USER}} {{ENV_FILE}}
    systemctl enable {{ALLOY_SVC}} {{NVIDIA_SVC}}
    # restart (not just enable --now) so an already-running agent re-reads the
    # EnvironmentFile — systemd only loads it on start, so rotated creds need a
    # full restart, not a SIGHUP reload.
    systemctl restart {{ALLOY_SVC}} {{NVIDIA_SVC}}"
    echo ""
    systemctl is-active {{ALLOY_SVC}} && systemctl is-active {{NVIDIA_SVC}}
    echo "Done. Check: just alloy::logs 80   |   just alloy::status"

# Fetch the workstation ingest password from the cluster Secret into the env
# file. Needs a working kubectl context for the cluster.
[group('alloy')]
[no-cd]
fetch-creds CTX="":
    #!/usr/bin/env bash
    set -euo pipefail
    ctx_flag=""
    [[ -n "{{CTX}}" ]] && ctx_flag="--context {{CTX}}"
    PW=$(kubectl $ctx_flag -n monitoring get secret workstation-ingest-credentials -o jsonpath='{.data.password}' | base64 -d)
    [[ -z "$PW" ]] && { echo "ERROR: empty password from secret"; exit 1; }
    sudo tee {{ENV_FILE}} >/dev/null <<EOF
    PROM_USER=kettle-omarchy
    PROM_PASS=$PW
    LOKI_USER=kettle-omarchy
    LOKI_PASS=$PW
    CUSTOM_ARGS=--stability.level=experimental
    EOF
    sudo chmod 600 {{ENV_FILE}}
    sudo chown {{ALLOY_USER}}:{{ALLOY_USER}} {{ENV_FILE}}
    echo "Wrote credentials to {{ENV_FILE}} (40-char password)."

[group('alloy')]
[no-cd]
enable:
    sudo systemctl enable --now {{ALLOY_SVC}} {{NVIDIA_SVC}}
    systemctl is-active {{ALLOY_SVC}}
    systemctl is-active {{NVIDIA_SVC}}

[group('alloy')]
[no-cd]
reload:
    sudo systemctl reload {{ALLOY_SVC}} || sudo systemctl restart {{ALLOY_SVC}}
    systemctl is-active {{ALLOY_SVC}}

[group('alloy')]
[no-cd]
status:
    systemctl status {{ALLOY_SVC}} --no-pager -n 20
    systemctl status {{NVIDIA_SVC}} --no-pager -n 10
    curl -sS http://127.0.0.1:12345/-/healthy || echo "Alloy UI not reachable on :12345"

[group('alloy')]
[no-cd]
logs N="200":
    journalctl -u {{ALLOY_SVC}} -n {{N}} -f --no-pager

# Auth round-trip smoke test (HTTP 400 == auth succeeded; empty body invalid).
[group('alloy')]
[no-cd]
test-ingest:
    #!/usr/bin/env bash
    set -euo pipefail
    . {{ENV_FILE}}
    PROM_URL="https://prometheus-ingest.home.kettle.sh/api/v1/write"
    echo "Expecting HTTP 400 (auth OK):"
    curl -sS -u "$PROM_USER:$PROM_PASS" -X POST "$PROM_URL" \
      -H 'Content-Type: application/x-protobuf' \
      -H 'X-Prometheus-Remote-Write-Version: 0.1.0' \
      --data-binary '' -o /dev/null -w '%{http_code}\n'

# NVIDIA exporter metric-name preflight: panel PromQL assumes specific
# nvidia_smi_* names that vary by exporter version. Run before relying on the
# GPU panels; if names differ, update panels/timeseries.py in grafana-dashboards.
[group('alloy')]
[no-cd]
gpu-metrics:
    #!/usr/bin/env bash
    set -euo pipefail
    sudo systemctl start {{NVIDIA_SVC}} 2>/dev/null || true
    sleep 2
    curl -sS http://127.0.0.1:9835/metrics 2>/dev/null \
      | grep -E '^nvidia_smi_(memory_total_bytes|memory_used_bytes|clocks_current_graphics_clock_hz|clocks_current_memory_clock_hz|temperature_gpu|power_draw_watts|utilization_gpu_ratio)' \
      | sort -u
    echo "--- (expect 7 names; if any differ, update panels/timeseries.py) ---"

[group('alloy')]
[no-cd]
[confirm]
uninstall:
    sudo systemctl disable --now {{ALLOY_SVC}} {{NVIDIA_SVC}} || true
    sudo rm -f {{DROPIN}}
    sudo systemctl daemon-reload || true
    sudo rm -rf {{ALLOY_DIR}}
    sudo pacman -Rns --noconfirm grafana-alloy nvidia-gpu-exporter-bin
