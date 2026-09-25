"""Smoke tests for foundation domain models (Step 1)."""

from __future__ import annotations

import pytest

from bob_optimizer.model.domain import (
    ClusterTopology,
    NodeCapacity,
    PlacementPlan,
    ResourceRequest,
    Service,
    ServiceAssignment,
    ServiceDependency,
)


# ---------------------------------------------------------------------------
# ResourceRequest
# ---------------------------------------------------------------------------


def test_resource_request_defaults() -> None:
    r = ResourceRequest()
    assert r.cpu_millicores == 100
    assert r.memory_mib == 128
    assert r.cpu_cores == 0.1


def test_resource_request_cpu_cores() -> None:
    r = ResourceRequest(cpu_millicores=500, memory_mib=256)
    assert r.cpu_cores == 0.5


def test_resource_request_invalid_cpu() -> None:
    with pytest.raises(Exception):
        ResourceRequest(cpu_millicores=0)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_service_basic() -> None:
    svc = Service(name="api-gateway", image="nginx:1.25", replicas=3)
    assert svc.name == "api-gateway"
    assert svc.replicas == 3


def test_service_name_lowercased() -> None:
    svc = Service(name="MyService")
    assert svc.name == "myservice"


def test_service_name_no_spaces() -> None:
    with pytest.raises(Exception):
        Service(name="bad name")


def test_service_replicas_minimum() -> None:
    with pytest.raises(Exception):
        Service(name="svc", replicas=0)


# ---------------------------------------------------------------------------
# NodeCapacity
# ---------------------------------------------------------------------------


def test_node_capacity_defaults() -> None:
    node = NodeCapacity(name="node-1")
    assert node.allocatable_cpu_millicores == 4000
    assert node.allocatable_memory_mib == 8192
    assert node.zone == "zone-a"


def test_node_latency_same_node() -> None:
    node = NodeCapacity(name="node-1")
    assert node.latency_to(node) == 0.0


def test_node_latency_cross_zone_fallback() -> None:
    n1 = NodeCapacity(name="node-1", zone="zone-a")
    n2 = NodeCapacity(name="node-2", zone="zone-b")
    # No measured latency → fallback 5.0 ms
    assert n1.latency_to(n2) == 5.0


def test_node_latency_same_zone_fallback() -> None:
    n1 = NodeCapacity(name="node-1", zone="zone-a")
    n2 = NodeCapacity(name="node-2", zone="zone-a")
    # Same zone, no measurement → fallback 0.5 ms
    assert n1.latency_to(n2) == 0.5


def test_node_latency_measured() -> None:
    n1 = NodeCapacity(name="node-1", zone="zone-a", latency_ms={"node-2": 1.2})
    n2 = NodeCapacity(name="node-2", zone="zone-b")
    assert n1.latency_to(n2) == 1.2


# ---------------------------------------------------------------------------
# ServiceDependency
# ---------------------------------------------------------------------------


def test_service_dependency_basic() -> None:
    dep = ServiceDependency(source="frontend", target="backend", calls_per_second=50.0)
    assert dep.weight == 50.0


def test_service_dependency_self_loop_rejected() -> None:
    with pytest.raises(Exception):
        ServiceDependency(source="svc", target="svc")


# ---------------------------------------------------------------------------
# ClusterTopology
# ---------------------------------------------------------------------------


def _make_topology() -> ClusterTopology:
    svc_a = Service(name="frontend")
    svc_b = Service(name="backend")
    node1 = NodeCapacity(name="node-1", zone="zone-a")
    node2 = NodeCapacity(name="node-2", zone="zone-b")
    dep = ServiceDependency(source="frontend", target="backend", calls_per_second=100.0)
    return ClusterTopology(
        services=(svc_a, svc_b),
        nodes=(node1, node2),
        dependencies=(dep,),
    )


def test_topology_dimensions() -> None:
    topo = _make_topology()
    assert topo.n_services == 2
    assert topo.n_nodes == 2
    assert topo.n_variables == 4


def test_topology_service_index() -> None:
    topo = _make_topology()
    assert topo.service_index("frontend") == 0
    assert topo.service_index("backend") == 1


def test_topology_variable_index() -> None:
    topo = _make_topology()
    # frontend on node-1 → 0 * 2 + 0 = 0
    assert topo.variable_index("frontend", "node-1") == 0
    # frontend on node-2 → 0 * 2 + 1 = 1
    assert topo.variable_index("frontend", "node-2") == 1
    # backend on node-1 → 1 * 2 + 0 = 2
    assert topo.variable_index("backend", "node-1") == 2


def test_topology_invalid_dependency_ref() -> None:
    svc = Service(name="frontend")
    node = NodeCapacity(name="node-1")
    dep = ServiceDependency(source="frontend", target="ghost-service")
    with pytest.raises(Exception):
        ClusterTopology(services=(svc,), nodes=(node,), dependencies=(dep,))


# ---------------------------------------------------------------------------
# PlacementPlan
# ---------------------------------------------------------------------------


def test_placement_plan_basic() -> None:
    plan = PlacementPlan(
        assignments=(
            ServiceAssignment(service_name="frontend", node_name="node-1", zone="zone-a"),
            ServiceAssignment(service_name="backend", node_name="node-2", zone="zone-b"),
        ),
        total_cost=42.0,
        solver_name="qiea",
        solve_time_s=0.35,
    )
    assert plan.node_for("frontend") == "node-1"
    assert plan.node_for("backend") == "node-2"
    assert plan.services_on_node("node-1") == ["frontend"]
    assert plan.is_feasible  # penalty_cost defaults to 0.0


def test_placement_plan_duplicate_service_rejected() -> None:
    with pytest.raises(Exception):
        PlacementPlan(
            assignments=(
                ServiceAssignment(service_name="frontend", node_name="node-1"),
                ServiceAssignment(service_name="frontend", node_name="node-2"),
            ),
            total_cost=0.0,
        )


def test_placement_plan_missing_service_raises() -> None:
    plan = PlacementPlan(
        assignments=(ServiceAssignment(service_name="frontend", node_name="node-1"),),
        total_cost=0.0,
    )
    with pytest.raises(KeyError):
        plan.node_for("backend")
