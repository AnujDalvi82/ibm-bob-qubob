"""First-Fit Decreasing (FFD) greedy baseline solver.

Algorithm
---------
1. Sort services in *decreasing* order of resource demand (CPU × memory).
2. For each service in that order, iterate over nodes in increasing order of
   current load and assign the service to the first node that has enough
   remaining capacity (first-fit).
3. If no node fits, assign to the least-loaded node (best-effort fallback).

The FFD heuristic is a classical bin-packing approximation.  It runs in
O(N · S) time and produces a deterministic, reproducible placement that
serves as a fast lower bound for benchmarking.

The *objective* callable is evaluated on the final placement to produce a
comparable cost value, and a trivial single-step convergence history is
recorded so the benchmark harness can treat all solvers uniformly.
"""

from __future__ import annotations

import time

import numpy as np

from bob_optimizer.solvers.base import BaseSolver, ObjectiveFn, SolverResult


class GreedyFFDSolver(BaseSolver):
    """First-Fit Decreasing greedy placement baseline.

    Parameters
    ----------
    resource_demands:
        Optional 1-D array of shape ``(n_services,)`` with per-service
        resource weights.  When *None* all services are treated as equal
        and the sort order is arbitrary (degenerates to First-Fit).
    node_capacities:
        Optional 1-D array of shape ``(n_nodes,)`` with per-node capacity
        budgets.  When *None* capacities are treated as infinite (no
        resource constraint is modelled).
    """

    NAME = "greedy_ffd"

    def __init__(
        self,
        resource_demands: np.ndarray | None = None,
        node_capacities: np.ndarray | None = None,
    ) -> None:
        self.resource_demands = resource_demands
        self.node_capacities = node_capacities

    def solve(
        self,
        objective: ObjectiveFn,
        n_variables: int,
        n_nodes: int,
        *,
        seed: int | None = None,  # unused — greedy is deterministic
    ) -> SolverResult:
        t_start = time.perf_counter()

        n_services = n_variables // n_nodes

        # ----------------------------------------------------------------
        # Resource demands and node capacities
        # ----------------------------------------------------------------
        if self.resource_demands is not None and len(self.resource_demands) == n_services:
            demands = np.asarray(self.resource_demands, dtype=float)
        else:
            demands = np.ones(n_services, dtype=float)

        if self.node_capacities is not None and len(self.node_capacities) == n_nodes:
            caps = np.asarray(self.node_capacities, dtype=float)
        else:
            caps = np.full(n_nodes, float("inf"))

        # ----------------------------------------------------------------
        # Sort services: decreasing demand (First-Fit Decreasing)
        # ----------------------------------------------------------------
        service_order = np.argsort(demands)[::-1]  # largest demand first

        # ----------------------------------------------------------------
        # Greedy assignment
        # ----------------------------------------------------------------
        remaining = caps.copy()
        assignment = np.full(n_services, -1, dtype=int)  # service → node index

        for svc_idx in service_order:
            demand = demands[svc_idx]
            placed = False

            # First-fit: pick the first node with sufficient remaining capacity
            for node_idx in range(n_nodes):
                if remaining[node_idx] >= demand:
                    assignment[svc_idx] = node_idx
                    remaining[node_idx] -= demand
                    placed = True
                    break

            if not placed:
                # Best-effort fallback: assign to least-loaded node
                node_idx = int(np.argmax(remaining))
                assignment[svc_idx] = node_idx
                remaining[node_idx] = max(0.0, remaining[node_idx] - demand)

        # ----------------------------------------------------------------
        # Encode assignment as flat binary vector x_{i,j}
        # ----------------------------------------------------------------
        x = np.zeros(n_variables, dtype=np.int8)
        for svc_idx, node_idx in enumerate(assignment):
            if node_idx >= 0:
                x[svc_idx * n_nodes + node_idx] = 1

        cost = objective(x)
        solve_time = time.perf_counter() - t_start

        return SolverResult(
            best_solution=x,
            best_cost=cost,
            convergence_history=[(0, cost)],
            solve_time_s=solve_time,
            metadata={
                "solver": self.NAME,
                "assignment": assignment.tolist(),
            },
        )
