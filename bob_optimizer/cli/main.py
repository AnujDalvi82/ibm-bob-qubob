"""QUBOB CLI — entrypoint for Bob IDE integration."""

from __future__ import annotations

import click


@click.group()
@click.version_option(package_name="qubob")
def cli() -> None:
    """QUBOB: Quantum-Inspired microservice placement optimiser."""


@cli.command()
@click.option("--manifest", "-m", required=True, help="Path to k8s manifest dir or file.")
@click.option(
    "--solver",
    "-s",
    default="qiea",
    type=click.Choice(["qiea", "sqa", "greedy", "sa"], case_sensitive=False),
    show_default=True,
    help="Solver algorithm to use.",
)
@click.option("--output", "-o", default="-", help="Output file for the patch (default: stdout).")
def optimise(manifest: str, solver: str, output: str) -> None:
    """Optimise service placement and emit a nodeAffinity patch diff."""
    click.echo(f"[qubob] solver={solver}  manifest={manifest}  output={output}")
    click.echo("[qubob] Step 2–7 implementation pending — scaffold complete.")


@cli.command()
@click.argument("service_name")
def explain(service_name: str) -> None:
    """Explain why SERVICE_NAME was placed on its assigned node."""
    click.echo(f"[qubob] Explanation for '{service_name}' — pending Step 7.")


@cli.command()
@click.option(
    "--solvers",
    default="qiea,sqa,greedy",
    show_default=True,
    help="Comma-separated list of solvers to benchmark.",
)
@click.option(
    "--sizes",
    default="10,20,50",
    show_default=True,
    help="Comma-separated list of problem sizes (number of services).",
)
@click.option("--instances", default=5, show_default=True, help="Random instances per size.")
def benchmark(solvers: str, sizes: str, instances: int) -> None:
    """Run comparative benchmark across solvers and problem sizes."""
    click.echo(f"[qubob] benchmark: solvers={solvers}  sizes={sizes}  instances={instances}")
    click.echo("[qubob] Benchmark suite pending Step 9.")


if __name__ == "__main__":
    cli()
