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
[no-cd]
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
    export HOSTNAME_VAL='{{HOSTNAME}}'
    export TEMPLATE_PATH='{{TEMPLATE}}'
    uv run python -c "
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
