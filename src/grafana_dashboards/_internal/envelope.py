from __future__ import annotations


def wrap_v2(spec: dict, *, uid: str) -> dict:
    """Wrap a foundation-sdk dashboard `spec` in the v2beta1 envelope.

    The SDK's `.build()` returns the spec body only. Grafana's v2 schema
    expects `{apiVersion, kind, metadata, spec}` at the root, and v2
    drops v1's `__inputs` import-time substitution — datasource selection
    is a runtime DatasourceVariable inside `spec.variables` instead.
    """
    return {
        "apiVersion": "dashboard.grafana.app/v2beta1",
        "kind": "Dashboard",
        "metadata": {"name": uid},
        "spec": spec,
    }
