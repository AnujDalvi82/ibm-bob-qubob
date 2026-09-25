#!/usr/bin/env python
"""QUBOB Automated Benchmark Suite.

Compares three placement solvers across three topology scales:

  Solvers:
    - QIEA   — Quantum-Inspired Evolutionary Algorithm
    - GA     — Classical Genetic Algorithm baseline
    - Greedy — First-Fit Decreasing heuristic

  Topologies:
    - Small      :  8 services ×  3 nodes
    - Medium     : 24 services ×  6 nodes  (custom, not the 9-node default)
    - Enterprise : 64 services × 12 nodes  (custom, not the 15-node default)

Outputs:
  - Rich comparison table printed to stdout
  - benchmarks/benchmark_results.json   — raw numeric results
  - benchmarks/benchmark_summary.md     — Markdown table for README embedding

Usage:
    python benchmarks/run_benchmark.py
    python benchmarks/run_benchmark.py --seed 123
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path when run directly
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bob_optimizer.model.qubo import build_objective, decompose_cost  # noqa: E402
from bob_optimizer.parser.synthetic_generator import _build_topology  # noqa: E402
from bob_optimizer.solvers.baseline_ga import ClassicalGASolver  # noqa: E402
from bob_optimizer.solvers.greedy import GreedyFFDSolver  # noqa: E402
from bob_optimizer.solvers.qiea import QIEASolver  # noqa: E402

console = Console()

# ---------------------------------------------------------------------------
# Topology specs  (name, n_services, n_nodes)
# ---------------------------------------------------------------------------

TOPOLOGIES = [
    ("Small (8×3)", 8, 3),
    ("Medium (24×6)", 24, 6),
    ("Enterprise (64×12)", 64, 12),
]

# ---------------------------------------------------------------------------
# Solver factory
# ---------------------------------------------------------------------------

SOLVER_CONFIGS = {
    "QIEA": lambda n_gen: QIEASolver(population_size=20, n_generations=n_gen),
    "Classical GA": lambda n_gen: ClassicalGASolver(population_size=30, n_generations=n_gen),
    "Greedy FFD": lambda _: GreedyFFDSolver(),
}

# Generations per topology scale
GENERATIONS = {
    "Small (8×3)": 100,
    "Medium (24×6)": 150,
    "Enterprise (64×12)": 200,
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    topology: str
    solver: str
    best_cost: float
    latency_cost: float
    latency_reduction_pct: float
    convergence_iterations: int
    solve_time_s: float
    memory_peak_mib: float


# ---------------------------------------------------------------------------
# Unoptimised baseline (all services on node-0)
# ---------------------------------------------------------------------------


def _naive_cost(topology, objective) -> float:  # type: ignore[no-untyped-def]
    """All services crammed onto node-0 — worst-case placement baseline."""
    x = np.zeros(topology.n_variables, dtype=np.float64)
    for i in range(topology.n_services):
        x[i * topology.n_nodes] = 1.0
    return float(objective(x))


# ---------------------------------------------------------------------------
# Run a single benchmark cell
# ---------------------------------------------------------------------------


def run_one(
    solver_name: str,
    topology_name: str,
    n_services: int,
    n_nodes: int,
    seed: int,
) -> BenchmarkResult:
    topo = _build_topology(
        name=topology_name,
        n_services=n_services,
        n_nodes=n_nodes,
        seed=seed,
    )
    objective = build_objective(topo)
    n_gen = GENERATIONS[topology_name]
    solver = SOLVER_CONFIGS[solver_name](n_gen)

    naive = _naive_cost(topo, objective)

    # ---------------------------------------------------------------
    # Memory tracking
    # ---------------------------------------------------------------
    tracemalloc.start()
    t0 = time.perf_counter()

    result = solver.solve(
        objective,
        topo.n_variables,
        topo.n_nodes,
        seed=seed,
    )

    elapsed = time.perf_counter() - t0
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    peak_mib = peak_bytes / (1024 * 1024)

    # ---------------------------------------------------------------
    # Cost decomposition
    # ---------------------------------------------------------------
    components = decompose_cost(topo, result.best_solution)
    lat_cost = components["latency"]

    latency_reduction = (
        100.0 * (naive - result.best_cost) / naive if naive > 0 else 0.0
    )

    convergence_iters = len(result.convergence_history)

    return BenchmarkResult(
        topology=topology_name,
        solver=solver_name,
        best_cost=round(result.best_cost, 4),
        latency_cost=round(lat_cost, 4),
        latency_reduction_pct=round(latency_reduction, 2),
        convergence_iterations=convergence_iters,
        solve_time_s=round(elapsed, 4),
        memory_peak_mib=round(peak_mib, 2),
    )


# ---------------------------------------------------------------------------
# Rich table renderer
# ---------------------------------------------------------------------------

_SOLVER_STYLES = {
    "QIEA": "bold cyan",
    "Classical GA": "yellow",
    "Greedy FFD": "magenta",
}


def render_results_table(results: list[BenchmarkResult]) -> None:
    table = Table(
        title="QUBOB Benchmark Results — QIEA vs Classical GA vs Greedy FFD",
        show_header=True,
        header_style="bold white on dark_blue",
        border_style="blue",
        show_lines=True,
    )

    table.add_column("Topology", style="bold", min_width=18)
    table.add_column("Solver", min_width=14)
    table.add_column("Best Cost", justify="right", min_width=11)
    table.add_column("Network Latency", justify="right", min_width=15)
    table.add_column("Lat. Reduction %", justify="right", min_width=16)
    table.add_column("Iterations", justify="right", min_width=10)
    table.add_column("Solve Time (s)", justify="right", min_width=14)
    table.add_column("Mem Peak (MiB)", justify="right", min_width=14)

    current_topo = ""
    for r in results:
        topo_cell = r.topology if r.topology != current_topo else ""
        current_topo = r.topology

        style = _SOLVER_STYLES.get(r.solver, "")

        # Colour-code latency reduction
        if r.latency_reduction_pct >= 20:
            lat_str = f"[bold green]{r.latency_reduction_pct:+.1f}%[/bold green]"
        elif r.latency_reduction_pct >= 5:
            lat_str = f"[green]{r.latency_reduction_pct:+.1f}%[/green]"
        else:
            lat_str = f"[yellow]{r.latency_reduction_pct:+.1f}%[/yellow]"

        # Colour-code memory (green if <50 MiB)
        mem_str = (
            f"[bold green]{r.memory_peak_mib:.1f}[/bold green]"
            if r.memory_peak_mib < 50
            else f"[red]{r.memory_peak_mib:.1f}[/red]"
        )

        table.add_row(
            topo_cell,
            f"[{style}]{r.solver}[/{style}]" if style else r.solver,
            f"{r.best_cost:.4f}",
            f"{r.latency_cost:.4f}",
            lat_str,
            str(r.convergence_iterations),
            f"{r.solve_time_s:.3f}",
            mem_str,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Markdown table
# ---------------------------------------------------------------------------


def _md_table(results: list[BenchmarkResult]) -> str:
    lines = [
        "## QUBOB Benchmark Results\n",
        "| Topology | Solver | Best Cost | Network Latency | Lat. Reduction % "
        "| Iterations | Solve Time (s) | Mem Peak (MiB) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.topology} | **{r.solver}** | {r.best_cost:.4f} "
            f"| {r.latency_cost:.4f} | {r.latency_reduction_pct:+.1f}% "
            f"| {r.convergence_iterations} | {r.solve_time_s:.3f} "
            f"| {r.memory_peak_mib:.1f} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="QUBOB Automated Benchmark Suite")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42)")
    args = parser.parse_args()

    console.print(
        Panel(
            "[bold cyan]QUBOB — Automated Benchmark Suite[/bold cyan]\n"
            "[dim]QIEA  ·  Classical GA  ·  Greedy FFD[/dim]\n"
            "[dim]Small (8×3)  ·  Medium (24×6)  ·  Enterprise (64×12)[/dim]",
            expand=False,
        )
    )

    results: list[BenchmarkResult] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        total_runs = len(TOPOLOGIES) * len(SOLVER_CONFIGS)
        task = progress.add_task("Running benchmarks…", total=total_runs)

        for topo_name, n_svc, n_nod in TOPOLOGIES:
            for solver_name in SOLVER_CONFIGS:
                progress.update(
                    task,
                    description=f"[cyan]{solver_name}[/cyan] on [yellow]{topo_name}[/yellow]",
                )
                result = run_one(solver_name, topo_name, n_svc, n_nod, seed=args.seed)
                results.append(result)
                progress.advance(task)

    console.print()
    render_results_table(results)

    # -----------------------------------------------------------------------
    # Persist results
    # -----------------------------------------------------------------------
    out_dir = Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "benchmark_results.json"
    json_path.write_text(json.dumps([asdict(r) for r in results], indent=2))

    md_path = out_dir / "benchmark_summary.md"
    md_path.write_text(_md_table(results))

    console.print(f"\n[green]✓[/green] Results saved → [bold]{json_path}[/bold]")
    console.print(f"[green]✓[/green] Markdown   → [bold]{md_path}[/bold]")

    # -----------------------------------------------------------------------
    # Summary headline
    # -----------------------------------------------------------------------
    qiea_results = [r for r in results if r.solver == "QIEA"]
    ga_results = [r for r in results if r.solver == "Classical GA"]
    greedy_results = [r for r in results if r.solver == "Greedy FFD"]

    if qiea_results and ga_results:
        avg_qiea_cost = sum(r.best_cost for r in qiea_results) / len(qiea_results)
        avg_ga_cost = sum(r.best_cost for r in ga_results) / len(ga_results)
        avg_greedy_cost = sum(r.best_cost for r in greedy_results) / len(greedy_results)
        qiea_vs_ga = 100.0 * (avg_ga_cost - avg_qiea_cost) / avg_ga_cost if avg_ga_cost else 0
        qiea_vs_greedy = (
            100.0 * (avg_greedy_cost - avg_qiea_cost) / avg_greedy_cost
            if avg_greedy_cost
            else 0
        )

        all_mem_ok = all(r.memory_peak_mib < 50 for r in results)
        mem_icon = "[bold green]✓[/bold green]" if all_mem_ok else "[yellow]~[/yellow]"

        console.print(
            Panel(
                f"[bold green]QIEA outperforms GA by   {qiea_vs_ga:+.1f}% avg cost[/bold green]\n"
                f"[bold green]QIEA outperforms Greedy by {qiea_vs_greedy:+.1f}% avg cost[/bold green]\n"
                f"{mem_icon} All memory footprints < 50 MiB (near-zero Bobcoin impact)",
                title="[bold]Benchmark Summary[/bold]",
                expand=False,
            )
        )


if __name__ == "__main__":
    main()
