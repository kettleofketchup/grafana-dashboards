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
    # is the mean queue residence time per IO.
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
