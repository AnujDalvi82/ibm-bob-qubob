"""QUBOB Dashboard — FastAPI application.

Serves a single-page HTML5/SVG/Canvas dashboard that visualises:
  - Service dependency graph
  - Before vs After latency heatmap
  - Node CPU & RAM packing bars
  - Quantum convergence curves (QIEA vs GA vs Greedy)

All topology data and solver results are computed on the server side and
delivered to the frontend as JSON via /api/topology and /api/benchmark.

REST Action Endpoints (watsonx Orchestrate integration)
-------------------------------------------------------
POST /api/v1/analyze   — parse manifests, return topology + bottlenecks
POST /api/v1/optimize  — run solver, return PlacementPlan + cost breakdown

Cluster Registry
----------------
The QUBOB_CLUSTER_REGISTRY_JSON environment variable maps logical cluster_id
strings to manifest paths (12-factor cloud best practice).  Raw filesystem
paths are never exposed in API payloads.

Built-in fallbacks (always available):
  "demo"       → synthetic 10-service ecommerce topology
  "ecommerce"  → examples/ecommerce/ directory (or synthetic if absent)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np

# ---------------------------------------------------------------------------
# Ensure project root is importable when run via `python -m`
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel, Field
    import uvicorn  # type: ignore[import-untyped]
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

from bob_optimizer.model.graph import build_latency_matrix
from bob_optimizer.model.qubo import build_objective, decompose_cost
from bob_optimizer.parser.synthetic_generator import _build_topology
from bob_optimizer.solvers.baseline_ga import ClassicalGASolver
from bob_optimizer.solvers.greedy import GreedyFFDSolver
from bob_optimizer.solvers.qiea import QIEASolver

# ---------------------------------------------------------------------------
# Cluster Registry — maps logical cluster_id → manifest path
# ---------------------------------------------------------------------------
# 12-factor best practice: configure via QUBOB_CLUSTER_REGISTRY_JSON env var.
# Raw filesystem paths are never exposed in API request/response payloads.
#
# Built-in fallbacks (always available without configuration):
#   "demo"       → synthetic 10-service ecommerce topology (no manifests needed)
#   "ecommerce"  → examples/ecommerce/ directory, or synthetic if absent
# ---------------------------------------------------------------------------

_EXAMPLES_DIR = _ROOT / "examples"

_BUILTIN_REGISTRY: dict[str, str] = {
    "demo": "__synthetic__",
    "ecommerce": str(_EXAMPLES_DIR / "ecommerce"),
}

def _load_cluster_registry() -> dict[str, str]:
    """Load cluster registry from env var, merged with built-in fallbacks."""
    registry = dict(_BUILTIN_REGISTRY)
    raw = os.environ.get("QUBOB_CLUSTER_REGISTRY_JSON", "")
    if raw:
        try:
            user_registry = json.loads(raw)
            if isinstance(user_registry, dict):
                registry.update(user_registry)
        except json.JSONDecodeError:
            pass  # Malformed env var — silently fall back to built-ins
    return registry


CLUSTER_REGISTRY: dict[str, str] = _load_cluster_registry()


# ---------------------------------------------------------------------------
# REST API — Pydantic request / response models
# ---------------------------------------------------------------------------
# These models are only instantiated when FastAPI is available.  They use
# string-literal annotations (from __future__ import annotations) so the
# module can be imported even when pydantic is absent.
# ---------------------------------------------------------------------------

if _FASTAPI_AVAILABLE:
    class AnalyzeRequest(BaseModel):  # type: ignore[misc]
        cluster_id: str = Field(..., description="Logical cluster name registered on this server")
        manifest_type: Literal["auto", "kubernetes", "compose"] = Field(
            "auto", description="Manifest format hint"
        )

    class BottleneckEdge(BaseModel):  # type: ignore[misc]
        source: str
        target: str
        calls_per_second: float
        protocol: str

    class AnalyzeResponse(BaseModel):  # type: ignore[misc]
        cluster_id: str
        n_services: int
        n_nodes: int
        n_dependencies: int
        bottlenecks: list[BottleneckEdge]
        latency_matrix: list[list[float]]

    class ServiceAssignmentOut(BaseModel):  # type: ignore[misc]
        service_name: str
        node_name: str
        zone: str

    class OptimizeRequest(BaseModel):  # type: ignore[misc]
        cluster_id: str = Field(..., description="Logical cluster name registered on this server")
        algorithm: Literal["qiea", "ga", "greedy"] = Field(
            "greedy", description="Solver algorithm"
        )
        generations: int = Field(200, ge=1, description="Iteration budget for qiea/ga")
        seed: int = Field(42, description="RNG seed for reproducibility")

    class OptimizeResponse(BaseModel):  # type: ignore[misc]
        cluster_id: str
        solver_name: str
        solve_time_s: float
        latency_reduction_pct: float
        total_cost: float
        latency_cost: float
        resource_cost: float
        affinity_cost: float
        penalty_cost: float
        is_feasible: bool
        assignments: list[ServiceAssignmentOut]


# ---------------------------------------------------------------------------
# Cluster resolution helpers
# ---------------------------------------------------------------------------


def _resolve_cluster(cluster_id: str) -> Any | None:
    """Return a ClusterTopology for *cluster_id*, or None if not registered."""
    from bob_optimizer.model.domain import ClusterTopology  # noqa: PLC0415

    path_str = CLUSTER_REGISTRY.get(cluster_id)
    if path_str is None:
        return None

    if path_str == "__synthetic__":
        return _build_topology("ecommerce-demo", n_services=10, n_nodes=3, seed=7)

    # Real manifest path
    p = Path(path_str)
    if not p.exists():
        # Path registered but missing — fall back to synthetic for registered name
        return _build_topology(cluster_id, n_services=10, n_nodes=3, seed=7)

    # Detect compose vs k8s
    from bob_optimizer.cli.main import (  # noqa: PLC0415
        _collect_manifest_paths,
        _inject_demo_nodes,
        _parse_topology,
    )

    try:
        paths, is_compose = _collect_manifest_paths(str(p))
        topology = _parse_topology(paths, is_compose)
        return _inject_demo_nodes(topology)
    except Exception:  # noqa: BLE001
        return None


def _get_solver_instance(algorithm: str, generations: int) -> Any:
    """Return a configured solver instance."""
    algo = algorithm.lower()
    if algo == "qiea":
        return QIEASolver(n_generations=generations)
    if algo == "ga":
        return ClassicalGASolver(n_generations=generations)
    return GreedyFFDSolver()


# ---------------------------------------------------------------------------
# Build demo topology (ecommerce-like 10-service cluster)
# ---------------------------------------------------------------------------

_DEMO_TOPO = _build_topology("ecommerce-demo", n_services=10, n_nodes=3, seed=7)
_OBJECTIVE = build_objective(_DEMO_TOPO)


# ---------------------------------------------------------------------------
# Pre-run solvers and cache results at import time (fast enough for demo)
# ---------------------------------------------------------------------------


def _run_solvers(seed: int = 42) -> dict[str, Any]:
    """Run all three solvers on the demo topology and return serialisable results."""
    solvers = {
        "QIEA": QIEASolver(population_size=20, n_generations=100),
        "Classical GA": ClassicalGASolver(population_size=30, n_generations=100),
        "Greedy FFD": GreedyFFDSolver(),
    }
    topo = _DEMO_TOPO
    obj = _OBJECTIVE

    naive_x = np.zeros(topo.n_variables, dtype=np.float64)
    for i in range(topo.n_services):
        naive_x[i * topo.n_nodes] = 1.0
    naive_cost = float(obj(naive_x))
    naive_components = decompose_cost(topo, naive_x)

    results: dict[str, Any] = {
        "naive_cost": naive_cost,
        "naive_latency": naive_components["latency"],
        "solvers": {},
    }

    for name, solver in solvers.items():
        t0 = time.perf_counter()
        result = solver.solve(obj, topo.n_variables, topo.n_nodes, seed=seed)
        elapsed = time.perf_counter() - t0
        components = decompose_cost(topo, result.best_solution)

        # Node assignments: service_index -> node_index
        n_nodes = topo.n_nodes
        assignment = {}
        for i, svc in enumerate(topo.services):
            row = result.best_solution[i * n_nodes : (i + 1) * n_nodes]
            j = int(np.argmax(row)) if row.any() else 0
            assignment[svc.name] = topo.nodes[j].name

        results["solvers"][name] = {
            "best_cost": float(result.best_cost),
            "latency_cost": float(components["latency"]),
            "resource_cost": float(components["resource"]),
            "solve_time_s": round(elapsed, 4),
            "convergence": [(int(g), float(c)) for g, c in result.convergence_history],
            "assignment": assignment,
            "latency_reduction_pct": (
                100.0 * (naive_cost - result.best_cost) / naive_cost if naive_cost > 0 else 0.0
            ),
        }

    return results


# Pre-compute at module load
_SOLVER_RESULTS: dict[str, Any] = _run_solvers()


# ---------------------------------------------------------------------------
# Topology serialisation
# ---------------------------------------------------------------------------


def _topology_json() -> dict[str, Any]:
    topo = _DEMO_TOPO
    services = [
        {
            "name": s.name,
            "cpu_m": s.resources.cpu_millicores,
            "mem_mib": s.resources.memory_mib,
            "replicas": s.replicas,
        }
        for s in topo.services
    ]
    nodes = [
        {
            "name": n.name,
            "zone": n.zone,
            "cpu_m": n.allocatable_cpu_millicores,
            "mem_mib": n.allocatable_memory_mib,
        }
        for n in topo.nodes
    ]
    deps = [
        {"source": d.source, "target": d.target, "rps": d.calls_per_second}
        for d in topo.dependencies
    ]

    # Latency matrix
    n = topo.n_nodes
    lat_matrix = [[topo.nodes[i].latency_to(topo.nodes[j]) for j in range(n)] for i in range(n)]

    # Node utilisation per solver (best assignment)
    node_util: dict[str, list[dict[str, float]]] = {}
    for solver_name, sdata in _SOLVER_RESULTS["solvers"].items():
        assignment = sdata["assignment"]
        node_cpu: dict[str, float] = {nd.name: 0.0 for nd in topo.nodes}
        node_mem: dict[str, float] = {nd.name: 0.0 for nd in topo.nodes}
        for svc in topo.services:
            nd_name = assignment.get(svc.name, topo.nodes[0].name)
            node_cpu[nd_name] = node_cpu.get(nd_name, 0.0) + svc.resources.cpu_millicores
            node_mem[nd_name] = node_mem.get(nd_name, 0.0) + svc.resources.memory_mib

        node_util[solver_name] = [
            {
                "name": nd.name,
                "zone": nd.zone,
                "cpu_used": node_cpu[nd.name],
                "cpu_total": nd.allocatable_cpu_millicores,
                "mem_used": node_mem[nd.name],
                "mem_total": nd.allocatable_memory_mib,
                "cpu_pct": 100.0 * node_cpu[nd.name] / nd.allocatable_cpu_millicores,
                "mem_pct": 100.0 * node_mem[nd.name] / nd.allocatable_memory_mib,
            }
            for nd in topo.nodes
        ]

    return {
        "services": services,
        "nodes": nodes,
        "dependencies": deps,
        "latency_matrix": lat_matrix,
        "node_util": node_util,
        "solver_results": _SOLVER_RESULTS,
    }


# ---------------------------------------------------------------------------
# HTML dashboard (self-contained, inline CSS + JS)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QUBOB Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
         background: #0d1117; color: #e6edf3; font-size: 14px; line-height: 1.5; }
  header { background: #161b22; border-bottom: 1px solid #30363d; padding: 12px 24px;
           display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 700; color: #58a6ff; }
  header .badge { background: #1f6feb; color: #fff; font-size: 11px; padding: 2px 8px;
                  border-radius: 12px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
          padding: 16px; max-width: 1400px; margin: 0 auto; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 16px; }
  .card h2 { font-size: 13px; font-weight: 600; color: #8b949e; text-transform: uppercase;
             letter-spacing: .05em; margin-bottom: 12px; }
  svg { width: 100%; overflow: visible; }
  canvas { display: block; }
  .solver-tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .tab { padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;
         font-weight: 600; border: 1px solid #30363d; background: #21262d; color: #8b949e;
         transition: all .15s; }
  .tab.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
  .legend { display: flex; gap: 16px; font-size: 11px; color: #8b949e; margin-bottom: 8px;
            flex-wrap: wrap; }
  .legend span { display: flex; align-items: center; gap: 4px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .metric-row { display: flex; justify-content: space-between; align-items: baseline;
                padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 13px; }
  .metric-row:last-child { border-bottom: none; }
  .metric-val { font-weight: 700; color: #58a6ff; }
  .metric-val.green { color: #3fb950; }
  .metric-val.red { color: #f85149; }
  .bar-row { margin-bottom: 10px; }
  .bar-label { display: flex; justify-content: space-between; font-size: 12px;
               color: #8b949e; margin-bottom: 3px; }
  .bar-bg { background: #21262d; border-radius: 4px; height: 14px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width .5s ease; }
  .bar-cpu { background: linear-gradient(90deg, #1f6feb, #58a6ff); }
  .bar-mem { background: linear-gradient(90deg, #8957e5, #d2a8ff); }
  @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>QUBOB Dashboard</h1>
  <span class="badge">Quantum-Inspired</span>
  <span class="badge" style="background:#238636">Live</span>
  <span id="topo-info" style="color:#8b949e;font-size:12px;margin-left:auto;"></span>
</header>

<div class="grid">

  <!-- 1. Dependency Graph -->
  <div class="card" style="grid-column:1/3">
    <h2>Service Dependency Graph</h2>
    <div class="solver-tabs" id="graph-tabs"></div>
    <svg id="dep-graph" height="320" viewBox="0 0 1200 320"></svg>
  </div>

  <!-- 2. Latency Heatmap -->
  <div class="card">
    <h2>Before vs After Latency Heatmap (ms)</h2>
    <div class="solver-tabs" id="heatmap-tabs"></div>
    <canvas id="heatmap-canvas" height="260"></canvas>
  </div>

  <!-- 3. Convergence Curves -->
  <div class="card">
    <h2>Quantum Convergence Curves</h2>
    <div class="legend" id="conv-legend"></div>
    <canvas id="conv-canvas" height="260"></canvas>
  </div>

  <!-- 4. Node Packing -->
  <div class="card">
    <h2>Node Packing — CPU &amp; RAM Utilisation</h2>
    <div class="solver-tabs" id="pack-tabs"></div>
    <div id="pack-bars"></div>
  </div>

  <!-- 5. Solver Summary -->
  <div class="card">
    <h2>Solver Comparison</h2>
    <div id="solver-metrics"></div>
  </div>

</div>

<script>
const SOLVER_COLORS = {
  'QIEA': '#58a6ff',
  'Classical GA': '#ffa657',
  'Greedy FFD': '#3fb950',
};

let _data = null;
let _activeSolver = 'QIEA';

// ─── Fetch & boot ───────────────────────────────────────────────────────────
async function boot() {
  const r = await fetch('/api/topology');
  _data = await r.json();

  const solvers = Object.keys(_data.solver_results.solvers);

  document.getElementById('topo-info').textContent =
    `${_data.services.length} services · ${_data.nodes.length} nodes · ${_data.dependencies.length} dependencies`;

  buildTabs('graph-tabs', solvers, s => { _activeSolver = s; drawGraph(); });
  buildTabs('heatmap-tabs', ['Before (Naive)', ...solvers], s => drawHeatmap(s));
  buildTabs('pack-tabs', solvers, s => drawPacking(s));

  drawGraph();
  drawHeatmap('Before (Naive)');
  drawConvergence();
  drawPacking(solvers[0]);
  drawSolverMetrics();
}

function buildTabs(containerId, labels, onClick) {
  const el = document.getElementById(containerId);
  el.innerHTML = '';
  labels.forEach((lbl, i) => {
    const btn = document.createElement('button');
    btn.className = 'tab' + (i === 0 ? ' active' : '');
    btn.textContent = lbl;
    btn.onclick = () => {
      el.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      onClick(lbl);
    };
    el.appendChild(btn);
  });
}

// ─── Dependency Graph ────────────────────────────────────────────────────────
function drawGraph() {
  const svg = document.getElementById('dep-graph');
  const W = svg.viewBox.baseVal.width;
  const H = svg.viewBox.baseVal.height;
  svg.innerHTML = '';

  const services = _data.services;
  const deps = _data.dependencies;
  const assignment = _data.solver_results.solvers[_activeSolver]?.assignment || {};
  const nodes = _data.nodes;
  const nodeColors = {'us-east-1a':'#1f6feb','us-east-1b':'#8957e5','us-east-1c':'#238636'};

  // Circular layout
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.38;
  const pos = {};
  services.forEach((s, i) => {
    const angle = (2 * Math.PI * i / services.length) - Math.PI / 2;
    pos[s.name] = { x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle) };
  });

  const maxRps = Math.max(...deps.map(d => d.rps));

  // Edges
  deps.forEach(d => {
    const s = pos[d.source], t = pos[d.target];
    if (!s || !t) return;
    const w = 0.5 + 3.5 * (d.rps / maxRps);
    const alpha = 0.15 + 0.7 * (d.rps / maxRps);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', s.x); line.setAttribute('y1', s.y);
    line.setAttribute('x2', t.x); line.setAttribute('y2', t.y);
    line.setAttribute('stroke', `rgba(88,166,255,${alpha})`);
    line.setAttribute('stroke-width', w);
    svg.appendChild(line);
  });

  // Nodes
  services.forEach(s => {
    const p = pos[s.name];
    const assignedNode = assignment[s.name];
    const zone = nodes.find(n => n.name === assignedNode)?.zone || 'us-east-1a';
    const color = nodeColors[zone] || '#58a6ff';
    const r = 6 + Math.sqrt(s.cpu_m) * 0.25;

    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', p.x); circle.setAttribute('cy', p.y);
    circle.setAttribute('r', r);
    circle.setAttribute('fill', color);
    circle.setAttribute('stroke', '#30363d'); circle.setAttribute('stroke-width', 1.5);
    svg.appendChild(circle);

    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', p.x); text.setAttribute('y', p.y - r - 4);
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('font-size', '11'); text.setAttribute('fill', '#e6edf3');
    text.textContent = s.name;
    svg.appendChild(text);

    const nodeText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    nodeText.setAttribute('x', p.x); nodeText.setAttribute('y', p.y + r + 13);
    nodeText.setAttribute('text-anchor', 'middle');
    nodeText.setAttribute('font-size', '9'); nodeText.setAttribute('fill', '#8b949e');
    nodeText.textContent = assignedNode || '';
    svg.appendChild(nodeText);
  });

  // Zone legend
  Object.entries(nodeColors).forEach(([zone, color], i) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', 8); rect.setAttribute('y', 8 + i * 18);
    rect.setAttribute('width', 10); rect.setAttribute('height', 10);
    rect.setAttribute('fill', color); rect.setAttribute('rx', 2);
    const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    txt.setAttribute('x', 24); txt.setAttribute('y', 17 + i * 18);
    txt.setAttribute('font-size', '11'); txt.setAttribute('fill', '#8b949e');
    txt.textContent = zone;
    g.appendChild(rect); g.appendChild(txt);
    svg.appendChild(g);
  });
}

// ─── Latency Heatmap ────────────────────────────────────────────────────────
function drawHeatmap(mode) {
  const canvas = document.getElementById('heatmap-canvas');
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.parentElement.clientWidth - 32;

  const services = _data.services;
  const N = services.length;
  const cellSize = Math.min(Math.floor((canvas.width - 80) / N), 36);
  const offsetX = 80, offsetY = 20;
  canvas.height = offsetY + N * cellSize + 40;

  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Build latency matrix for the selected mode
  const latMat = Array.from({length: N}, () => Array(N).fill(0));
  const nodeLatMap = {};
  _data.nodes.forEach(n => {
    nodeLatMap[n.name] = n;
  });
  const nodeLat = _data.latency_matrix;
  const nodeList = _data.nodes;

  let assignment = {};
  if (mode === 'Before (Naive)') {
    services.forEach((s, i) => {
      assignment[s.name] = nodeList[0].name;
    });
  } else {
    assignment = _data.solver_results.solvers[mode]?.assignment || {};
  }

  const deps = _data.dependencies;
  const depMap = {};
  deps.forEach(d => { depMap[`${d.source}|${d.target}`] = d.rps; });

  for (let i = 0; i < N; i++) {
    for (let j = 0; j < N; j++) {
      const key = `${services[i].name}|${services[j].name}`;
      const rps = depMap[key] || 0;
      if (rps === 0) continue;
      const ni = nodeList.findIndex(n => n.name === assignment[services[i].name]);
      const nj = nodeList.findIndex(n => n.name === assignment[services[j].name]);
      const lat = (ni >= 0 && nj >= 0) ? nodeLat[ni][nj] : 8;
      latMat[i][j] = rps * lat;
    }
  }

  const maxVal = Math.max(...latMat.flat(), 1);

  function latColor(v) {
    const t = v / maxVal;
    const r = Math.round(t * 248 + (1 - t) * 35);
    const g = Math.round((1 - t) * 197 + t * 81);
    const b = Math.round((1 - t) * 80 + t * 73);
    return `rgb(${r},${g},${b})`;
  }

  for (let i = 0; i < N; i++) {
    for (let j = 0; j < N; j++) {
      ctx.fillStyle = latColor(latMat[i][j]);
      ctx.fillRect(offsetX + j * cellSize, offsetY + i * cellSize, cellSize - 1, cellSize - 1);
    }
  }

  // Labels
  ctx.fillStyle = '#8b949e';
  ctx.font = `${Math.min(10, cellSize * 0.55)}px system-ui`;
  ctx.textAlign = 'right';
  services.forEach((s, i) => {
    ctx.fillText(s.name, offsetX - 4, offsetY + i * cellSize + cellSize * 0.65);
  });
  ctx.textAlign = 'center';
  ctx.save();
  services.forEach((s, j) => {
    ctx.save();
    ctx.translate(offsetX + j * cellSize + cellSize * 0.5, offsetY - 4);
    ctx.rotate(-Math.PI / 4);
    ctx.fillText(s.name, 0, 0);
    ctx.restore();
  });
}

// ─── Convergence Curves ──────────────────────────────────────────────────────
function drawConvergence() {
  const canvas = document.getElementById('conv-canvas');
  canvas.width = canvas.parentElement.clientWidth - 32;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pad = { top: 20, right: 20, bottom: 40, left: 60 };

  ctx.clearRect(0, 0, W, H);

  const solvers = Object.entries(_data.solver_results.solvers);
  const allCosts = solvers.flatMap(([, s]) => s.convergence.map(([, c]) => c));
  const maxIter = Math.max(...solvers.map(([, s]) => s.convergence.length));
  const minCost = Math.min(...allCosts);
  const maxCost = Math.max(...allCosts);
  const costRange = maxCost - minCost || 1;

  const toX = i => pad.left + (i / (maxIter - 1)) * (W - pad.left - pad.right);
  const toY = c => pad.top + (1 - (c - minCost) / costRange) * (H - pad.top - pad.bottom);

  // Grid
  ctx.strokeStyle = '#21262d';
  ctx.lineWidth = 1;
  for (let y = 0; y <= 4; y++) {
    const yy = pad.top + y * (H - pad.top - pad.bottom) / 4;
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(W - pad.right, yy); ctx.stroke();
    const val = maxCost - y * costRange / 4;
    ctx.fillStyle = '#8b949e'; ctx.font = '10px system-ui'; ctx.textAlign = 'right';
    ctx.fillText(val.toFixed(0), pad.left - 6, yy + 4);
  }

  // Axes labels
  ctx.fillStyle = '#8b949e'; ctx.font = '11px system-ui'; ctx.textAlign = 'center';
  ctx.fillText('Generation', W / 2, H - 6);
  ctx.save(); ctx.translate(12, H / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillText('Objective Cost', 0, 0); ctx.restore();

  // Curves
  solvers.forEach(([name, s]) => {
    const color = SOLVER_COLORS[name] || '#ffffff';
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    s.convergence.forEach(([gen, cost], idx) => {
      const x = toX(gen), y = toY(cost);
      if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });

  // Legend
  const legend = document.getElementById('conv-legend');
  legend.innerHTML = solvers.map(([name]) =>
    `<span><span class="dot" style="background:${SOLVER_COLORS[name]}"></span>${name}</span>`
  ).join('');
}

// ─── Node Packing ────────────────────────────────────────────────────────────
function drawPacking(solverName) {
  const container = document.getElementById('pack-bars');
  container.innerHTML = '';
  const util = _data.node_util[solverName] || [];
  util.forEach(n => {
    const div = document.createElement('div');
    div.className = 'bar-row';
    div.innerHTML = `
      <div class="bar-label">
        <strong>${n.name}</strong><span style="color:#8b949e">${n.zone}</span>
      </div>
      <div class="bar-label"><span>CPU: ${n.cpu_used}m / ${n.cpu_total}m</span><span>${n.cpu_pct.toFixed(1)}%</span></div>
      <div class="bar-bg"><div class="bar-fill bar-cpu" style="width:${Math.min(n.cpu_pct,100)}%"></div></div>
      <div class="bar-label" style="margin-top:4px"><span>RAM: ${n.mem_used}Mi / ${n.mem_total}Mi</span><span>${n.mem_pct.toFixed(1)}%</span></div>
      <div class="bar-bg"><div class="bar-fill bar-mem" style="width:${Math.min(n.mem_pct,100)}%"></div></div>
    `;
    container.appendChild(div);
  });
}

// ─── Solver Metrics ──────────────────────────────────────────────────────────
function drawSolverMetrics() {
  const container = document.getElementById('solver-metrics');
  const naive = _data.solver_results.naive_cost;
  const rows = Object.entries(_data.solver_results.solvers).map(([name, s]) => {
    const red = s.latency_reduction_pct.toFixed(1);
    const cls = parseFloat(red) >= 10 ? 'green' : '';
    return `
      <div class="metric-row">
        <span><span class="dot" style="background:${SOLVER_COLORS[name]};margin-right:6px"></span>${name}</span>
        <span>
          Cost: <span class="metric-val">${s.best_cost.toFixed(2)}</span>
          &nbsp;|&nbsp;
          Red: <span class="metric-val ${cls}">${red}%</span>
          &nbsp;|&nbsp;
          ${s.solve_time_s.toFixed(3)}s
        </span>
      </div>`;
  });
  container.innerHTML = `
    <div class="metric-row"><span>Naive baseline cost</span><span class="metric-val red">${naive.toFixed(2)}</span></div>
    ${rows.join('')}`;
}

window.addEventListener('load', boot);
window.addEventListener('resize', () => {
  if (!_data) return;
  drawHeatmap(document.querySelector('#heatmap-tabs .tab.active')?.textContent || 'Before (Naive)');
  drawConvergence();
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


def create_app() -> "FastAPI":
    """Create and configure the FastAPI dashboard application.

    Routes
    ------
    GET  /                    — Interactive HTML dashboard
    GET  /api/topology        — Demo topology + pre-computed solver results (JSON)
    POST /api/v1/analyze      — Parse manifests for cluster_id; return topology summary
    POST /api/v1/optimize     — Run solver for cluster_id; return PlacementPlan
    GET  /openapi.json        — OpenAPI 3.0 schema (auto-generated by FastAPI)
    """
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError(
            "FastAPI and uvicorn are required for the dashboard. "
            "Install them with: pip install fastapi uvicorn"
        )

    app = FastAPI(
        title="QUBOB Placement Optimization API",
        version="1.0.0",
        description=(
            "Quantum-Inspired microservice placement optimizer. "
            "Exposes analyze and optimize as autonomous actions for "
            "watsonx Orchestrate and watsonx Assistant."
        ),
    )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(content=_DASHBOARD_HTML)

    @app.get("/api/topology", include_in_schema=False)
    async def topology() -> JSONResponse:
        return JSONResponse(content=_topology_json())

    # ------------------------------------------------------------------
    # POST /api/v1/analyze
    # ------------------------------------------------------------------

    @app.post(
        "/api/v1/analyze",
        response_model=AnalyzeResponse,  # type: ignore[name-defined]
        operation_id="qubob_analyze",
        summary="Analyze Kubernetes or Docker Compose manifests",
        tags=["Analysis"],
    )
    async def api_analyze(
        request: AnalyzeRequest,  # type: ignore[name-defined]
    ) -> AnalyzeResponse:  # type: ignore[name-defined]
        """Parse manifests for *cluster_id* and return topology + bottlenecks."""
        from fastapi import HTTPException  # noqa: PLC0415

        topology_obj = _resolve_cluster(request.cluster_id)
        if topology_obj is None:
            raise HTTPException(
                status_code=404,
                detail=f"cluster_id '{request.cluster_id}' is not registered on this server.",
            )

        # Build latency matrix
        lat_mat = build_latency_matrix(topology_obj)
        lat_list: list[list[float]] = lat_mat.tolist()

        # Top-5 bottleneck edges by RPS
        sorted_deps = sorted(
            topology_obj.dependencies,
            key=lambda d: d.calls_per_second,
            reverse=True,
        )[:5]
        bottlenecks = [
            BottleneckEdge(  # type: ignore[name-defined]
                source=d.source,
                target=d.target,
                calls_per_second=d.calls_per_second,
                protocol=d.protocol,
            )
            for d in sorted_deps
        ]

        return AnalyzeResponse(  # type: ignore[name-defined]
            cluster_id=request.cluster_id,
            n_services=topology_obj.n_services,
            n_nodes=topology_obj.n_nodes,
            n_dependencies=len(topology_obj.dependencies),
            bottlenecks=bottlenecks,
            latency_matrix=lat_list,
        )

    # ------------------------------------------------------------------
    # POST /api/v1/optimize
    # ------------------------------------------------------------------

    @app.post(
        "/api/v1/optimize",
        response_model=OptimizeResponse,  # type: ignore[name-defined]
        operation_id="qubob_optimize",
        summary="Run quantum-inspired placement optimizer",
        tags=["Optimization"],
    )
    async def api_optimize(
        request: OptimizeRequest,  # type: ignore[name-defined]
    ) -> OptimizeResponse:  # type: ignore[name-defined]
        """Run solver for *cluster_id* and return the optimal PlacementPlan."""
        from fastapi import HTTPException  # noqa: PLC0415

        topology_obj = _resolve_cluster(request.cluster_id)
        if topology_obj is None:
            raise HTTPException(
                status_code=404,
                detail=f"cluster_id '{request.cluster_id}' is not registered on this server.",
            )

        objective = build_objective(topology_obj)
        solver = _get_solver_instance(request.algorithm, request.generations)

        solver_kwargs: dict[str, Any] = {"seed": request.seed}
        if request.algorithm in {"qiea", "ga"}:
            solver_kwargs["n_generations"] = request.generations

        t0 = time.perf_counter()
        try:
            result = solver.solve(
                objective,
                topology_obj.n_variables,
                topology_obj.n_nodes,
                **solver_kwargs,
            )
        except TypeError:
            result = solver.solve(
                objective,
                topology_obj.n_variables,
                topology_obj.n_nodes,
                seed=request.seed,
            )
        elapsed = time.perf_counter() - t0

        cost_components = decompose_cost(topology_obj, result.best_solution)

        # Build assignments list
        n_nodes = topology_obj.n_nodes
        assignments = []
        for i, svc in enumerate(topology_obj.services):
            row = result.best_solution[i * n_nodes : (i + 1) * n_nodes]
            j = int(np.argmax(row)) if row.any() else 0
            node = topology_obj.nodes[j]
            assignments.append(
                ServiceAssignmentOut(  # type: ignore[name-defined]
                    service_name=svc.name,
                    node_name=node.name,
                    zone=node.zone,
                )
            )

        # Latency reduction vs naive baseline (all on node-0)
        naive_x = np.zeros(topology_obj.n_variables, dtype=np.float64)
        for i in range(topology_obj.n_services):
            naive_x[i * n_nodes] = 1.0
        naive_cost = decompose_cost(topology_obj, naive_x)
        if naive_cost["total"] > 0:
            reduction_pct = 100.0 * (naive_cost["total"] - cost_components["total"]) / naive_cost["total"]
        else:
            reduction_pct = 0.0

        total_cost = cost_components["total"]

        return OptimizeResponse(  # type: ignore[name-defined]
            cluster_id=request.cluster_id,
            solver_name=request.algorithm.upper(),
            solve_time_s=round(elapsed, 4),
            latency_reduction_pct=round(reduction_pct, 2),
            total_cost=total_cost,
            latency_cost=cost_components["latency"],
            resource_cost=cost_components["resource"],
            affinity_cost=cost_components["affinity"],
            penalty_cost=cost_components["penalty"],
            is_feasible=abs(cost_components["penalty"]) < 1e-6,
            assignments=assignments,
        )

    return app


# ---------------------------------------------------------------------------
# Stand-alone runner
# ---------------------------------------------------------------------------


def run_dashboard(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True) -> None:
    """Launch the dashboard server (called from the CLI)."""
    if not _FASTAPI_AVAILABLE:
        print(
            "ERROR: FastAPI and uvicorn are required.\n"
            "Install: pip install fastapi uvicorn",
            file=sys.stderr,
        )
        sys.exit(1)

    if open_browser:
        import threading
        import webbrowser

        def _open() -> None:
            time.sleep(1.2)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_dashboard()
