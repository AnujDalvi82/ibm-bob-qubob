"""Comprehensive tests for manifest parsers, graph builder, QUBO cost engine,
and synthetic topology generator.

Coverage targets
----------------
* Kubernetes parser  (TestKubernetesParser)   — 10 tests
* Docker Compose parser (TestComposeParser)   —  6 tests
* Graph builder      (TestGraphBuilder)        —  6 tests
* QUBO cost engine   (TestQUBOCost)            — 11 tests
* Synthetic generator (TestSyntheticGenerator) —  7 tests
* End-to-end solver  (TestEndToEndSolverIntegration) — 4 tests
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import numpy as np
import pytest

from bob_optimizer.model.domain import (
    ClusterTopology,
    NodeCapacity,
    ResourceRequest,
    Service,
    ServiceDependency,
)
from bob_optimizer.model.graph import (
    RTT_CROSSZONE_MS,
    RTT_INTRAZONE_MS,
    RTT_LOOPBACK_MS,
    build_capacity_vector,
    build_latency_matrix,
    build_resource_demand_vector,
    build_service_graph,
)
from bob_optimizer.model.qubo import (
    _affinity_cost,
    _latency_cost,
    _penalty_cost,
    _resource_cost,
    build_objective,
    decompose_cost,
)
from bob_optimizer.parser.compose import parse_compose
from bob_optimizer.parser.kubernetes import (
    _parse_cpu,
    _parse_memory,
    parse_manifests,
)
from bob_optimizer.parser.synthetic_generator import (
    make_enterprise_topology,
    make_medium_topology,
    make_small_topology,
)
from bob_optimizer.solvers.baseline_ga import ClassicalGASolver
from bob_optimizer.solvers.greedy import GreedyFFDSolver
from bob_optimizer.solvers.qiea import QIEASolver


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_two_node_topology(
    *,
    anti_affinity: bool = False,
    cpu_demand: int = 100,
    node_cpu: int = 8000,
) -> ClusterTopology:
    """Small 2-service / 2-node topology for targeted unit tests."""
    svc_a = Service(
        name="svc-a",
        resources=ResourceRequest(cpu_millicores=cpu_demand, memory_mib=128),
        anti_affinity_services=["svc-b"] if anti_affinity else [],
    )
    svc_b = Service(
        name="svc-b",
        resources=ResourceRequest(cpu_millicores=cpu_demand, memory_mib=128),
        anti_affinity_services=["svc-a"] if anti_affinity else [],
    )
    node_0 = NodeCapacity(
        name="node-0",
        zone="us-east-1a",
        allocatable_cpu_millicores=node_cpu,
        latency_ms={"node-0": 0.0, "node-1": 8.0},
    )
    node_1 = NodeCapacity(
        name="node-1",
        zone="us-east-1b",
        allocatable_cpu_millicores=node_cpu,
        latency_ms={"node-0": 8.0, "node-1": 0.0},
    )
    dep = ServiceDependency(source="svc-a", target="svc-b", calls_per_second=10.0)
    return ClusterTopology(
        name="test",
        services=(svc_a, svc_b),
        nodes=(node_0, node_1),
        dependencies=(dep,),
    )


def _one_hot_vector(n_services: int, n_nodes: int, assignments: list[int]) -> np.ndarray:
    """Build a flat one-hot placement vector.

    assignments[i] = j means service i is placed on node j.
    """
    x = np.zeros(n_services * n_nodes, dtype=np.int8)
    for i, j in enumerate(assignments):
        x[i * n_nodes + j] = 1
    return x


# ===========================================================================
# Kubernetes Parser Tests
# ===========================================================================


class TestKubernetesParser:
    """Unit tests for bob_optimizer/parser/kubernetes.py."""

    # --- CPU / memory string parsing helpers ---

    def test_cpu_parsing_millicore_form(self) -> None:
        assert _parse_cpu("250m") == 250

    def test_cpu_parsing_fractional_core(self) -> None:
        assert _parse_cpu("0.5") == 500

    def test_cpu_parsing_whole_core(self) -> None:
        assert _parse_cpu("2") == 2000

    def test_memory_parsing_mi(self) -> None:
        assert _parse_memory("128Mi") == 128

    def test_memory_parsing_gi(self) -> None:
        assert _parse_memory("1Gi") == 1024

    def test_memory_parsing_plain_bytes(self) -> None:
        # 536870912 bytes = 512 MiB
        assert _parse_memory("536870912") == 512

    # --- Full manifest parsing ---

    def test_deployment_basic(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: frontend
              namespace: production
            spec:
              replicas: 2
              template:
                spec:
                  containers:
                    - name: app
                      image: nginx:1.25
                      resources:
                        requests:
                          cpu: "250m"
                          memory: "256Mi"
        """)
        p = tmp_path / "deploy.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        assert topo.n_services == 1
        svc = topo.services[0]
        assert svc.name == "frontend"
        assert svc.namespace == "production"
        assert svc.image == "nginx:1.25"
        assert svc.replicas == 2
        # resources scaled by replicas
        assert svc.resources.cpu_millicores == 500   # 250m * 2
        assert svc.resources.memory_mib == 512       # 256MiB * 2

    def test_resource_defaults(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: backend
            spec:
              template:
                spec:
                  containers:
                    - name: app
                      image: myapp:latest
        """)
        p = tmp_path / "deploy.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        svc = topo.services[0]
        # Default 100m * 1 replica = 100
        assert svc.resources.cpu_millicores == 100
        assert svc.resources.memory_mib == 128

    def test_multi_container_pod_sum(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: multi
            spec:
              replicas: 1
              template:
                spec:
                  containers:
                    - name: main
                      image: main:latest
                      resources:
                        requests:
                          cpu: "250m"
                          memory: "256Mi"
                    - name: sidecar
                      image: envoy:latest
                      resources:
                        requests:
                          cpu: "250m"
                          memory: "256Mi"
        """)
        p = tmp_path / "deploy.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        svc = topo.services[0]
        assert svc.resources.cpu_millicores == 500    # 250 + 250
        assert svc.resources.memory_mib == 512        # 256 + 256
        assert "sidecar" in svc.annotations.get("containers", "")

    def test_statefulset_replicas(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: apps/v1
            kind: StatefulSet
            metadata:
              name: postgres
            spec:
              replicas: 3
              template:
                spec:
                  containers:
                    - name: db
                      image: postgres:15
        """)
        p = tmp_path / "sts.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        assert topo.services[0].replicas == 3

    def test_anti_affinity_rule(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: api
            spec:
              template:
                spec:
                  affinity:
                    podAntiAffinity:
                      requiredDuringSchedulingIgnoredDuringExecution:
                        - labelSelector:
                            matchLabels:
                              app: worker
                          topologyKey: kubernetes.io/hostname
                  containers:
                    - name: api
                      image: api:latest
        """)
        p = tmp_path / "deploy.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        assert "worker" in topo.services[0].anti_affinity_services

    def test_node_affinity_required(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: gpu-job
            spec:
              template:
                spec:
                  affinity:
                    nodeAffinity:
                      requiredDuringSchedulingIgnoredDuringExecution:
                        nodeSelectorTerms:
                          - matchExpressions:
                              - key: accelerator
                                operator: In
                                values: ["nvidia-tesla-v100"]
                  containers:
                    - name: trainer
                      image: trainer:latest
        """)
        p = tmp_path / "deploy.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        assert topo.services[0].required_node_labels.get("accelerator") == "nvidia-tesla-v100"

    def test_env_url_dependency_inference(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: checkout
            spec:
              template:
                spec:
                  containers:
                    - name: app
                      image: checkout:latest
                      env:
                        - name: PAYMENT_URL
                          value: "http://payment/api"
            ---
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: payment
            spec:
              template:
                spec:
                  containers:
                    - name: app
                      image: payment:latest
        """)
        p = tmp_path / "deploy.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        dep_targets = {d.target for d in topo.dependencies}
        assert "payment" in dep_targets

    def test_unknown_kind_skipped(self, tmp_path: Path) -> None:
        manifest = textwrap.dedent("""\
            apiVersion: batch/v1
            kind: CronJob
            metadata:
              name: cleanup
            spec: {}
            ---
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: web
            spec:
              template:
                spec:
                  containers:
                    - name: app
                      image: web:latest
        """)
        p = tmp_path / "deploy.yaml"
        p.write_text(manifest)
        topo = parse_manifests([p])
        assert topo.n_services == 1
        assert topo.services[0].name == "web"


# ===========================================================================
# Docker Compose Parser Tests
# ===========================================================================


class TestComposeParser:
    """Unit tests for bob_optimizer/parser/compose.py."""

    def test_compose_service_basic(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            version: "3.9"
            services:
              web:
                image: nginx:1.25
                deploy:
                  replicas: 2
                  resources:
                    limits:
                      cpus: "0.5"
                      memory: "256m"
        """)
        p = tmp_path / "docker-compose.yml"
        p.write_text(content)
        topo = parse_compose(p)
        assert topo.n_services == 1
        svc = topo.services[0]
        assert svc.name == "web"
        assert svc.image == "nginx:1.25"
        assert svc.replicas == 2
        assert svc.resources.cpu_millicores == 1000   # 0.5 * 2
        assert svc.resources.memory_mib == 512        # 256m * 2

    def test_compose_resource_defaults(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            services:
              app:
                image: myapp:latest
        """)
        p = tmp_path / "docker-compose.yml"
        p.write_text(content)
        topo = parse_compose(p)
        svc = topo.services[0]
        assert svc.resources.cpu_millicores == 100
        assert svc.resources.memory_mib == 128

    def test_depends_on_list(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            services:
              api:
                image: api:latest
                depends_on:
                  - db
              db:
                image: postgres:15
        """)
        p = tmp_path / "docker-compose.yml"
        p.write_text(content)
        topo = parse_compose(p)
        dep_targets = {d.target for d in topo.dependencies}
        assert "db" in dep_targets

    def test_depends_on_dict(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            services:
              api:
                image: api:latest
                depends_on:
                  db:
                    condition: service_healthy
              db:
                image: postgres:15
        """)
        p = tmp_path / "docker-compose.yml"
        p.write_text(content)
        topo = parse_compose(p)
        dep_targets = {d.target for d in topo.dependencies}
        assert "db" in dep_targets

    def test_no_duplicate_edges(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            services:
              api:
                image: api:latest
                depends_on:
                  - db
                environment:
                  DB_URL: "http://db/query"
              db:
                image: postgres:15
        """)
        p = tmp_path / "docker-compose.yml"
        p.write_text(content)
        topo = parse_compose(p)
        api_to_db = [(d.source, d.target) for d in topo.dependencies if d.target == "db"]
        assert len(api_to_db) == 1

    def test_build_only_service(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            services:
              custom:
                build: .
        """)
        p = tmp_path / "docker-compose.yml"
        p.write_text(content)
        topo = parse_compose(p)
        assert topo.services[0].image == "build:custom"


# ===========================================================================
# Graph Builder Tests
# ===========================================================================


class TestGraphBuilder:
    """Unit tests for bob_optimizer/model/graph.py."""

    def _make_topology(self) -> ClusterTopology:
        return _make_two_node_topology()

    def test_service_graph_nodes(self) -> None:
        topo = self._make_topology()
        g = build_service_graph(topo)
        assert set(g.nodes) == {"svc-a", "svc-b"}

    def test_service_graph_node_attributes(self) -> None:
        topo = self._make_topology()
        g = build_service_graph(topo)
        assert g.nodes["svc-a"]["cpu_millicores"] == 100
        assert g.nodes["svc-a"]["memory_mib"] == 128

    def test_service_graph_edges(self) -> None:
        topo = self._make_topology()
        g = build_service_graph(topo)
        assert g.has_edge("svc-a", "svc-b")
        assert g["svc-a"]["svc-b"]["weight"] == pytest.approx(10.0)
        assert g["svc-a"]["svc-b"]["protocol"] == "http"

    def test_latency_matrix_shape(self) -> None:
        topo = self._make_topology()
        mat = build_latency_matrix(topo)
        assert mat.shape == (2, 2)
        assert mat.dtype == np.float64

    def test_latency_matrix_diagonal(self) -> None:
        topo = self._make_topology()
        mat = build_latency_matrix(topo)
        np.testing.assert_array_equal(np.diag(mat), [0.0, 0.0])

    def test_latency_matrix_symmetry(self) -> None:
        topo = self._make_topology()
        mat = build_latency_matrix(topo)
        np.testing.assert_allclose(mat, mat.T, atol=1e-12)

    def test_demand_vector_shape_and_values(self) -> None:
        topo = self._make_topology()
        demands = build_resource_demand_vector(topo)
        assert demands.shape == (2,)
        assert demands.dtype == np.float64
        np.testing.assert_array_equal(demands, [100.0, 100.0])

    def test_capacity_vector_shape_and_values(self) -> None:
        topo = self._make_topology()
        caps = build_capacity_vector(topo)
        assert caps.shape == (2,)
        np.testing.assert_array_equal(caps, [8000.0, 8000.0])


# ===========================================================================
# QUBO Cost Engine Tests
# ===========================================================================


class TestQUBOCost:
    """Unit tests for bob_optimizer/model/qubo.py."""

    def _topo(self, **kwargs: bool | int) -> ClusterTopology:  # type: ignore[override]
        return _make_two_node_topology(**kwargs)  # type: ignore[arg-type]

    # --- H_penalty ---

    def test_feasible_one_hot_zero_penalty(self) -> None:
        topo = self._topo()
        # Both services on node 0: x = [1,0, 1,0]
        x = _one_hot_vector(2, 2, [0, 0])
        costs = decompose_cost(topo, x)
        assert costs["penalty"] == pytest.approx(0.0)

    def test_infeasible_double_assign_penalty(self) -> None:
        topo = self._topo()
        # service 0 on both nodes: x = [1,1, 1,0]
        x = np.array([1, 1, 1, 0], dtype=np.int8)
        costs = decompose_cost(topo, x)
        assert costs["penalty"] > 0.0

    def test_unassigned_penalty(self) -> None:
        topo = self._topo()
        # service 0 unassigned: x = [0,0, 1,0]
        x = np.array([0, 0, 1, 0], dtype=np.int8)
        costs = decompose_cost(topo, x)
        assert costs["penalty"] > 0.0

    # --- H_latency ---

    def test_collocated_lower_latency(self) -> None:
        topo = self._topo()
        # Both on node 0 (same node) vs both on different nodes
        x_same = _one_hot_vector(2, 2, [0, 0])
        x_diff = _one_hot_vector(2, 2, [0, 1])
        costs_same = decompose_cost(topo, x_same)
        costs_diff = decompose_cost(topo, x_diff)
        # Cross-node assignment should have higher latency cost
        assert costs_diff["latency"] > costs_same["latency"]

    # --- H_resource ---

    def test_capacity_overflow_resource_cost(self) -> None:
        # Node capacity = 50 m, service demand = 100 m → overflow
        topo = _make_two_node_topology(cpu_demand=100, node_cpu=50)
        x = _one_hot_vector(2, 2, [0, 0])
        costs = decompose_cost(topo, x)
        assert costs["resource"] > 0.0

    def test_no_overflow_resource_cost(self) -> None:
        # Generous capacity (8000 m), small demand (100 m per service)
        topo = _make_two_node_topology(cpu_demand=100, node_cpu=8000)
        x = _one_hot_vector(2, 2, [0, 0])
        costs = decompose_cost(topo, x)
        assert costs["resource"] == pytest.approx(0.0)

    # --- H_affinity ---

    def test_anti_affinity_violation(self) -> None:
        topo = _make_two_node_topology(anti_affinity=True)
        # Both on node 0 → violates anti-affinity
        x = _one_hot_vector(2, 2, [0, 0])
        costs = decompose_cost(topo, x)
        assert costs["affinity"] > 0.0

    def test_anti_affinity_satisfied(self) -> None:
        topo = _make_two_node_topology(anti_affinity=True)
        # On different nodes → no violation
        x = _one_hot_vector(2, 2, [0, 1])
        costs = decompose_cost(topo, x)
        assert costs["affinity"] == pytest.approx(0.0)

    # --- build_objective ---

    def test_objective_fn_is_callable(self) -> None:
        topo = self._topo()
        obj = build_objective(topo)
        assert callable(obj)
        assert obj.__name__ == "qubo_objective"

    def test_objective_returns_float(self) -> None:
        topo = self._topo()
        obj = build_objective(topo)
        x = _one_hot_vector(2, 2, [0, 1])
        result = obj(x)
        assert isinstance(result, float)

    def test_decompose_cost_keys(self) -> None:
        topo = self._topo()
        x = _one_hot_vector(2, 2, [0, 0])
        costs = decompose_cost(topo, x)
        assert set(costs.keys()) == {"latency", "resource", "affinity", "penalty", "total"}

    def test_total_is_sum_of_components(self) -> None:
        topo = self._topo()
        x = _one_hot_vector(2, 2, [0, 1])
        costs = decompose_cost(topo, x)
        expected_total = (
            topo.lambda_latency * costs["latency"]
            + topo.lambda_resource * costs["resource"]
            + topo.lambda_affinity * costs["affinity"]
            + topo.lambda_penalty * costs["penalty"]
        )
        assert costs["total"] == pytest.approx(expected_total, rel=1e-9)


# ===========================================================================
# Synthetic Generator Tests
# ===========================================================================


class TestSyntheticGenerator:
    """Unit tests for bob_optimizer/parser/synthetic_generator.py."""

    def test_small_topology_sizes(self) -> None:
        topo = make_small_topology()
        assert topo.n_services == 8
        assert topo.n_nodes == 3

    def test_medium_topology_sizes(self) -> None:
        topo = make_medium_topology()
        assert topo.n_services == 24
        assert topo.n_nodes == 9

    def test_enterprise_topology_sizes(self) -> None:
        topo = make_enterprise_topology()
        assert topo.n_services == 64
        assert topo.n_nodes == 15

    def test_determinism(self) -> None:
        t1 = make_small_topology(seed=0)
        t2 = make_small_topology(seed=0)
        # Compare service names and resource values
        assert [s.name for s in t1.services] == [s.name for s in t2.services]
        for s1, s2 in zip(t1.services, t2.services):
            assert s1.resources.cpu_millicores == s2.resources.cpu_millicores
            assert s1.resources.memory_mib == s2.resources.memory_mib
        assert [n.name for n in t1.nodes] == [n.name for n in t2.nodes]

    def test_different_seeds_differ(self) -> None:
        t0 = make_small_topology(seed=0)
        t1 = make_small_topology(seed=1)
        cpu_0 = [s.resources.cpu_millicores for s in t0.services]
        cpu_1 = [s.resources.cpu_millicores for s in t1.services]
        assert cpu_0 != cpu_1

    def test_three_zone_distribution(self) -> None:
        for make_topo in (make_small_topology, make_medium_topology, make_enterprise_topology):
            topo = make_topo()
            zones = {n.zone for n in topo.nodes}
            assert len(zones) == 3, f"Expected 3 zones, got {zones}"

    def test_latency_values_small(self) -> None:
        topo = make_small_topology(seed=0)
        mat = build_latency_matrix(topo)
        nodes = topo.nodes
        for j in range(topo.n_nodes):
            for k in range(topo.n_nodes):
                if j == k:
                    assert mat[j, k] == pytest.approx(0.0)
                elif nodes[j].zone == nodes[k].zone:
                    assert mat[j, k] == pytest.approx(RTT_INTRAZONE_MS)
                else:
                    assert mat[j, k] == pytest.approx(RTT_CROSSZONE_MS)


# ===========================================================================
# End-to-End Solver Integration Tests
# ===========================================================================


class TestEndToEndSolverIntegration:
    """Integration tests: synthetic topology → build_objective → solver."""

    def _initial_cost(self, topology: ClusterTopology) -> float:
        """Cost of a random (likely infeasible) placement used as upper bound."""
        rng = np.random.default_rng(99)
        x_rand = rng.integers(0, 2, size=topology.n_variables, dtype=np.int8)
        obj = build_objective(topology)
        return obj(x_rand)

    def test_qiea_solves_small_topology(self) -> None:
        topo = make_small_topology(seed=7)
        obj = build_objective(topo)
        solver = QIEASolver(population_size=10, n_generations=50)
        result = solver.solve(obj, topo.n_variables, topo.n_nodes, seed=0)
        assert result.best_cost < 1e6   # convergence sanity (well below random)
        # One-hot feasibility: each service block sums to 1
        sol = result.best_solution
        for i in range(topo.n_services):
            block = sol[i * topo.n_nodes:(i + 1) * topo.n_nodes]
            assert int(block.sum()) == 1, f"Service {i} not one-hot: {block}"

    def test_ga_solves_small_topology(self) -> None:
        topo = make_small_topology(seed=7)
        obj = build_objective(topo)
        solver = ClassicalGASolver(population_size=20, n_generations=50)
        result = solver.solve(obj, topo.n_variables, topo.n_nodes, seed=0)
        assert result.best_cost < 1e6
        sol = result.best_solution
        for i in range(topo.n_services):
            block = sol[i * topo.n_nodes:(i + 1) * topo.n_nodes]
            assert int(block.sum()) == 1

    def test_greedy_solves_small_topology(self) -> None:
        topo = make_small_topology(seed=7)
        obj = build_objective(topo)
        demands = build_resource_demand_vector(topo)
        capacities = build_capacity_vector(topo)
        solver = GreedyFFDSolver(resource_demands=demands, node_capacities=capacities)
        result = solver.solve(obj, topo.n_variables, topo.n_nodes, seed=0)
        assert result.best_cost < 1e6
        sol = result.best_solution
        for i in range(topo.n_services):
            block = sol[i * topo.n_nodes:(i + 1) * topo.n_nodes]
            assert int(block.sum()) == 1

    def test_all_solvers_feasible_medium_topology(self) -> None:
        """All three solvers produce feasible placements on medium topology."""
        topo = make_medium_topology(seed=3)
        obj = build_objective(topo)
        demands = build_resource_demand_vector(topo)
        capacities = build_capacity_vector(topo)

        solvers = [
            QIEASolver(population_size=15, n_generations=30),
            ClassicalGASolver(population_size=20, n_generations=30),
            GreedyFFDSolver(resource_demands=demands, node_capacities=capacities),
        ]

        for solver in solvers:
            result = solver.solve(obj, topo.n_variables, topo.n_nodes, seed=42)
            sol = result.best_solution
            for i in range(topo.n_services):
                block = sol[i * topo.n_nodes:(i + 1) * topo.n_nodes]
                assert int(block.sum()) == 1, (
                    f"{solver.__class__.__name__}: service {i} not one-hot"
                )
