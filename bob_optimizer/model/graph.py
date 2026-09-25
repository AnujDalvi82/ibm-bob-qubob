"""NetworkX service dependency graph builder and NumPy topology helpers.

Provides four pure functions that transform a ClusterTopology into the
numerical structures consumed by the QUBO cost engine and solvers.

Functions
---------
build_service_graph    : ClusterTopology → nx.DiGraph
build_latency_matrix   : ClusterTopology → ndarray (n_nodes, n_nodes)
build_resource_demand_vector : ClusterTopology → ndarray (n_services,)
build_capacity_vector  : ClusterTopology → ndarray (n_nodes,)

Three-tier RTT constants (ms) — used by the synthetic generator to populate
NodeCapacity.latency_ms; exposed here so they can be imported from a single
authoritative location.
"""

from __future__ import annotations

import numpy as np
import networkx as nx

from bob_optimizer.model.domain import ClusterTopology

# ---------------------------------------------------------------------------
# RTT tier constants (ms) — single source of truth
# ---------------------------------------------------------------------------

RTT_LOOPBACK_MS: float = 0.1    # same physical / virtual node
RTT_INTRAZONE_MS: float = 1.5   # same availability zone, different node
RTT_CROSSZONE_MS: float = 8.0   # different availability zones

# Type alias for the service dependency graph
ServiceGraph = nx.DiGraph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_service_graph(topology: ClusterTopology) -> ServiceGraph:
    """Build a directed weighted NetworkX graph from *topology*.

    Nodes
    -----
    One node per service, keyed by service name.
    Node attributes: ``replicas``, ``cpu_millicores``, ``memory_mib``.

    Edges
    -----
    One directed edge per ``ServiceDependency``.
    Edge attributes: ``weight`` (calls_per_second), ``protocol``.

    Parameters
    ----------
    topology:
        The cluster topology to model.

    Returns
    -------
    nx.DiGraph with ``n_services`` nodes and ``n_dependencies`` edges.
    """
    g: ServiceGraph = nx.DiGraph()

    for svc in topology.services:
        g.add_node(
            svc.name,
            replicas=svc.replicas,
            cpu_millicores=svc.resources.cpu_millicores,
            memory_mib=svc.resources.memory_mib,
        )

    for dep in topology.dependencies:
        g.add_edge(
            dep.source,
            dep.target,
            weight=dep.calls_per_second,
            protocol=dep.protocol,
        )

    return g


def build_latency_matrix(topology: ClusterTopology) -> np.ndarray:
    """Build an ``(n_nodes, n_nodes)`` float64 pairwise latency matrix.

    Entry ``M[j, j']`` is ``topology.nodes[j].latency_to(topology.nodes[j'])``
    in milliseconds.  The diagonal is always 0.0.

    Falls back to ``NodeCapacity.latency_to()``'s built-in heuristic for any
    unmeasured pair (0.5 ms intra-zone, 5.0 ms cross-zone).

    Parameters
    ----------
    topology:
        The cluster topology whose nodes form the matrix rows/columns.

    Returns
    -------
    Symmetric float64 ndarray of shape ``(n_nodes, n_nodes)``.
    """
    n = topology.n_nodes
    mat = np.zeros((n, n), dtype=np.float64)
    nodes = topology.nodes
    for j in range(n):
        for k in range(n):
            if j != k:
                mat[j, k] = nodes[j].latency_to(nodes[k])
    return mat


def build_resource_demand_vector(topology: ClusterTopology) -> np.ndarray:
    """Build a ``(n_services,)`` float64 vector of per-service CPU demand.

    Each element is ``service.resources.cpu_millicores`` (already scaled by
    replica count by the parsers / synthetic generator).

    Parameters
    ----------
    topology:
        The cluster topology whose services define the demand.

    Returns
    -------
    float64 ndarray of shape ``(n_services,)``.
    """
    return np.array(
        [svc.resources.cpu_millicores for svc in topology.services],
        dtype=np.float64,
    )


def build_capacity_vector(topology: ClusterTopology) -> np.ndarray:
    """Build a ``(n_nodes,)`` float64 vector of per-node allocatable CPU.

    Each element is ``node.allocatable_cpu_millicores``.

    Parameters
    ----------
    topology:
        The cluster topology whose nodes define the capacity.

    Returns
    -------
    float64 ndarray of shape ``(n_nodes,)``.
    """
    return np.array(
        [node.allocatable_cpu_millicores for node in topology.nodes],
        dtype=np.float64,
    )
