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
