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
