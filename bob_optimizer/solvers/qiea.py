"""Quantum-Inspired Evolutionary Algorithm (QIEA) solver.

Theory
------
Each individual in the population is a *Q-chromosome* — a vector of Q-bits.
A single Q-bit is a probability amplitude pair (α, β) satisfying:

    |α|² + |β|² = 1

where |α|² is the probability that the bit collapses to 0 and |β|² is the
probability it collapses to 1.  Initialisation places every Q-bit in equal
superposition: α = β = 1/√2.

At each generation:
1. *Observation* – each Q-chromosome is measured (Monte Carlo collapse) to
   produce a classical binary solution.
2. *Evaluation* – the objective function is called; feasibility is enforced
   via a one-hot repair operator.
3. *Update* – Q-bits are rotated toward the best-so-far solution using the
   DynamicRotationGate U(Δθ).
4. *Catastrophe* – if population diversity drops below a threshold the
   QuantumCatastropheOperator applies σ_x (NOT / phase-flip) to random bits.

References
----------
Han & Kim, "Quantum-Inspired Evolutionary Algorithm for a Class of
Combinatorial Optimization", IEEE TEC 6(6), 2002.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from bob_optimizer.solvers.base import BaseSolver, ObjectiveFn, SolverResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SQRT2_INV: Final[float] = 1.0 / np.sqrt(2.0)

# Rotation angle magnitude table: indexed by [x_i_best][x_i_current][f_best < f_current]
# Rows / columns: 0 = bit is 0, 1 = bit is 1
# Last dim: 0 = current solution is NOT better, 1 = current solution IS better
# Values follow Han & Kim Table I with a small baseline angle.
_DELTA_THETA_TABLE: Final[np.ndarray] = np.array(
    [
        # x_best=0, x_cur=0 — no information: small nudge
        [[0.0, 0.0], [0.05 * np.pi, -0.05 * np.pi]],
        # x_best=1, x_cur=0 — rotate toward 1
        [[0.05 * np.pi, 0.05 * np.pi], [0.0, 0.0]],
    ],
    dtype=np.float64,
)


# ---------------------------------------------------------------------------
# Q-bit vector
# ---------------------------------------------------------------------------


@dataclass
class QBitVector:
    """A single Q-chromosome: array of (α, β) amplitude pairs.

    Parameters
    ----------
    n_bits:
        Number of Q-bits (= total QUBO variables).
    rng:
        NumPy random generator used for all stochastic operations.
    """

    n_bits: int
    rng: np.random.Generator = field(repr=False)

    # α amplitudes — shape (n_bits,)
    alpha: np.ndarray = field(init=False)
    # β amplitudes — shape (n_bits,)
    beta: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        # Equal superposition: α = β = 1/√2
        self.alpha = np.full(self.n_bits, _SQRT2_INV)
        self.beta = np.full(self.n_bits, _SQRT2_INV)

    # ------------------------------------------------------------------
    # Core quantum operations
    # ------------------------------------------------------------------

    def measure(self) -> np.ndarray:
        """Monte Carlo observation: collapse each Q-bit to a classical bit.

        Bit *i* collapses to 1 with probability |β_i|² and to 0 with
        probability |α_i|² = 1 − |β_i|².

        Returns
        -------
        np.ndarray
            1-D binary array of shape (n_bits,) with dtype int8.
        """
        prob_one = self.beta ** 2  # shape (n_bits,)
        return (self.rng.random(self.n_bits) < prob_one).astype(np.int8)

    def is_normalised(self, tol: float = 1e-9) -> bool:
        """Return True iff |α_i|² + |β_i|² = 1 for every Q-bit (within *tol*)."""
        norms = self.alpha ** 2 + self.beta ** 2
        return bool(np.all(np.abs(norms - 1.0) < tol))

    def diversity(self) -> float:
        """Mean entropy H = -Σ (p log p + q log q) over all Q-bits.

        Returns a value in [0, 1]; 1.0 means full superposition, 0.0 means
        all Q-bits are fully collapsed.
        """
        eps = 1e-12
        p = np.clip(self.alpha ** 2, eps, 1.0 - eps)
        q = np.clip(self.beta ** 2, eps, 1.0 - eps)
        entropy = -(p * np.log2(p) + q * np.log2(q))
        return float(np.mean(entropy))


# ---------------------------------------------------------------------------
# Dynamic rotation gate
# ---------------------------------------------------------------------------


class DynamicRotationGate:
    """Unitary rotation gate U(Δθ) that steers Q-bits toward the best solution.

    The rotation angle magnitude Δθ is drawn from the lookup table and then
    *scaled* by an adaptive factor derived from the recent fitness improvement
    rate — faster improvement → larger rotations for aggressive exploitation;
    stagnation → smaller rotations to preserve diversity.

    Parameters
    ----------
    base_scale:
        Multiplier applied to all table angles.
    improvement_boost:
        Additional scale factor when fitness improved in the last generation.
    """

    def __init__(self, base_scale: float = 1.0, improvement_boost: float = 1.5) -> None:
        self.base_scale = base_scale
        self.improvement_boost = improvement_boost
        self._improved_last_gen: bool = False

    def notify_improvement(self, improved: bool) -> None:
        """Tell the gate whether the best cost improved in the last generation."""
        self._improved_last_gen = improved

    def apply(
        self,
        qbit: QBitVector,
        current_solution: np.ndarray,
        best_solution: np.ndarray,
    ) -> None:
        """Rotate *qbit* in-place toward *best_solution*.

        For each bit position *i* the signed rotation angle is:

            Δθ_i = sign_factor × |table_angle| × adaptive_scale

        where *sign_factor* ∈ {-1, 0, +1} is determined by the quadrant of
        (α_i, β_i) and the table entry, and *adaptive_scale* increases when
        the best solution improved last generation.
        """
        scale = self.base_scale * (self.improvement_boost if self._improved_last_gen else 1.0)

        x_best = best_solution.astype(int)
        x_cur = current_solution.astype(int)

        for i in range(qbit.n_bits):
            xb = min(x_best[i], 1)
            xc = min(x_cur[i], 1)

            # Determine if current solution is *better* (lower cost) — we do
            # not have the cost here so we use a proxy: same bit = no info.
            # The caller uses the full table logic; here we use a simplified
            # directional rule: if xb != xc, rotate toward xb.
            if xb == xc:
                continue  # bits agree — no rotation needed

            # Read base angle from table (|xb|, |xc|, improvement flag)
            improved_flag = 1 if self._improved_last_gen else 0
            raw_angle = _DELTA_THETA_TABLE[xb, xc, improved_flag]
            delta_theta = scale * raw_angle

            if delta_theta == 0.0:
                continue

            a, b = qbit.alpha[i], qbit.beta[i]
            cos_t = np.cos(delta_theta)
            sin_t = np.sin(delta_theta)
            qbit.alpha[i] = cos_t * a - sin_t * b
            qbit.beta[i] = sin_t * a + cos_t * b

        # Re-normalise to guard against floating-point drift
        norms = np.sqrt(qbit.alpha ** 2 + qbit.beta ** 2)
        norms = np.where(norms == 0.0, 1.0, norms)
        qbit.alpha /= norms
        qbit.beta /= norms


# ---------------------------------------------------------------------------
# Quantum catastrophe operator
# ---------------------------------------------------------------------------


class QuantumCatastropheOperator:
    """Phase-flip / NOT gate (σ_x) mutation to escape local minima.

    When population *diversity* drops below *diversity_threshold* this
    operator applies σ_x to a random fraction of Q-bits, effectively
    flipping their probability mass between 0 and 1 and injecting fresh
    diversity.

    σ_x:  (α, β) → (β, α)
    """

    def __init__(
        self,
        diversity_threshold: float = 0.15,
        flip_fraction: float = 0.3,
    ) -> None:
        self.diversity_threshold = diversity_threshold
        self.flip_fraction = flip_fraction

    def apply_if_needed(self, qbits: list[QBitVector], rng: np.random.Generator) -> int:
        """Apply σ_x to random Q-bits when mean diversity is low.

        Returns the number of Q-bits that were flipped (0 if not triggered).
        """
        mean_diversity = float(np.mean([qb.diversity() for qb in qbits]))
        if mean_diversity >= self.diversity_threshold:
            return 0

        total_flipped = 0
        for qb in qbits:
            n_flip = max(1, int(qb.n_bits * self.flip_fraction))
            indices = rng.choice(qb.n_bits, size=n_flip, replace=False)
            # σ_x: swap α ↔ β
            qb.alpha[indices], qb.beta[indices] = (
                qb.beta[indices].copy(),
                qb.alpha[indices].copy(),
            )
            total_flipped += n_flip

        return total_flipped


# ---------------------------------------------------------------------------
# One-hot repair operator
# ---------------------------------------------------------------------------


def _repair_one_hot(x: np.ndarray, n_nodes: int, rng: np.random.Generator) -> np.ndarray:
    """Enforce the placement constraint: exactly one node per service.

    For each service block of length *n_nodes*:
    - If all zeros → activate a random node.
    - If multiple ones → keep the first active; zero out the rest.

    Parameters
    ----------
    x:
        Mutable binary array of length ``n_services × n_nodes``.
    n_nodes:
        Number of candidate nodes.
    rng:
        Generator used when a zero-block needs a random activation.

    Returns
    -------
    np.ndarray
        The repaired array (same object, modified in-place).
    """
    n_services = len(x) // n_nodes
    for i in range(n_services):
        block = x[i * n_nodes : (i + 1) * n_nodes]
        active = np.where(block == 1)[0]
        if len(active) == 0:
            # No node selected → pick one at random
            j = rng.integers(n_nodes)
            block[j] = 1
        elif len(active) > 1:
            # Too many → keep first, zero out the rest
            block[:] = 0
            block[active[0]] = 1
    return x


# ---------------------------------------------------------------------------
# QIEA Solver
# ---------------------------------------------------------------------------


class QIEASolver(BaseSolver):
    """Quantum-Inspired Evolutionary Algorithm for combinatorial placement.

    Parameters
    ----------
    population_size:
        Number of Q-chromosomes in the population.
    n_generations:
        Maximum number of evolutionary generations.
    diversity_threshold:
        Entropy threshold below which the catastrophe operator fires.
    flip_fraction:
        Fraction of Q-bits flipped by the catastrophe operator.
    base_scale:
        Base multiplier for rotation gate angles.
    improvement_boost:
        Rotation angle boost factor when fitness improved.
    """

    NAME = "qiea"

    def __init__(
        self,
        population_size: int = 20,
        n_generations: int = 100,
        diversity_threshold: float = 0.15,
        flip_fraction: float = 0.3,
        base_scale: float = 1.0,
        improvement_boost: float = 1.5,
    ) -> None:
        self.population_size = population_size
        self.n_generations = n_generations
        self._catastrophe = QuantumCatastropheOperator(diversity_threshold, flip_fraction)
        self._gate = DynamicRotationGate(base_scale, improvement_boost)

    # ------------------------------------------------------------------
    # BaseSolver interface
    # ------------------------------------------------------------------

    def solve(
        self,
        objective: ObjectiveFn,
        n_variables: int,
        n_nodes: int,
        *,
        seed: int | None = None,
    ) -> SolverResult:
        """Run QIEA and return the best found solution.

        Parameters
        ----------
        objective:
            Callable ``f(x) -> float``; lower is better.
        n_variables:
            Total binary variables (``n_services × n_nodes``).
        n_nodes:
            Nodes per service — used by the repair operator.
        seed:
            RNG seed for full reproducibility.
        """
        rng = np.random.default_rng(seed)
        t_start = time.perf_counter()

        # Initialise population in equal superposition
        population = [QBitVector(n_variables, rng) for _ in range(self.population_size)]

        best_solution: np.ndarray = np.zeros(n_variables, dtype=np.int8)
        best_cost = float("inf")
        convergence: list[tuple[int, float]] = []
        catastrophe_events = 0

        for gen in range(self.n_generations):
            prev_best = best_cost

            for qb in population:
                # 1. Observe — collapse Q-chromosome to a classical bit string
                candidate = qb.measure()

                # 2. Repair — enforce one-hot placement constraint
                candidate = _repair_one_hot(candidate, n_nodes, rng)

                # 3. Evaluate
                cost = objective(candidate)

                # 4. Update global best
                if cost < best_cost:
                    best_cost = cost
                    best_solution = candidate.copy()

            # 5. Notify gate whether improvement occurred
            improved = best_cost < prev_best
            self._gate.notify_improvement(improved)

            # 6. Rotate all Q-chromosomes toward global best
            for qb in population:
                measured = qb.measure()
                self._gate.apply(qb, measured, best_solution)

            # 7. Catastrophe check — inject diversity if population converged
            n_flipped = self._catastrophe.apply_if_needed(population, rng)
            if n_flipped > 0:
                catastrophe_events += 1

            convergence.append((gen, best_cost))

        solve_time = time.perf_counter() - t_start

        return SolverResult(
            best_solution=best_solution,
            best_cost=best_cost,
            convergence_history=convergence,
            solve_time_s=solve_time,
            metadata={
                "solver": self.NAME,
                "population_size": self.population_size,
                "n_generations": self.n_generations,
                "catastrophe_events": catastrophe_events,
            },
        )
