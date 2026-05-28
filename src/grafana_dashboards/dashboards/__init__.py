"""Registered dashboards.

Each module under this package defines one dashboard and registers a
build callable via :func:`register`. Generation happens by iterating
:func:`all_dashboards` and invoking each callable to obtain a
foundation-sdk builder.

Adding a dashboard:

1. Create ``src/grafana_dashboards/dashboards/<slug>.py``.
2. Define ``build()`` returning a ``DashboardSpec``.
3. Decorate it with ``@register("<slug>")``.
4. Add an import to :data:`_AUTOLOAD` below so the registry sees it.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import NamedTuple

from grafana_foundation_sdk.builders.dashboardv2beta1 import Dashboard


class DashboardSpec(NamedTuple):
    """A dashboard ready to be rendered.

    Attributes:
        uid: Stable identifier. Becomes ``metadata.name`` in the v2
            envelope and the output filename (``<uid>.json``).
        builder: A foundation-sdk Dashboard builder. ``.build()`` is
            called by the generator.
    """

    uid: str
    builder: Dashboard


_REGISTRY: dict[str, Callable[[], DashboardSpec]] = {}

# Modules that contribute dashboards. Listed explicitly so registry
# membership is a property of source, not import order.
_AUTOLOAD = (
    "grafana_dashboards.dashboards.service_health",
)


def register(slug: str) -> Callable[[Callable[[], DashboardSpec]], Callable[[], DashboardSpec]]:
    """Register a dashboard builder factory under ``slug``."""

    def decorator(fn: Callable[[], DashboardSpec]) -> Callable[[], DashboardSpec]:
        if slug in _REGISTRY:
            raise ValueError(f"dashboard slug already registered: {slug!r}")
        _REGISTRY[slug] = fn
        return fn

    return decorator


def all_dashboards() -> dict[str, Callable[[], DashboardSpec]]:
    """Return the registry, triggering autoload of dashboard modules."""
    for mod in _AUTOLOAD:
        importlib.import_module(mod)
    return dict(_REGISTRY)
