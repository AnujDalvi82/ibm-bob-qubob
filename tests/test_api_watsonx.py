"""Tests for the QUBOB REST API endpoints and watsonx Orchestrate integration.

Covers:
- POST /api/v1/analyze  (qubob_analyze action)
- POST /api/v1/optimize (qubob_optimize action)
- GET  /openapi.json    (OpenAPI 3.0 schema)
- Cluster registry behaviour (built-in fallbacks, unknown cluster_id, env var override)
- Regression guard: existing GET /api/topology route unchanged

All tests use FastAPI's TestClient (synchronous ASGI test transport) and are
self-contained — no fixtures, no conftest.py, no filesystem manifests required.
The "demo" cluster_id resolves to a synthetic 10-service topology.
"""

from __future__ import annotations

import json
import os

import pytest

# ---------------------------------------------------------------------------
# Guard — skip entire module if FastAPI / httpx are not installed
# ---------------------------------------------------------------------------
fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from bob_optimizer.dashboard.app import (  # noqa: E402
    CLUSTER_REGISTRY,
    _BUILTIN_REGISTRY,
    _load_cluster_registry,
    create_app,
)


# ---------------------------------------------------------------------------
# Shared test client helper
# ---------------------------------------------------------------------------


def _make_client() -> TestClient:
    """Return a TestClient wrapping a freshly created QUBOB app."""
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Cluster Registry tests
# ---------------------------------------------------------------------------


class TestClusterRegistry:
    def test_demo_always_present(self) -> None:
        assert "demo" in CLUSTER_REGISTRY

    def test_ecommerce_always_present(self) -> None:
        assert "ecommerce" in CLUSTER_REGISTRY

    def test_demo_maps_to_synthetic_sentinel(self) -> None:
        assert CLUSTER_REGISTRY["demo"] == "__synthetic__"

    def test_env_var_overrides_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "QUBOB_CLUSTER_REGISTRY_JSON",
            json.dumps({"my-cluster": "/tmp/manifests"}),
        )
        registry = _load_cluster_registry()
        assert registry["my-cluster"] == "/tmp/manifests"
        # Built-in fallbacks still present
        assert "demo" in registry
        assert "ecommerce" in registry

    def test_malformed_env_var_falls_back_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUBOB_CLUSTER_REGISTRY_JSON", "{not valid json}")
        registry = _load_cluster_registry()
        # Built-ins survive even with malformed override
        assert "demo" in registry

    def test_non_dict_env_var_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUBOB_CLUSTER_REGISTRY_JSON", '["list", "not", "dict"]')
        registry = _load_cluster_registry()
        assert "demo" in registry


# ---------------------------------------------------------------------------
# POST /api/v1/analyze
# ---------------------------------------------------------------------------


class TestAnalyzeEndpoint:
    def test_analyze_demo_cluster_returns_200(self) -> None:
        client = _make_client()
        resp = client.post("/api/v1/analyze", json={"cluster_id": "demo"})
        assert resp.status_code == 200

    def test_analyze_demo_has_services_and_nodes(self) -> None:
        client = _make_client()
        data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        assert data["n_services"] > 0
        assert data["n_nodes"] > 0

    def test_analyze_demo_cluster_id_echoed(self) -> None:
        client = _make_client()
        data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        assert data["cluster_id"] == "demo"

    def test_analyze_demo_has_dependencies(self) -> None:
        client = _make_client()
        data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        assert data["n_dependencies"] >= 0

    def test_analyze_demo_has_bottlenecks(self) -> None:
        client = _make_client()
        data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        assert isinstance(data["bottlenecks"], list)
        # Demo topology has dependencies — expect at least one bottleneck
        assert len(data["bottlenecks"]) > 0

    def test_analyze_demo_bottleneck_fields(self) -> None:
        client = _make_client()
        data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        edge = data["bottlenecks"][0]
        assert "source" in edge
        assert "target" in edge
        assert "calls_per_second" in edge
        assert "protocol" in edge

    def test_analyze_demo_latency_matrix_shape(self) -> None:
        client = _make_client()
        data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        mat = data["latency_matrix"]
        n_nodes = data["n_nodes"]
        assert len(mat) == n_nodes
        assert all(len(row) == n_nodes for row in mat)

    def test_analyze_demo_latency_matrix_diagonal_zero(self) -> None:
        client = _make_client()
        data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        mat = data["latency_matrix"]
        for i, row in enumerate(mat):
            assert row[i] == pytest.approx(0.0), f"Diagonal entry [{i}][{i}] should be 0 ms"

    def test_analyze_unknown_cluster_returns_404(self) -> None:
        client = _make_client()
        resp = client.post("/api/v1/analyze", json={"cluster_id": "does-not-exist"})
        assert resp.status_code == 404

    def test_analyze_404_detail_mentions_cluster_id(self) -> None:
        client = _make_client()
        resp = client.post("/api/v1/analyze", json={"cluster_id": "ghost-cluster"})
        detail = resp.json().get("detail", "")
        assert "ghost-cluster" in detail

    def test_analyze_missing_cluster_id_returns_422(self) -> None:
        client = _make_client()
        resp = client.post("/api/v1/analyze", json={})
        assert resp.status_code == 422

    def test_analyze_ecommerce_fallback_works(self) -> None:
        """ecommerce cluster_id should resolve even if examples/ecommerce/ is absent."""
        client = _make_client()
        resp = client.post("/api/v1/analyze", json={"cluster_id": "ecommerce"})
        assert resp.status_code == 200
        assert resp.json()["n_services"] > 0


# ---------------------------------------------------------------------------
# POST /api/v1/optimize
# ---------------------------------------------------------------------------


class TestOptimizeEndpoint:
    def test_optimize_demo_greedy_returns_200(self) -> None:
        client = _make_client()
        resp = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        )
        assert resp.status_code == 200

    def test_optimize_demo_greedy_assignments_count(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        ).json()
        analyze_data = client.post("/api/v1/analyze", json={"cluster_id": "demo"}).json()
        assert len(data["assignments"]) == analyze_data["n_services"]

    def test_optimize_demo_greedy_assignment_fields(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        ).json()
        for assignment in data["assignments"]:
            assert assignment["service_name"]
            assert assignment["node_name"]
            assert "zone" in assignment

    def test_optimize_demo_greedy_is_feasible(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        ).json()
        assert data["is_feasible"] is True

    def test_optimize_demo_greedy_solver_name(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        ).json()
        assert data["solver_name"] == "GREEDY"

    def test_optimize_demo_qiea_returns_200(self) -> None:
        client = _make_client()
        resp = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "qiea", "generations": 50, "seed": 7},
        )
        assert resp.status_code == 200

    def test_optimize_demo_qiea_solver_name(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "qiea", "generations": 50, "seed": 7},
        ).json()
        assert data["solver_name"] == "QIEA"

    def test_optimize_demo_qiea_is_feasible(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "qiea", "generations": 50, "seed": 7},
        ).json()
        assert data["is_feasible"] is True

    def test_optimize_demo_ga_returns_200(self) -> None:
        client = _make_client()
        resp = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "ga", "generations": 30, "seed": 0},
        )
        assert resp.status_code == 200

    def test_optimize_cost_fields_are_non_negative(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        ).json()
        assert data["total_cost"] >= 0.0
        assert data["latency_cost"] >= 0.0
        assert data["resource_cost"] >= 0.0
        assert data["affinity_cost"] >= 0.0
        assert data["penalty_cost"] >= 0.0

    def test_optimize_solve_time_positive(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        ).json()
        assert data["solve_time_s"] >= 0.0

    def test_optimize_cluster_id_echoed(self) -> None:
        client = _make_client()
        data = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "greedy"},
        ).json()
        assert data["cluster_id"] == "demo"

    def test_optimize_seed_reproducibility(self) -> None:
        """Same seed must produce identical assignments."""
        client = _make_client()
        payload = {"cluster_id": "demo", "algorithm": "greedy", "seed": 99}
        run1 = client.post("/api/v1/optimize", json=payload).json()["assignments"]
        run2 = client.post("/api/v1/optimize", json=payload).json()["assignments"]
        assert run1 == run2

    def test_optimize_unknown_cluster_returns_404(self) -> None:
        client = _make_client()
        resp = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "ghost-cluster", "algorithm": "greedy"},
        )
        assert resp.status_code == 404

    def test_optimize_invalid_algorithm_returns_422(self) -> None:
        client = _make_client()
        resp = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "demo", "algorithm": "bogus-solver"},
        )
        assert resp.status_code == 422

    def test_optimize_missing_cluster_id_returns_422(self) -> None:
        client = _make_client()
        resp = client.post("/api/v1/optimize", json={"algorithm": "greedy"})
        assert resp.status_code == 422

    def test_optimize_ecommerce_fallback_works(self) -> None:
        client = _make_client()
        resp = client.post(
            "/api/v1/optimize",
            json={"cluster_id": "ecommerce", "algorithm": "greedy"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_feasible"] is True


# ---------------------------------------------------------------------------
# GET /openapi.json — OpenAPI 3.0 schema
# ---------------------------------------------------------------------------


class TestOpenAPISchema:
    def test_openapi_returns_200(self) -> None:
        client = _make_client()
        resp = client.get("/openapi.json")
        assert resp.status_code == 200

    def test_openapi_has_analyze_path(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        assert "/api/v1/analyze" in schema["paths"]

    def test_openapi_has_optimize_path(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        assert "/api/v1/optimize" in schema["paths"]

    def test_openapi_analyze_operation_id(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/api/v1/analyze"]["post"]
        assert op["operationId"] == "qubob_analyze"

    def test_openapi_optimize_operation_id(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/api/v1/optimize"]["post"]
        assert op["operationId"] == "qubob_optimize"

    def test_openapi_has_components_schemas(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        assert "components" in schema
        assert "schemas" in schema["components"]

    def test_openapi_title_is_set(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        assert "QUBOB" in schema["info"]["title"]

    def test_openapi_analyze_has_post_method(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        assert "post" in schema["paths"]["/api/v1/analyze"]

    def test_openapi_optimize_has_post_method(self) -> None:
        client = _make_client()
        schema = client.get("/openapi.json").json()
        assert "post" in schema["paths"]["/api/v1/optimize"]


# ---------------------------------------------------------------------------
# Regression guard — existing routes unchanged
# ---------------------------------------------------------------------------


class TestExistingRoutesUnchanged:
    def test_topology_route_still_returns_200(self) -> None:
        client = _make_client()
        resp = client.get("/api/topology")
        assert resp.status_code == 200

    def test_topology_route_has_services_key(self) -> None:
        client = _make_client()
        data = client.get("/api/topology").json()
        assert "services" in data

    def test_topology_route_has_nodes_key(self) -> None:
        client = _make_client()
        data = client.get("/api/topology").json()
        assert "nodes" in data

    def test_topology_route_has_solver_results(self) -> None:
        client = _make_client()
        data = client.get("/api/topology").json()
        assert "solver_results" in data

    def test_dashboard_root_returns_html(self) -> None:
        client = _make_client()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
