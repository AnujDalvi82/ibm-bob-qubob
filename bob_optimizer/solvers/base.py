"""Abstract Solver base protocol shared by all QUBOB solver implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------


@dataclass
class SolverResult:
    """Returned by every solver's ``solve()`` method.

    Parameters
    ----------
    best_solution:
        1-D binary numpy array of length *n_variables*.  For placement
        problems the variable ordering is ``x_{i,j} = i * n_nodes + j``.
    best_cost:
        Objective value at *best_solution* (lower is better).
    convergence_history:
        List of ``(generation, best_cost)`` tuples — one entry per
        generation / iteration.
    solve_time_s:
        Wall-clock seconds spent inside ``solve()``.
    metadata:
        Solver-specific diagnostics (diversity, acceptance rates, …).
    """

    best_solution: np.ndarray
    best_cost: float
    convergence_history: list[tuple[int, float]] = field(default_factory=list)
    solve_time_s: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

ObjectiveFn = Callable[[np.ndarray], float]
"""Signature expected by every solver: f(x: ndarray[int]) -> float."""


class BaseSolver(ABC):
    """Minimal contract every QUBOB solver must satisfy."""

    @abstractmethod
    def solve(
        self,
        objective: ObjectiveFn,
        n_variables: int,
        n_nodes: int,
        *,
        seed: int | None = None,
    ) -> SolverResult:
        """Run the solver and return a :class:`SolverResult`.

        Parameters
        ----------
        objective:
            Callable that accepts a 1-D binary ``np.ndarray`` of length
            *n_variables* and returns a scalar cost (lower is better).
        n_variables:
            Total number of binary decision variables (``n_services × n_nodes``).
        n_nodes:
            Number of candidate nodes — used by repair operators to enforce
            the one-hot constraint (exactly one node per service).
        seed:
            Optional RNG seed for reproducibility.
        """
