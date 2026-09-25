# QUBOB: Quantum-Inspired Unconstrained Binary Optimizer for IBM Bob 2.0

> **Hackathon Project** — IBM Bob Hackathon 2025
> **Status:** Active Development — Step 1 Scaffold

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Why Classical Greedy Falls Short](#2-why-classical-greedy-falls-short)
3. [Why Physical QPUs Are Not the Answer (Yet)](#3-why-physical-qpus-are-not-the-answer-yet)
4. [QUBOB Architecture: Quantum-Inspired on Classical CPU](#4-qubob-architecture-quantum-inspired-on-classical-cpu)
5. [Mathematical Formulation](#5-mathematical-formulation)
6. [Algorithm Deep-Dive: QIEA & SQA](#6-algorithm-deep-dive-qiea--sqa)
7. [Bob IDE Integration Pillars](#7-bob-ide-integration-pillars)
8. [Package Layout](#8-package-layout)
9. [Roadmap](#9-roadmap)

---

## 1. Problem Statement

Modern cloud-native applications are composed of tens to hundreds of microservices. Placing these services optimally across Kubernetes clusters, availability
zones, and individual nodes is an **NP-hard combinatorial optimisation problem**.

### Search Space

For **N** services and **S** candidate nodes the number of possible placements is:

```
|Ω| = S^N
```

Even a modest deployment of 20 services across 10 nodes yields 10²⁰ candidate solutions — far beyond exhaustive search.

### Cost of Suboptimal Placement

| Symptom | Root Cause | Typical Impact |
|---|---|---|
| High p99 latency | Cross-zone service calls | +5–10 ms per RPC hop |
| Elevated cloud bills | Inter-zone / egress traffic | $0.01–0.09 / GB depending on provider |
| Cascading failures | Hot-spot resource contention | Unpredictable |
| SLA breaches | Affinity rules ignored | Revenue / compliance risk |

### Goal

Produce **actionable Kubernetes `nodeAffinity` patch diffs** in seconds, running entirely inside the Bob IDE on a developer laptop with near-zero CPU and
memory overhead.

---

## 2. Why Classical Greedy Falls Short

Greedy heuristics (first-fit, best-fit, bin-packing variants) assign services one by one based on locally optimal choices. They are fast but suffer from
critical structural weaknesses:

- **Local minima traps** — once a service is placed it is rarely reconsidered; early bad decisions propagate.
- **Ignoring interaction effects** — cross-service latency penalties are non-local; they only emerge when the full assignment is evaluated.
- **No diversity** — a single deterministic pass produces one solution with no exploration of the landscape.
- **Poor on plateaus** — many placement configurations share identical partial costs, causing greedy to make arbitrary tie-breaks that degrade global quality.

Simulated Annealing (SA) improves on greedy but is a purely classical single-trajectory method. It escapes local minima stochastically but can require
millions of iterations to converge on large instances.

---

## 3. Why Physical QPUs Are Not the Answer (Yet)

Real quantum hardware (IBM Quantum, IonQ, Rigetti) is a compelling long-term direction but is impractical for this use case today:

| Constraint | Reality |
|---|---|
| Qubit count | Current NISQ devices offer 100–400 noisy qubits — not enough for non-trivial service graphs after encoding overhead |
| Gate error rates | 0.1–1% per two-qubit gate; deep circuits decohere before finishing |
| Queue time | Jobs can wait hours on shared hardware |
| Cost | Cloud QPU access is expensive and consumes Bobcoins at scale |
| Connectivity | Requires network round-trips to external quantum cloud; unusable offline |

**QUBOB's insight:** the mathematical structure of quantum algorithms (superposition, interference, tunnelling) can be **simulated on classical CPU** with
polynomial overhead for problem sizes relevant to Kubernetes clusters (N ≤ 200 services, S ≤ 50 nodes). This is the field of **Quantum-Inspired
Evolutionary Algorithms (QIEA)**.

---

## 4. QUBOB Architecture: Quantum-Inspired on Classical CPU

```
┌─────────────────────────────────────────────────────────────┐
│                        Bob IDE                              │
│                                                             │
│  ┌──────────┐   YAML/JSON   ┌──────────────────────────┐   │
│  │ k8s /    │ ──────────▶   │   Parser & Ingestion      │   │
│  │ compose  │               │ (bob_optimizer/parser/)   │   │
│  └──────────┘               └────────────┬─────────────┘   │
│                                          │ ClusterTopology  │
│                              ┌───────────▼─────────────┐   │
│                              │  Graph Model & QUBO      │   │
│                              │  Cost Engine             │   │
│                              │ (bob_optimizer/model/)   │   │
│                              └───────────┬─────────────┘   │
│                                          │ QUBO matrix H    │
│                         ┌────────────────▼──────────────┐  │
│                         │         Solver Layer           │  │
│                         │  ┌──────────┐  ┌───────────┐  │  │
│                         │  │  QIEA    │  │   SQA     │  │  │
│                         │  │ (Q-bits) │  │(Trotter)  │  │  │
│                         │  └──────────┘  └───────────┘  │  │
│                         │  ┌──────────────────────────┐  │  │
│                         │  │   Classical Baseline      │  │  │
│                         │  │ (Greedy / SA reference)   │  │  │
│                         │  └──────────────────────────┘  │  │
│                         └───────────────┬────────────────┘  │
│                                         │ PlacementPlan      │
│                          ┌──────────────▼──────────────┐    │
│                          │  Diff Generator              │    │
│                          │  (bob_optimizer/diff/)       │    │
│                          └──────────────┬───────────────┘   │
│                                         │ nodeAffinity patch │
│                          ┌──────────────▼──────────────┐    │
│                          │  CLI / Bob Tool Interface    │    │
│                          │  (bob_optimizer/cli/)        │    │
│                          └─────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### Component Summary

| Layer | Module | Responsibility |
|---|---|---|
| **Ingestion** | `parser/` | Parse Kubernetes Deployments, Services, ConfigMaps; Docker Compose files; extract resource requests, labels, anti-affinity hints |
| **Model** | `model/` | Build weighted service dependency graph; assemble QUBO cost matrix from latency, resource, and constraint terms |
| **Solvers** | `solvers/` | QIEA (Q-chromosome population), SQA (Suzuki–Trotter decomposition), classical SA/Greedy baselines |
| **Diff** | `diff/` | Translate `PlacementPlan` → Kubernetes `nodeAffinity` patch objects; produce unified git-style diffs |
| **CLI** | `cli/` | `qubob optimise`, `qubob explain`, `qubob benchmark` commands; Bob tool registration |

---

## 5. Mathematical Formulation

### 5.1 Decision Variables

Encode placement as a binary matrix **x**:

```
x_{i,j} ∈ {0, 1}    where i = service index, j = node index
```

Constraint: each service assigned to exactly one node:

```
∑_j x_{i,j} = 1   ∀ i
```

### 5.2 QUBO Objective (H_total)

```
H_total = λ_lat · H_latency  +  λ_res · H_resource  +  λ_aff · H_affinity  +  λ_pen · H_penalty
```

#### Latency Term

```
H_latency = ∑_{(i,k) ∈ E} w_{ik} · ∑_j ∑_{j' ≠ j} x_{i,j} · x_{k,j'} · d(j, j')
```

Where `d(j, j')` is the measured inter-node latency (ms) and `w_{ik}` is the call-frequency weight on edge (i, k).

#### Resource Term

```
H_resource = ∑_j  ReLU( ∑_i x_{i,j} · r_i  -  C_j )²
```

Penalises nodes whose assigned resource demand exceeds capacity `C_j`.

#### Affinity Term

Encodes hard/soft `podAffinity` and `podAntiAffinity` rules as quadratic penalty/reward terms.

#### One-hot Penalty

```
H_penalty = ∑_i ( ∑_j x_{i,j} - 1 )²
```

### 5.3 QUBO Matrix

All terms collapse into the canonical QUBO form:

```
H = xᵀ Q x
```

where **Q** is a symmetric matrix of dimension **(N·S) × (N·S)**. Minimising H gives the optimal placement.

---

## 6. Algorithm Deep-Dive: QIEA & SQA

### 6.1 Quantum-Inspired Evolutionary Algorithm (QIEA)

Each individual in the population is a **Q-chromosome** — a vector of Q-bits:

```
|q⟩ = [α₁  α₂  … α_{N·S}]
        [β₁  β₂  … β_{N·S}]

where |αᵢ|² + |βᵢ|² = 1
```

**|αᵢ|²** = probability of bit being 0; **|βᵢ|²** = probability of bit being 1.

**Evolution loop (one generation):**

```
1. Observation:   collapse each Q-bit → binary solution x by sampling
2. Evaluation:    compute H(x) = xᵀ Q x
3. Update:        rotate Q-bits toward best observed solution using gate:

   U(Δθᵢ) = [ cos(Δθᵢ)  -sin(Δθᵢ) ]
              [ sin(Δθᵢ)   cos(Δθᵢ) ]

   Δθᵢ is determined by a lookup table based on (αᵢ, βᵢ, best_bit)
4. Repair:        enforce one-hot constraints via greedy repair
5. Migrate:       exchange elite individuals across sub-populations
```

**Key advantages over classical EA:**
- Population diversity maintained via quantum superposition (no premature convergence)
- Implicit parallelism: each Q-bit simultaneously encodes multiple solution candidates
- Fast convergence: typically 50–200 generations for N=50 services

### 6.2 Simulated Quantum Annealing (SQA)

SQA maps the QUBO to a (P+1)-dimensional Ising model using the **Suzuki–Trotter decomposition**:

```
H_SQA = -∑_{τ=1}^{P} [ ∑_{(i,j)} J_{ij} σᵢ^τ σⱼ^τ  +  Γ_τ · ∑_i σᵢ^τ σᵢ^{τ+1} ]
```

- P = number of Trotter slices (replicas)
- Γ_τ = transverse field strength, annealed from Γ_max → 0
- Coupling between replicas simulates quantum tunnelling through energy barriers

SQA consistently outperforms classical SA on QUBO instances with rugged landscapes (many local minima separated by barriers).

### 6.3 Algorithm Comparison

| Property | Greedy | Classical SA | QIEA | SQA |
|---|---|---|---|---|
| Guarantees global optimum | ✗ | ✗ | ✗ | ✗ |
| Escapes local minima | ✗ | Probabilistic | ✓ (population) | ✓ (tunnelling) |
| Solution diversity | None | None | High | Medium |
| Convergence speed (N=50) | < 1 s | ~10 s | ~2 s | ~5 s |
| Sensitivity to λ tuning | Low | Medium | Low | Medium |
| Parallelisable | ✗ | ✗ | ✓ | ✓ |

---

## 7. Bob IDE Integration Pillars

### Pillar 1 — Bob Tool Registration

`qubob` registers itself as a native Bob tool so that users can invoke it via the Bob chat interface:

```
@bob optimise my k8s manifests for minimum latency
```

Bob recognises the intent, calls `qubob optimise --manifest ./k8s/ --solver qiea`, and streams the resulting diff back into the chat.

### Pillar 2 — In-Editor Diff Preview

The diff generator produces Kubernetes-native `nodeAffinity` patches that Bob renders as a side-by-side diff directly in the IDE editor pane, allowing
one-click application.

### Pillar 3 — Explain Mode

```
@bob explain why service checkout was placed on node-3
```

QUBOB traces the cost components (latency, resource, affinity) for the chosen placement and returns a human-readable breakdown via `qubob explain`.

### Pillar 4 — Benchmark Mode

```
qubob benchmark --instances 10 --sizes 10,20,50 --solvers qiea,sqa,greedy
```

Generates comparative plots of solution quality vs. wall-clock time, exported as Rich tables and optional JSON for CI integration.

### Pillar 5 — Zero External Dependencies at Runtime

All solvers run on pure Python + NumPy. No quantum cloud account, no Docker daemon, no GPU required. Works fully offline inside Bob IDE.

---

## 8. Package Layout

```
ibm-bob-hack-online/
├── PROJECT_SPEC.md
├── pyproject.toml
├── bob_optimizer/
│   ├── __init__.py
│   ├── parser/
│   │   ├── __init__.py
│   │   ├── kubernetes.py       # Deployment / Service / ConfigMap ingestion
│   │   └── compose.py          # Docker Compose v3 ingestion
│   ├── model/
│   │   ├── __init__.py
│   │   ├── domain.py           # Pydantic domain models
│   │   ├── graph.py            # NetworkX service dependency graph
│   │   └── qubo.py             # QUBO cost matrix assembler
│   ├── solvers/
│   │   ├── __init__.py
│   │   ├── base.py             # Abstract Solver protocol
│   │   ├── qiea.py             # Quantum-Inspired Evolutionary Algorithm
│   │   ├── sqa.py              # Simulated Quantum Annealing
│   │   └── classical.py        # Greedy + SA baselines
│   ├── diff/
│   │   ├── __init__.py
│   │   ├── patch.py            # nodeAffinity patch builder
│   │   └── unified.py          # Unified git-style diff renderer
│   └── cli/
│       ├── __init__.py
│       └── main.py             # Click CLI entrypoint
└── tests/
    ├── __init__.py
    ├── test_domain.py
    ├── test_qubo.py
    ├── test_qiea.py
    └── test_diff.py
```

---

## 9. Roadmap

| Step | Milestone | Status |
|---|---|---|
| 1 | Project scaffold, domain models, pyproject.toml | ✅ Complete |
| 2 | Kubernetes & Compose parser implementation | 🔲 Pending |
| 3 | QUBO cost matrix assembler | 🔲 Pending |
| 4 | QIEA solver (core Q-chromosome loop) | 🔲 Pending |
| 5 | SQA solver (Trotter decomposition) | 🔲 Pending |
| 6 | Classical SA / Greedy baselines | 🔲 Pending |
| 7 | nodeAffinity diff generator | 🔲 Pending |
| 8 | CLI + Bob tool registration | 🔲 Pending |
| 9 | Benchmark suite & Rich output | 🔲 Pending |
| 10 | End-to-end integration tests | 🔲 Pending |

---

*QUBOB — Quantum-Inspired Placement Optimisation, running entirely inside IBM Bob IDE.*
