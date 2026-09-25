"""QUBOB CLI — bob-opt entrypoint for IBM Bob IDE integration.

Sub-commands
------------
bob-opt analyze <path>
    Ingest manifests (k8s YAML or docker-compose.yml) and display:
    - Service dependency graph summary
    - RTT latency matrix (Rich table)
    - Current latency bottlenecks

bob-opt optimize <path> [--algorithm qiea|ga|greedy] [--generations N]
    Run the selected solver and output:
    - Optimal placement table with node/zone assignments
    - Per-service cost breakdown
    - Latency reduction % vs a naive random placement baseline

bob-opt diff <path>
    Generate and preview syntax-highlighted unified diffs of the manifests
    based on the most-recent optimise run (cached as .qubob_plan.yaml in the
    manifest directory).

bob-opt apply <path>
    Apply the patch diffs to the manifests with automatic .bak backups.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import click
import numpy as np
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.syntax import Syntax
from rich.table import Table

from bob_optimizer.diff.compose_patch import apply_compose_patches, generate_compose_patch
from bob_optimizer.diff.k8s_patch import apply_patches, generate_patch
from bob_optimizer.model.domain import (
    ClusterTopology,
    PlacementPlan,
    ServiceAssignment,
)
from bob_optimizer.model.graph import build_latency_matrix, build_service_graph
from bob_optimizer.model.qubo import build_objective, decompose_cost
from bob_optimizer.parser.compose import parse_compose
from bob_optimizer.parser.kubernetes import parse_manifests
from bob_optimizer.parser.synthetic_generator import make_small_topology
from bob_optimizer.solvers.greedy import GreedyFFDSolver

console = Console()

# Cached plan filename written next to the manifests directory
_PLAN_FILENAME = ".qubob_plan.json"

# ---------------------------------------------------------------------------
# Solver registry
# ---------------------------------------------------------------------------

_SOLVER_CHOICES = ["qiea", "ga", "greedy"]


def _get_solver(algorithm: str) -> Any:
    """Return a solver instance for the given algorithm name."""
    algo = algorithm.lower()
    if algo == "qiea":
        from bob_optimizer.solvers.qiea import QIEASolver
        return QIEASolver()
    if algo == "ga":
        from bob_optimizer.solvers.classical import ClassicalGASolver
        return ClassicalGASolver()
    if algo == "greedy":
        return GreedyFFDSolver()
    raise click.BadParameter(f"Unknown algorithm '{algorithm}'. Choose from: {_SOLVER_CHOICES}")


# ---------------------------------------------------------------------------
# Manifest detection helpers
# ---------------------------------------------------------------------------


def _is_compose_file(path: Path) -> bool:
    """Return True if *path* looks like a Docker Compose file."""
    name = path.name.lower()
    return "compose" in name and path.suffix in {".yml", ".yaml"}


def _collect_manifest_paths(path_arg: str) -> tuple[list[Path], bool]:
    """Return (paths, is_compose) from a path argument.

    If *path_arg* is a file: returns ``([path], is_compose)``.
    If *path_arg* is a directory: collects all ``.yml/.yaml`` files,
    detects whether any are Compose files, and returns accordingly.
    """
    p = Path(path_arg)
    if p.is_file():
        return [p], _is_compose_file(p)
    if p.is_dir():
        yaml_files = sorted(p.glob("**/*.yml")) + sorted(p.glob("**/*.yaml"))
        compose_files = [f for f in yaml_files if _is_compose_file(f)]
        k8s_files = [f for f in yaml_files if not _is_compose_file(f)]
        if compose_files:
            return compose_files, True
        return k8s_files, False
    raise click.BadParameter(f"Path '{path_arg}' does not exist.")


def _parse_topology(paths: list[Path], is_compose: bool) -> ClusterTopology:
    """Parse manifests into a ClusterTopology."""
    if is_compose:
        # Use first Compose file found
        return parse_compose(paths[0])
    return parse_manifests(paths)


def _plan_cache_path(paths: list[Path]) -> Path:
    """Return the path where the plan JSON cache is stored."""
    base = paths[0].parent
    return base / _PLAN_FILENAME


def _save_plan(plan: PlacementPlan, topology: ClusterTopology, cache_path: Path) -> None:
    """Serialise plan + topology metadata to a JSON cache file."""
    data = {
        "plan": plan.model_dump(),
        "topology_name": topology.name,
        "n_services": topology.n_services,
        "n_nodes": topology.n_nodes,
    }
    cache_path.write_text(json.dumps(data, indent=2))


def _load_plan(cache_path: Path) -> PlacementPlan | None:
    """Load a cached PlacementPlan from *cache_path*, or return None."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
        return PlacementPlan.model_validate(data["plan"])
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Synthetic node injection (for manifests that have no nodes)
# ---------------------------------------------------------------------------


def _inject_demo_nodes(topology: ClusterTopology) -> ClusterTopology:
    """Inject three synthetic nodes when topology has none (k8s parser output)."""
    if topology.n_nodes > 0:
        return topology
    demo = make_small_topology(seed=0)
    # Use the first min(3, n_services) nodes from the demo
    n_nodes = min(3, max(1, topology.n_services))
    nodes = demo.nodes[:n_nodes]
    return topology.model_copy(update={"nodes": nodes})


# ---------------------------------------------------------------------------
# Rich rendering helpers
# ---------------------------------------------------------------------------


def _render_dependency_graph(topology: ClusterTopology) -> None:
    """Print service dependency graph summary using Rich."""
    g = build_service_graph(topology)
    table = Table(title="Service Dependency Graph", show_header=True, header_style="bold cyan")
    table.add_column("Service", style="green")
    table.add_column("Replicas", justify="right")
    table.add_column("CPU (m)", justify="right")
    table.add_column("Mem (MiB)", justify="right")
    table.add_column("Out-edges", justify="right")
    table.add_column("In-edges", justify="right")

    for svc in topology.services:
        out_deg = g.out_degree(svc.name)
        in_deg = g.in_degree(svc.name)
        table.add_row(
            svc.name,
            str(svc.replicas),
            str(svc.resources.cpu_millicores),
            str(svc.resources.memory_mib),
            str(out_deg),
            str(in_deg),
        )
    console.print(table)


def _render_latency_matrix(topology: ClusterTopology) -> None:
    """Print a compact RTT matrix for the cluster nodes."""
    if topology.n_nodes == 0:
        console.print("[yellow]No nodes in topology — latency matrix unavailable.[/yellow]")
        return

    mat = build_latency_matrix(topology)
    node_names = [n.name for n in topology.nodes]

    table = Table(title="Node RTT Latency Matrix (ms)", show_header=True, header_style="bold magenta")
    table.add_column("From \\ To", style="bold")
    for name in node_names:
        table.add_column(name, justify="right")

    for i, row_name in enumerate(node_names):
        cells = [f"{mat[i, j]:.1f}" for j in range(len(node_names))]
        table.add_row(row_name, *cells)
    console.print(table)


def _render_placement_table(plan: PlacementPlan, topology: ClusterTopology) -> None:
    """Print the optimal placement table with cost breakdown."""
    table = Table(
        title=f"Optimal Placement Plan  [solver: {plan.solver_name}]",
        show_header=True,
        header_style="bold green",
    )
    table.add_column("Service", style="cyan")
    table.add_column("Node", style="yellow")
    table.add_column("Zone", style="blue")

    for a in plan.assignments:
        table.add_row(a.service_name, a.node_name, a.zone or "—")
    console.print(table)

    # Cost breakdown
    cost_table = Table(title="Cost Breakdown", show_header=True, header_style="bold red")
    cost_table.add_column("Component", style="bold")
    cost_table.add_column("Value", justify="right")
    cost_table.add_row("Latency cost (H_lat)", f"{plan.latency_cost:.4f}")
    cost_table.add_row("Resource cost (H_res)", f"{plan.resource_cost:.4f}")
    cost_table.add_row("Affinity cost (H_aff)", f"{plan.affinity_cost:.4f}")
    cost_table.add_row("Penalty cost (H_pen)", f"{plan.penalty_cost:.4f}")
    cost_table.add_row("[bold]Total cost[/bold]", f"[bold]{plan.total_cost:.4f}[/bold]")
    cost_table.add_row("Solve time (s)", f"{plan.solve_time_s:.3f}")
    console.print(cost_table)


def _solution_to_plan(
    best_solution: np.ndarray,
    topology: ClusterTopology,
    solver_name: str,
    solve_time_s: float,
    cost_components: dict[str, float],
) -> PlacementPlan:
    """Convert a flat binary solution vector to a ``PlacementPlan``."""
    n_nodes = topology.n_nodes
    assignments: list[ServiceAssignment] = []
    for i, svc in enumerate(topology.services):
        row = best_solution[i * n_nodes : (i + 1) * n_nodes]
        j = int(np.argmax(row)) if row.any() else 0
        node = topology.nodes[j]
        assignments.append(
            ServiceAssignment(
                service_name=svc.name,
                node_name=node.name,
                zone=node.zone,
            )
        )
    return PlacementPlan(
        assignments=tuple(assignments),
        total_cost=cost_components["total"],
        latency_cost=cost_components["latency"],
        resource_cost=cost_components["resource"],
        affinity_cost=cost_components["affinity"],
        penalty_cost=cost_components["penalty"],
        solver_name=solver_name,
        solve_time_s=solve_time_s,
    )


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="qubob")
def cli() -> None:
    """bob-opt: Quantum-Inspired microservice placement optimiser."""


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path")
def analyze(path: str) -> None:
    """Ingest manifests at PATH and display the service graph, RTT matrix, and bottlenecks."""
    console.print(Panel("[bold cyan]QUBOB — Manifest Analysis[/bold cyan]", expand=False))

    paths, is_compose = _collect_manifest_paths(path)
    console.print(
        f"[dim]Detected {'Docker Compose' if is_compose else 'Kubernetes'} manifests "
        f"({len(paths)} file(s))[/dim]"
    )

    topology = _parse_topology(paths, is_compose)
    topology = _inject_demo_nodes(topology)

    console.print(
        f"\n[bold]Topology:[/bold] {topology.name}  "
        f"[cyan]{topology.n_services}[/cyan] services  "
        f"[yellow]{topology.n_nodes}[/yellow] nodes  "
        f"[green]{len(topology.dependencies)}[/green] dependencies\n"
    )

    _render_dependency_graph(topology)
    console.print()
    _render_latency_matrix(topology)

    # Bottleneck report: top-5 heaviest dependency edges
    if topology.dependencies:
        console.print()
        top_deps = sorted(topology.dependencies, key=lambda d: d.calls_per_second, reverse=True)[:5]
        bot_table = Table(title="Top Latency Bottleneck Edges", header_style="bold red")
        bot_table.add_column("Source", style="cyan")
        bot_table.add_column("Target", style="yellow")
        bot_table.add_column("RPS", justify="right")
        bot_table.add_column("Protocol")
        for dep in top_deps:
            bot_table.add_row(dep.source, dep.target, f"{dep.calls_per_second:.1f}", dep.protocol)
        console.print(bot_table)


# ---------------------------------------------------------------------------
# optimize
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path")
@click.option(
    "--algorithm",
    "-a",
    default="greedy",
    type=click.Choice(_SOLVER_CHOICES, case_sensitive=False),
    show_default=True,
    help="Solver algorithm.",
)
@click.option(
    "--generations",
    "-g",
    default=200,
    show_default=True,
    type=int,
    help="Number of generations / iterations (QIEA and GA only).",
)
@click.option("--seed", default=42, show_default=True, type=int, help="RNG seed.")
def optimize(path: str, algorithm: str, generations: int, seed: int) -> None:
    """Run the solver on manifests at PATH and output the optimal placement."""
    console.print(
        Panel(
            f"[bold green]QUBOB — Optimise[/bold green]  algorithm=[cyan]{algorithm}[/cyan]  "
            f"generations=[yellow]{generations}[/yellow]",
            expand=False,
        )
    )

    paths, is_compose = _collect_manifest_paths(path)
    topology = _parse_topology(paths, is_compose)
    topology = _inject_demo_nodes(topology)

    console.print(
        f"[dim]Topology: {topology.name}  {topology.n_services} services × "
        f"{topology.n_nodes} nodes  {topology.n_variables} QUBO variables[/dim]\n"
    )

    objective = build_objective(topology)
    solver = _get_solver(algorithm)

    # Support --generations for QIEA / GA via kwargs if available
    solver_kwargs: dict[str, Any] = {"seed": seed}
    if algorithm in {"qiea", "ga"}:
        solver_kwargs["n_generations"] = generations

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Running {algorithm.upper()} solver…", total=None)
        t0 = time.perf_counter()
        try:
            result = solver.solve(
                objective,
                topology.n_variables,
                topology.n_nodes,
                **solver_kwargs,
            )
        except TypeError:
            # Solver doesn't accept n_generations kwarg — run without it
            result = solver.solve(
                objective,
                topology.n_variables,
                topology.n_nodes,
                seed=seed,
            )
        elapsed = time.perf_counter() - t0
        progress.update(task, completed=1, total=1)

    cost_components = decompose_cost(topology, result.best_solution)
    plan = _solution_to_plan(
        result.best_solution, topology, algorithm.upper(), elapsed, cost_components
    )

    _render_placement_table(plan, topology)

    # Compute naive baseline (all services on node-0) for latency reduction %
    naive_x = np.zeros(topology.n_variables, dtype=np.float64)
    for i in range(topology.n_services):
        naive_x[i * topology.n_nodes] = 1.0
    naive_cost = decompose_cost(topology, naive_x)
    if naive_cost["total"] > 0:
        reduction_pct = 100.0 * (naive_cost["total"] - plan.total_cost) / naive_cost["total"]
        console.print(
            f"\n[bold green]Cost reduction vs naive baseline:[/bold green] "
            f"[cyan]{reduction_pct:+.1f}%[/cyan]"
        )

    # Cache plan for diff/apply sub-commands
    cache_path = _plan_cache_path(paths)
    _save_plan(plan, topology, cache_path)
    console.print(f"[dim]Plan cached → {cache_path}[/dim]")


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


@cli.command("diff")
@click.argument("path")
def diff_cmd(path: str) -> None:
    """Preview syntax-highlighted unified diffs for manifests at PATH."""
    console.print(Panel("[bold magenta]QUBOB — Diff Preview[/bold magenta]", expand=False))

    paths, is_compose = _collect_manifest_paths(path)
    cache_path = _plan_cache_path(paths)
    plan = _load_plan(cache_path)
    if plan is None:
        console.print(
            "[red]No cached plan found. Run [bold]bob-opt optimize[/bold] first.[/red]"
        )
        raise SystemExit(1)

    any_diff = False
    for p in paths:
        if is_compose:
            diff_text = generate_compose_patch(p, plan)
        else:
            diff_text = generate_patch(p, plan)

        if diff_text:
            any_diff = True
            console.print(f"\n[bold]{p}[/bold]")
            console.print(Syntax(diff_text, "diff", theme="monokai", line_numbers=True))

    if not any_diff:
        console.print("[yellow]No patches to apply — manifests already match the plan.[/yellow]")


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path")
@click.option(
    "--no-backup",
    is_flag=True,
    default=False,
    help="Skip creating .bak backups (dangerous — use with care).",
)
def apply(path: str, no_backup: bool) -> None:
    """Apply placement patches to manifests at PATH (with .bak backups by default)."""
    console.print(Panel("[bold yellow]QUBOB — Apply Patches[/bold yellow]", expand=False))

    paths, is_compose = _collect_manifest_paths(path)
    cache_path = _plan_cache_path(paths)
    plan = _load_plan(cache_path)
    if plan is None:
        console.print(
            "[red]No cached plan found. Run [bold]bob-opt optimize[/bold] first.[/red]"
        )
        raise SystemExit(1)

    backup = not no_backup

    if is_compose:
        modified = apply_compose_patches(paths, plan, backup=backup)
    else:
        topology = _parse_topology(paths, is_compose)
        modified = apply_patches(paths, plan, topology, backup=backup)

    if modified:
        for mp in modified:
            console.print(f"[green]✓[/green] Patched: {mp}")
            if backup:
                console.print(f"  [dim]Backup:  {mp.with_suffix(mp.suffix + '.bak')}[/dim]")
        console.print(f"\n[bold green]{len(modified)} file(s) updated.[/bold green]")
    else:
        console.print("[yellow]No files were modified — nothing to patch.[/yellow]")


# ---------------------------------------------------------------------------
# Legacy sub-commands kept for backwards compatibility
# ---------------------------------------------------------------------------


@cli.command(hidden=True)
@click.option("--manifest", "-m", required=True, help="Path to k8s manifest dir or file.")
@click.option(
    "--solver",
    "-s",
    default="greedy",
    type=click.Choice(["qiea", "ga", "greedy"], case_sensitive=False),
    show_default=True,
)
@click.option("--output", "-o", default="-", help="Output file for the patch (default: stdout).")
def optimise(manifest: str, solver: str, output: str) -> None:
    """[Legacy] Optimise and emit a nodeAffinity patch diff."""
    ctx = click.get_current_context()
    # Delegate to the new 'optimize' command
    ctx.invoke(optimize, path=manifest, algorithm=solver, generations=200, seed=42)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=8080, show_default=True, type=int, help="Bind port.")
@click.option("--no-browser", is_flag=True, default=False, help="Do not open browser.")
def dashboard(host: str, port: int, no_browser: bool) -> None:
    """Launch the interactive visual dashboard in the browser."""
    console.print(
        Panel(
            f"[bold cyan]QUBOB Dashboard[/bold cyan]\n"
            f"[dim]http://{host}:{port}[/dim]",
            expand=False,
        )
    )
    try:
        from bob_optimizer.dashboard.app import run_dashboard
    except ImportError as exc:
        console.print(f"[red]Dashboard dependencies missing:[/red] {exc}")
        console.print("Install with: [bold]pip install fastapi uvicorn[/bold]")
        raise SystemExit(1)
    run_dashboard(host=host, port=port, open_browser=not no_browser)


@cli.command(hidden=True)
@click.argument("service_name")
def explain(service_name: str) -> None:
    """Explain why SERVICE_NAME was placed on its assigned node."""
    console.print(
        f"[yellow]Explanation for '{service_name}' — run [bold]bob-opt optimize[/bold] first, "
        "then re-run explain.[/yellow]"
    )


@cli.command(hidden=True)
@click.option("--solvers", default="qiea,ga,greedy", show_default=True)
@click.option("--sizes", default="10,20,50", show_default=True)
@click.option("--instances", default=5, show_default=True)
def benchmark(solvers: str, sizes: str, instances: int) -> None:
    """Run comparative benchmark across solvers and problem sizes."""
    console.print(f"[dim]benchmark: solvers={solvers}  sizes={sizes}  instances={instances}[/dim]")
    console.print("[yellow]Benchmark suite coming in Step 5.[/yellow]")


if __name__ == "__main__":
    cli()
