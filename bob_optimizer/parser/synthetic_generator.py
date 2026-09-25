"""Synthetic topology generator for benchmarking and testing.

Produces deterministic ``ClusterTopology`` instances of three sizes:

- **Small**      —  8 services,  3 nodes (single benchmark run)
- **Medium**     — 24 services,  9 nodes
- **Enterprise** — 64 services, 15 nodes

All nodes are distributed across three availability zones
(``us-east-1a``, ``us-east-1b``, ``us-east-1c``) and wired with the canonical
three-tier RTT latency model from ``bob_optimizer.model.graph``:

    - same node       : RTT_LOOPBACK_MS  = 0.1 ms
    - same zone       : RTT_INTRAZONE_MS = 1.5 ms
    - different zones : RTT_CROSSZONE_MS = 8.0 ms

Usage
-----
>>> from bob_optimizer.parser.synthetic_generator import make_small_topology
>>> topo = make_small_topology(seed=42)
>>> topo.n_services, topo.n_nodes
(8, 3)
"""

from __future__ import annotations

import numpy as np

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
)

# ---------------------------------------------------------------------------
# Zone layout
# ---------------------------------------------------------------------------

_ZONES: tuple[str, ...] = ("us-east-1a", "us-east-1b", "us-east-1c")
_REGION: str = "us-east-1"

# Node resource ranges (millicores / MiB)
_NODE_CPU_M: int = 16_000   # 16 vCPU per node
_NODE_MEM_MIB: int = 65_536  # 64 GiB per node

# Service resource ranges
_SVC_CPU_MIN: int = 50
_SVC_CPU_MAX: int = 2000
_SVC_MEM_MIN: int = 64
_SVC_MEM_MAX: int = 4096

# Average out-degree for the dependency graph
_AVG_DEGREE: int = 2

# RPS range for synthetic edges (log-uniform)
_RPS_LOG_MIN: float = -1.0   # 10^-1 = 0.1 rps
_RPS_LOG_MAX: float = 3.0    # 10^3  = 1000 rps


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_nodes(n_nodes: int, rng: np.random.Generator) -> list[NodeCapacity]:
    """Create *n_nodes* nodes distributed evenly across three zones.

    Populates the full ``latency_ms`` dict for each node using the three-tier
    RTT constants so the QUBO latency term has real data to work with.
    """
    # Assign zones in round-robin order
    zone_assignments: list[str] = [_ZONES[i % len(_ZONES)] for i in range(n_nodes)]
    node_names: list[str] = [f"node-{i}" for i in range(n_nodes)]

    nodes: list[NodeCapacity] = []
    for i, name in enumerate(node_names):
        zone_i = zone_assignments[i]
        latency_ms: dict[str, float] = {}
        for j, peer_name in enumerate(node_names):
            if i == j:
                latency_ms[peer_name] = RTT_LOOPBACK_MS
            elif zone_assignments[j] == zone_i:
                latency_ms[peer_name] = RTT_INTRAZONE_MS
            else:
                latency_ms[peer_name] = RTT_CROSSZONE_MS
        nodes.append(
            NodeCapacity(
                name=name,
                zone=zone_i,
                region=_REGION,
                allocatable_cpu_millicores=_NODE_CPU_M,
                allocatable_memory_mib=_NODE_MEM_MIB,
                latency_ms=latency_ms,
            )
        )
    return nodes


def _make_services(n_services: int, rng: np.random.Generator) -> list[Service]:
    """Create *n_services* services with randomised (but seeded) resources."""
    services: list[Service] = []
    for i in range(n_services):
        cpu_m = int(rng.integers(_SVC_CPU_MIN, _SVC_CPU_MAX + 1))
        mem_mib = int(rng.integers(_SVC_MEM_MIN, _SVC_MEM_MAX + 1))
        services.append(
            Service(
                name=f"svc-{i}",
                image=f"registry.example.com/svc-{i}:latest",
                replicas=1,
                resources=ResourceRequest(cpu_millicores=cpu_m, memory_mib=mem_mib),
            )
        )
    return services


def _make_dependencies(
    services: list[Service],
    rng: np.random.Generator,
    avg_degree: int = _AVG_DEGREE,
) -> list[ServiceDependency]:
    """Build a sparse random directed dependency graph with no self-loops.

    Each service is given ``avg_degree`` randomly chosen targets (without
    replacement when possible).  Duplicate edges are deduplicated.
    """
    n = len(services)
    names = [svc.name for svc in services]
    seen: set[tuple[str, str]] = set()
    deps: list[ServiceDependency] = []

    for i, source_name in enumerate(names):
        candidates = [j for j in range(n) if j != i]
        if not candidates:
            continue
        # Draw up to avg_degree targets without replacement
        k = min(avg_degree, len(candidates))
        targets_idx = rng.choice(candidates, size=k, replace=False)
        for j in targets_idx:
            target_name = names[int(j)]
            if (source_name, target_name) not in seen:
                # Log-uniform RPS
                rps = float(10.0 ** rng.uniform(_RPS_LOG_MIN, _RPS_LOG_MAX))
                deps.append(
                    ServiceDependency(
                        source=source_name,
                        target=target_name,
                        calls_per_second=round(rps, 3),
                    )
                )
                seen.add((source_name, target_name))

    return deps


def _build_topology(
    name: str,
    n_services: int,
    n_nodes: int,
    seed: int,
) -> ClusterTopology:
    """Internal factory: build a topology of the requested size."""
    rng = np.random.default_rng(seed)
    nodes = _make_nodes(n_nodes, rng)
    services = _make_services(n_services, rng)
    deps = _make_dependencies(services, rng)
    return ClusterTopology(
        name=name,
        services=tuple(services),
        nodes=tuple(nodes),
        dependencies=tuple(deps),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def make_small_topology(seed: int = 0) -> ClusterTopology:
    """Return a Small topology: 8 services × 3 nodes across 3 zones.

    Parameters
    ----------
    seed:
        RNG seed for deterministic generation. Same seed always produces the
        same topology.

    Returns
    -------
    Fully populated ``ClusterTopology`` with latency matrix data.
    """
    return _build_topology("small-cluster", n_services=8, n_nodes=3, seed=seed)


def make_medium_topology(seed: int = 0) -> ClusterTopology:
    """Return a Medium topology: 24 services × 9 nodes across 3 zones.

    Parameters
    ----------
    seed:
        RNG seed for deterministic generation.

    Returns
    -------
    Fully populated ``ClusterTopology``.
    """
    return _build_topology("medium-cluster", n_services=24, n_nodes=9, seed=seed)


def make_enterprise_topology(seed: int = 0) -> ClusterTopology:
    """Return an Enterprise topology: 64 services × 15 nodes across 3 zones.

    Parameters
    ----------
    seed:
        RNG seed for deterministic generation.

    Returns
    -------
    Fully populated ``ClusterTopology``.
    """
    return _build_topology("enterprise-cluster", n_services=64, n_nodes=15, seed=seed)
