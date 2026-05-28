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
    expr = _expr_of(stat_psi_cpu())
    assert "host:psi_cpu_waiting:ratio1m" in expr
    assert "clamp_max" in expr and "100" in expr
    assert 'host_name="$host"' in expr


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
