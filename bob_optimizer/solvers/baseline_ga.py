"""Classical Genetic Algorithm baseline solver.

Implements a standard generational GA with:
- Binary-encoded chromosomes (same flat layout as QIEA: x_{i,j})
- Tournament selection
- Two-point crossover
- Bit-flip mutation
- One-hot repair to maintain feasibility

This serves as a quality benchmark against the quantum-inspired approaches.
"""

from __future__ import annotations

import time

import numpy as np

from bob_optimizer.solvers.base import BaseSolver, ObjectiveFn, SolverResult
from bob_optimizer.solvers.qiea import _repair_one_hot


def _tournament_select(
    population: np.ndarray,
    fitnesses: np.ndarray,
    k: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Return the best individual from a random tournament of size *k*."""
    indices = rng.choice(len(population), size=k, replace=False)
    winner = indices[np.argmin(fitnesses[indices])]
    return population[winner].copy()


def _two_point_crossover(
    parent_a: np.ndarray,
    parent_b: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """Two-point crossover on binary arrays.

    Two cut points are drawn uniformly at random. The segment between them is
    swapped between the two parents to produce two offspring.

    Parameters
    ----------
    parent_a, parent_b:
        Binary 1-D arrays of equal length.
    rng:
        NumPy random generator.

    Returns
    -------
    Tuple of two offspring arrays.
    """
    n = len(parent_a)
    pts = sorted(rng.choice(n + 1, size=2, replace=False))
    c1, c2 = parent_a.copy(), parent_b.copy()
    c1[pts[0] : pts[1]] = parent_b[pts[0] : pts[1]]
    c2[pts[0] : pts[1]] = parent_a[pts[0] : pts[1]]
    return c1, c2


def _bit_flip_mutation(
    individual: np.ndarray,
    mutation_rate: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Flip each bit independently with probability *mutation_rate*."""
    mask = rng.random(len(individual)) < mutation_rate
    individual[mask] ^= 1
    return individual


class ClassicalGASolver(BaseSolver):
    """Standard Genetic Algorithm for combinatorial placement optimisation.

    Parameters
    ----------
    population_size:
        Number of chromosomes.
    n_generations:
        Number of generational iterations.
    crossover_prob:
        Probability that two selected parents undergo crossover.
    mutation_rate:
        Per-bit flip probability during mutation.
    tournament_k:
        Tournament size for selection.
    elitism:
        Number of elite individuals carried unchanged to the next generation.
    """

    NAME = "classical_ga"

    def __init__(
        self,
        population_size: int = 30,
        n_generations: int = 100,
        crossover_prob: float = 0.8,
        mutation_rate: float | None = None,  # default: 1/n_variables
        tournament_k: int = 3,
        elitism: int = 2,
    ) -> None:
        self.population_size = population_size
        self.n_generations = n_generations
        self.crossover_prob = crossover_prob
        self._mutation_rate = mutation_rate
        self.tournament_k = tournament_k
        self.elitism = elitism

    def solve(
        self,
        objective: ObjectiveFn,
        n_variables: int,
        n_nodes: int,
        *,
        seed: int | None = None,
    ) -> SolverResult:
        rng = np.random.default_rng(seed)
        t_start = time.perf_counter()

        mutation_rate = self._mutation_rate if self._mutation_rate is not None else 1.0 / n_variables

        # ----------------------------------------------------------------
        # Initialise feasible population
        # ----------------------------------------------------------------
        n_services = n_variables // n_nodes
        population = np.zeros((self.population_size, n_variables), dtype=np.int8)
        for k in range(self.population_size):
            for i in range(n_services):
                j = rng.integers(n_nodes)
                population[k, i * n_nodes + j] = 1

        best_solution = population[0].copy()
        best_cost = float("inf")
        convergence: list[tuple[int, float]] = []

        for gen in range(self.n_generations):
            # Evaluate
            fitnesses = np.array([objective(ind) for ind in population])

            # Track global best
            gen_best_idx = int(np.argmin(fitnesses))
            if fitnesses[gen_best_idx] < best_cost:
                best_cost = float(fitnesses[gen_best_idx])
                best_solution = population[gen_best_idx].copy()

            convergence.append((gen, best_cost))

            # ----------------------------------------------------------------
            # Build next generation
            # ----------------------------------------------------------------
            # Elitism: carry top-k individuals unchanged
            elite_indices = np.argsort(fitnesses)[: self.elitism]
            next_gen = [population[i].copy() for i in elite_indices]

            while len(next_gen) < self.population_size:
                p1 = _tournament_select(population, fitnesses, self.tournament_k, rng)
                p2 = _tournament_select(population, fitnesses, self.tournament_k, rng)

                if rng.random() < self.crossover_prob:
                    c1, c2 = _two_point_crossover(p1, p2, rng)
                else:
                    c1, c2 = p1.copy(), p2.copy()

                for child in (c1, c2):
                    _bit_flip_mutation(child, mutation_rate, rng)
                    _repair_one_hot(child, n_nodes, rng)
                    next_gen.append(child)

            population = np.array(next_gen[: self.population_size], dtype=np.int8)

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
                "mutation_rate": mutation_rate,
            },
        )
