---
name: qubob
description: Quantum-inspired microservices architecture and topology optimizer for Kubernetes and Docker Compose.
triggers:
  - optimize placement
  - service placement
  - kubernetes affinity
  - microservice topology
  - qubo optimizer
  - node affinity patch
  - reduce latency
  - placement plan
  - qubob
  - docker compose placement
  - bob-opt
prerequisites:
  - Python 3.13+ environment with qubob installed (`pip install -e .` or `uv sync`)
  - kubectl access (optional — for live cluster node discovery)
  - Kubernetes YAML manifests OR a docker-compose.yml in the workspace
tools:
  - name: bob-opt
    description: The primary QUBOB CLI tool. Run `bob-opt --help` to see all sub-commands.
    sub_commands:
      - name: analyze
        usage: "bob-opt analyze <path>"
        description: >
          Parses all Kubernetes YAML or Docker Compose files at <path>, builds the
          service dependency graph, prints an RTT latency matrix for the cluster nodes,
          and highlights the top-5 latency bottleneck edges by RPS.
        example: "bob-opt analyze ./manifests/"

      - name: optimize
        usage: "bob-opt optimize <path> [--algorithm qiea|ga|greedy] [--generations N] [--seed S]"
        description: >
          Ingests manifests at <path>, runs the selected quantum-inspired (QIEA),
          classical GA, or Greedy FFD solver, and prints the optimal placement table
          showing which node/zone every service should be scheduled on, plus a full
          QUBO cost breakdown and latency reduction % vs the naive baseline.
          Caches the plan as .qubob_plan.json next to the manifests.
        example: "bob-opt optimize ./manifests/ --algorithm qiea --generations 500"
        options:
          --algorithm: "qiea (default: greedy), ga, or greedy"
          --generations: "Iteration budget for QIEA/GA (default: 200)"
          --seed: "RNG seed for reproducible results (default: 42)"

      - name: diff
        usage: "bob-opt diff <path>"
        description: >
          Reads the cached .qubob_plan.json produced by `optimize`, generates
          nodeAffinity + topologySpreadConstraints patches for Kubernetes manifests
          (or deploy.placement.constraints for Docker Compose), and previews them as
          syntax-highlighted unified diffs in the terminal.  Run `optimize` first.
        example: "bob-opt diff ./manifests/"

      - name: apply
        usage: "bob-opt apply <path> [--no-backup]"
        description: >
          Applies the placement patches to the manifest files in-place.
          Creates a .bak backup of every modified file by default.
          Pass --no-backup to skip backups (use with care).
        example: "bob-opt apply ./manifests/"
---

# QUBOB Skill — Quantum-Inspired Microservice Placement Optimizer

## Overview

QUBOB solves the NP-hard microservice placement problem as a Quadratic Unconstrained Binary
Optimization (QUBO) problem.  Given a set of Kubernetes Deployments or Docker Compose services
and a multi-node cluster, it finds the assignment `x_{i,j}` (service `i` → node `j`) that
minimises the multi-objective cost function:

```
H_total = λ_lat · H_latency
        + λ_res · H_resource
        + λ_aff · H_affinity
        + λ_pen · H_penalty
```

The optimal plan is translated into production-ready Kubernetes `nodeAffinity` +
`topologySpreadConstraints` patches (or Docker Compose `deploy.placement.constraints`),
output as unified diffs that can be applied with `git apply` or `patch`.

---

## Typical Workflow

### 1 · Analyze your manifests

```bash
bob-opt analyze ./k8s/
```

Bob will display:
- A **service dependency graph** (replicas, CPU, memory, in/out call edges)
- A **node RTT latency matrix** (intra-node 0.1 ms, intra-zone 1.5 ms, cross-zone 8.0 ms)
- The **top-5 latency bottleneck edges** by RPS

### 2 · Run the optimizer

```bash
# Fast greedy solver (seconds)
bob-opt optimize ./k8s/ --algorithm greedy

# Quantum-Inspired Evolutionary Algorithm (best quality)
bob-opt optimize ./k8s/ --algorithm qiea --generations 500

# Classical GA baseline
bob-opt optimize ./k8s/ --algorithm ga --generations 300
```

The plan is automatically cached as `.qubob_plan.json` next to the manifests.

### 3 · Preview the diff

```bash
bob-opt diff ./k8s/
```

Outputs a syntax-highlighted unified diff showing the `nodeAffinity` and
`topologySpreadConstraints` changes for each Deployment / StatefulSet.

### 4 · Apply the patches

```bash
bob-opt apply ./k8s/
```

Writes the patched YAML in-place.  Original files are backed up as `*.yml.bak`.

---

## Docker Compose Workflow

The same commands work for Docker Compose files:

```bash
bob-opt analyze ./docker-compose.yml
bob-opt optimize ./docker-compose.yml --algorithm qiea
bob-opt diff ./docker-compose.yml
bob-opt apply ./docker-compose.yml
```

Compose files receive `deploy.placement.constraints` and `deploy.labels` with
`qubob.node` / `qubob.zone` metadata.

---

## Cost Function Weights

Default λ values (tunable via `ClusterTopology` constructor):

| Parameter        | Default | Description                                  |
|------------------|---------|----------------------------------------------|
| `lambda_latency` | 1.0     | Penalises cross-zone RPC calls               |
| `lambda_resource`| 10.0    | Penalises CPU/RAM overflows on nodes         |
| `lambda_affinity`| 5.0     | Penalises anti-affinity violations           |
| `lambda_penalty` | 100.0   | Enforces one-service-per-node assignment     |

---

## Architecture

```
Manifests (k8s YAML / docker-compose.yml)
        │
        ▼
   Parser Layer
   ┌─────────────────────────────────┐
   │  kubernetes.py  /  compose.py   │
   │  → ClusterTopology (Pydantic)   │
   └─────────────────────────────────┘
        │
        ▼
   QUBO Cost Engine  (model/qubo.py)
   ┌─────────────────────────────────┐
   │  build_objective(topology)      │
   │  → ObjectiveFn: ndarray→float   │
   └─────────────────────────────────┘
        │
        ▼
   Solver Layer
   ┌────────────┬────────────┬────────────┐
   │ QIEASolver │ ClassicalGA│GreedySolver│
   │ (qiea.py)  │(classical) │ (greedy.py)│
   └────────────┴────────────┴────────────┘
        │
        ▼
   SolverResult → PlacementPlan (domain.py)
        │
        ▼
   Diff / Patch Layer
   ┌─────────────────────────────────┐
   │  k8s_patch.py / compose_patch   │
   │  → unified diff / in-place write│
   └─────────────────────────────────┘
```

---

## Conversational Workflows

### "Optimise my cluster"

When a user says *"optimise my microservices"*, *"reduce latency between services"*,
*"find the best node for each pod"*, or similar:

1. Ask for the path to their manifests (k8s YAML directory or docker-compose.yml).
2. Run `bob-opt analyze <path>` to show them the current topology.
3. Suggest `bob-opt optimize <path> --algorithm qiea` for best quality, or `--algorithm greedy` for speed.
4. Walk them through reviewing the diff with `bob-opt diff <path>`.
5. Offer to apply with `bob-opt apply <path>`.

### "What algorithm should I use?"

- **greedy** — sub-second; good for CI/CD smoke checks or ≤ 10 services
- **ga** — tens of seconds; balanced quality/speed for ≤ 30 services
- **qiea** — best quality via quantum-inspired amplitude updates; recommended for production

### "Why was service X placed on node Y?"

Point users to the cost breakdown table printed by `bob-opt optimize`.
Each column (latency / resource / affinity / penalty) shows which factor dominated.

### Troubleshooting

| Symptom | Resolution |
|---------|-----------|
| `No cached plan found` | Run `bob-opt optimize <path>` first |
| `No patches to apply` | Services not found in manifests — check names match exactly |
| High penalty cost | Solver didn't find a valid one-hot assignment — try more generations |
| `Path does not exist` | Check the path argument; supports files and directories |
