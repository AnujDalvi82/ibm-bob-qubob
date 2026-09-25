"""Multi-objective QUBO cost function factory.

Provides ``build_objective`` which accepts a ``ClusterTopology`` and returns a
pure ``ObjectiveFn`` (``Callable[[np.ndarray], float]``) compatible with all
``BaseSolver`` implementations.

Mathematical formulation (PROJECT_SPEC.md §5.2)
-----------------------------------------------

    H_total = λ_lat · H_latency
            + λ_res · H_resource
            + λ_aff · H_affinity
            + λ_pen · H_penalty

    H_latency  = Σ_{(i,k)∈E} w_ik · Σ_j Σ_{j'≠j} x_{i,j} · x_{k,j'} · d(j,j')
    H_resource = Σ_j  ReLU( Σ_i x_{i,j}·r_i − C_j )²
    H_affinity = Σ_{(i,k) anti-affinity} Σ_j x_{i,j} · x_{k,j}
    H_penalty  = Σ_i ( Σ_j x_{i,j} − 1 )²

Variable encoding (consistent with ClusterTopology.variable_index):
    x_{i,j}  →  x[i * n_nodes + j]

All inner loops are vectorised using NumPy; the returned closure pre-computes
heavy structures once so repeated calls are O(n_services·n_nodes²).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from bob_optimizer.model.graph import (
    build_capacity_vector,
    build_latency_matrix,
    build_resource_demand_vector,
)
from bob_optimizer.solvers.base import ObjectiveFn

if TYPE_CHECKING:
    from bob_optimizer.model.domain import ClusterTopology


# ---------------------------------------------------------------------------
# Term implementations (operate on 2-D x_mat: shape (n_services, n_nodes))
# ---------------------------------------------------------------------------


def _latency_cost(
    x_mat: np.ndarray,
    dep_weight_mat: np.ndarray,
    latency_mat: np.ndarray,
) -> float:
    """Compute H_latency.

    Parameters
    ----------
    x_mat:
        shape (n_services, n_nodes), float64 view of the binary solution.
    dep_weight_mat:
        shape (n_services, n_services) — entry [i, k] = calls_per_second
        for edge i→k; 0 elsewhere.
    latency_mat:
        shape (n_nodes, n_nodes) — pairwise RTT in ms.

    Returns
    -------
    H_latency as a Python float.
    """
    # node_assignment[i, j] = x_{i,j}
    # expected_latency_contrib for edge (i,k):
    #   w_ik * sum_{j,j'} x_{i,j} * x_{k,j'} * d(j,j')
    #
    # Vectorised: for each service i, compute the "weighted node distribution"
    # p_i = x_mat[i]  (shape n_nodes)
    # For each edge (i,k) with weight w_ik:
    #   cost += w_ik * p_i @ latency_mat @ p_k
    #
    # Aggregate: W = dep_weight_mat (n_services x n_services)
    # node_lat = x_mat @ latency_mat @ x_mat.T  shape (n_services, n_services)
    # total = sum(W * node_lat)
    node_lat = x_mat @ latency_mat @ x_mat.T  # (n_services, n_services)
    return float(np.sum(dep_weight_mat * node_lat))


def _resource_cost(
    x_mat: np.ndarray,
    demands: np.ndarray,
    capacities: np.ndarray,
) -> float:
    """Compute H_resource = Σ_j ReLU(load_j - capacity_j)².

    Parameters
    ----------
    x_mat:
        shape (n_services, n_nodes), float64.
    demands:
        shape (n_services,) — CPU millicores per service.
    capacities:
        shape (n_nodes,) — allocatable CPU millicores per node.

    Returns
    -------
    H_resource as a Python float.
    """
    # node_load[j] = Σ_i x_{i,j} * r_i
    node_load = x_mat.T @ demands          # shape (n_nodes,)
    overflow = np.maximum(node_load - capacities, 0.0)
    return float(np.sum(overflow ** 2))


def _affinity_cost(
    x_mat: np.ndarray,
    anti_pairs: list[tuple[int, int]],
) -> float:
    """Compute H_affinity = Σ_{anti-affinity (i,k)} Σ_j x_{i,j}·x_{k,j}.

    Parameters
    ----------
    x_mat:
        shape (n_services, n_nodes), float64.
    anti_pairs:
        Pre-computed list of (service_i_idx, service_k_idx) integer tuples
        for all anti-affinity constraints.

    Returns
    -------
    H_affinity as a Python float.
    """
    total = 0.0
    for i, k in anti_pairs:
        total += float(np.dot(x_mat[i], x_mat[k]))
    return total


def _penalty_cost(x_mat: np.ndarray) -> float:
    """Compute H_penalty = Σ_i (Σ_j x_{i,j} - 1)².

    Parameters
    ----------
    x_mat:
        shape (n_services, n_nodes), float64.

    Returns
    -------
    H_penalty as a Python float.
    """
    row_sums = x_mat.sum(axis=1)           # shape (n_services,)
    return float(np.sum((row_sums - 1.0) ** 2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_objective(topology: "ClusterTopology") -> ObjectiveFn:
    """Build a vectorised QUBO objective function for *topology*.

    Pre-computes all topology-derived matrices once.  The returned closure
    captures these structures and has zero Python-level loops over ``n_variables``.

    Parameters
    ----------
    topology:
        Fully populated ``ClusterTopology`` (services + nodes + dependencies).

    Returns
    -------
    Pure ``ObjectiveFn`` conforming to ``Callable[[np.ndarray], float]``.
    Lower return value = better placement.
    """
    n_services = topology.n_services
    n_nodes = topology.n_nodes

    # Pre-compute matrices
    latency_mat = build_latency_matrix(topology)                    # (n_nodes, n_nodes)
    demands = build_resource_demand_vector(topology)                 # (n_services,)
    capacities = build_capacity_vector(topology)                     # (n_nodes,)

    # Dependency weight matrix W[i, k] = calls_per_second for edge i→k
    dep_weight_mat = np.zeros((n_services, n_services), dtype=np.float64)
    svc_idx = {svc.name: i for i, svc in enumerate(topology.services)}
    for dep in topology.dependencies:
        i = svc_idx[dep.source]
        k = svc_idx[dep.target]
        dep_weight_mat[i, k] = dep.calls_per_second

    # Anti-affinity pairs: list of (i, k) index pairs
    anti_pairs: list[tuple[int, int]] = []
    for svc in topology.services:
        i = svc_idx[svc.name]
        for anti_name in svc.anti_affinity_services:
            if anti_name in svc_idx:
                k = svc_idx[anti_name]
                pair = (min(i, k), max(i, k))
                if pair not in anti_pairs:
                    anti_pairs.append(pair)

    # λ weights
    lam_lat = topology.lambda_latency
    lam_res = topology.lambda_resource
    lam_aff = topology.lambda_affinity
    lam_pen = topology.lambda_penalty

    def _objective(x: np.ndarray) -> float:
        """QUBO objective: H_total = λ_lat·H_lat + λ_res·H_res + λ_aff·H_aff + λ_pen·H_pen."""
        x_mat = x.reshape(n_services, n_nodes).astype(np.float64)
        h_lat = _latency_cost(x_mat, dep_weight_mat, latency_mat)
        h_res = _resource_cost(x_mat, demands, capacities)
        h_aff = _affinity_cost(x_mat, anti_pairs)
        h_pen = _penalty_cost(x_mat)
        return lam_lat * h_lat + lam_res * h_res + lam_aff * h_aff + lam_pen * h_pen

    _objective.__name__ = "qubo_objective"
    return _objective


def decompose_cost(
    topology: "ClusterTopology",
    x: np.ndarray,
) -> dict[str, float]:
    """Return the individual cost components for a placement vector *x*.

    Parameters
    ----------
    topology:
        The cluster topology used to build the cost model.
    x:
        Binary placement vector, shape ``(n_variables,)``.

    Returns
    -------
    Dict with keys ``"latency"``, ``"resource"``, ``"affinity"``, ``"penalty"``,
    and ``"total"`` (unweighted component values, not λ-scaled except total).
    """
    n_services = topology.n_services
    n_nodes = topology.n_nodes

    latency_mat = build_latency_matrix(topology)
    demands = build_resource_demand_vector(topology)
    capacities = build_capacity_vector(topology)

    dep_weight_mat = np.zeros((n_services, n_services), dtype=np.float64)
    svc_idx = {svc.name: i for i, svc in enumerate(topology.services)}
    for dep in topology.dependencies:
        dep_weight_mat[svc_idx[dep.source], svc_idx[dep.target]] = dep.calls_per_second

    anti_pairs: list[tuple[int, int]] = []
    for svc in topology.services:
        i = svc_idx[svc.name]
        for anti_name in svc.anti_affinity_services:
            if anti_name in svc_idx:
                k = svc_idx[anti_name]
                pair = (min(i, k), max(i, k))
                if pair not in anti_pairs:
                    anti_pairs.append(pair)

    x_mat = x.reshape(n_services, n_nodes).astype(np.float64)
    h_lat = _latency_cost(x_mat, dep_weight_mat, latency_mat)
    h_res = _resource_cost(x_mat, demands, capacities)
    h_aff = _affinity_cost(x_mat, anti_pairs)
    h_pen = _penalty_cost(x_mat)

    total = (
        topology.lambda_latency * h_lat
        + topology.lambda_resource * h_res
        + topology.lambda_affinity * h_aff
        + topology.lambda_penalty * h_pen
    )
    return {
        "latency": h_lat,
        "resource": h_res,
        "affinity": h_aff,
        "penalty": h_pen,
        "total": total,
    }
