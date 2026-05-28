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
