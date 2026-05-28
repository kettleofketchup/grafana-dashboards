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
