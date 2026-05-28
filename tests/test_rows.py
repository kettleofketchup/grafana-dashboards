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
