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
