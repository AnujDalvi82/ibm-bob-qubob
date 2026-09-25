# Manifest Ingestion & QUBO Topology Modeling — Step 3 Plan

## Top-Level Overview

**Goal:** Implement the complete data pipeline from raw Kubernetes / Docker Compose manifests through
a weighted service-dependency graph to a callable `ObjectiveFn` that the existing QIEA, GA, and Greedy
solvers can consume unchanged.

**Scope:**
- `bob_optimizer/parser/kubernetes.py` — K8s manifest ingestion → `ClusterTopology`
- `bob_optimizer/parser/compose.py` — Docker Compose ingestion → `ClusterTopology`
- `bob_optimizer/model/graph.py` — NetworkX directed weighted graph builder
- `bob_optimizer/model/qubo.py` — Multi-objective QUBO cost function that returns an `ObjectiveFn`
- `tests/fixtures/synthetic_generator.py` — Synthetic topology generator (Small / Medium / Enterprise)
- `tests/test_parsers_and_cost.py` — Full unit + integration test suite for the new layer

**Out of scope for this step:** SQA solver (`sqa.py`), diff generator, CLI wiring.

**Guiding constraints (from codebase conventions):**
- All domain types are Pydantic v2 frozen models from `bob_optimizer/model/domain.py`; parsers must
  produce valid `ClusterTopology` objects and must not introduce new mutable domain types.
- `ObjectiveFn = Callable[[np.ndarray], float]` (from `bob_optimizer/solvers/base.py`); the QUBO
  cost engine must conform to this exact type alias.
- Variable encoding: `x_{i,j} = x[i * n_nodes + j]`; matches `ClusterTopology.variable_index()`.
- Python 3.13+, strict mypy, Pydantic v2, NumPy ≥ 2.0, NetworkX ≥ 3.3, PyYAML ≥ 6.0.
- All latency fallback values align with `NodeCapacity.latency_to()`: 0.0 ms intra-node,
  0.5 ms intra-zone (unmeasured), 5.0 ms cross-zone (unmeasured). The plan adds explicit
  constants for the three benchmark RTT tiers: 0.1 ms loopback, 1.5 ms intra-zone, 8.0 ms
  cross-zone (overrides defaults when building synthetic topologies).

---

## Sub-Task 1 — Kubernetes Manifest Parser (`kubernetes.py`)

**Intent:** Parse one or more Kubernetes YAML documents (Deployment, StatefulSet, Service) and
produce a fully-validated `ClusterTopology`. This is the real-world ingestion path for k8s clusters.

**Expected Outcomes:**
- `parse_manifests(paths: list[Path]) -> ClusterTopology` is importable and callable.
- `Deployment` and `StatefulSet` documents yield one `Service` per workload (multi-container pods
  are reduced to summed resource requests; individual container names are stored in annotations).
- Resource requests/limits are parsed from `spec.containers[*].resources.requests`; missing
  requests fall back to documented defaults (100 m CPU, 128 MiB RAM).
- Port mappings are stored as annotations (`ports: "80,443"`); not modeled as first-class fields
  because the domain model does not have a ports field — this preserves the domain contract.
- `podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution` rules populate
  `Service.anti_affinity_services`.
- `nodeAffinity` required and preferred label selectors populate `Service.required_node_labels`
  and `Service.preferred_node_labels`.
- Service-to-service dependencies are inferred from `env` vars that look like in-cluster URLs
  (`http://<service-name>` or `<service-name>.<namespace>.svc.cluster.local`).
- K8s `Service` documents are used only for port-to-protocol mapping and are not turned into
  `Service` domain objects.

**Todo List:**
1. Define module-level constants: `_DEFAULT_CPU_M = 100`, `_DEFAULT_MEM_MIB = 128`.
2. Implement `_parse_resource_request(container: dict) -> ResourceRequest` — extract
   `requests.cpu` (handle `"250m"`, `"0.5"`, `"1"` forms) and `requests.memory`
   (handle `"128Mi"`, `"1Gi"`, `"512000000"` forms); fall back to defaults for missing keys.
3. Implement `_parse_affinity(spec: dict) -> tuple[dict, dict, list[str]]` — returns
   `(required_node_labels, preferred_node_labels, anti_affinity_services)`.
4. Implement `_infer_dependencies(containers: list[dict], known_service_names: set[str]) -> list[tuple[str, str]]` —
   scan all `env[*].value` strings for in-cluster URL patterns; return `(source, target)` pairs.
5. Implement `_workload_to_service(doc: dict) -> Service` — handles both Deployment and StatefulSet;
   sums resources across containers; sets `replicas` from `spec.replicas` (default 1).
6. Implement `parse_manifests(paths: list[Path], *, cluster_name: str = "k8s-cluster") -> ClusterTopology` —
   multi-pass: first pass collects all service names; second pass resolves dependency targets
   so forward references are handled correctly.
7. Handle edge case: multi-container pods — sum CPU and memory across all containers in the pod,
   store `"containers: <names>"` in `Service.annotations`.
8. Handle edge case: missing `spec.replicas` — default to 1 via `_DEFAULT_REPLICAS = 1`.
9. Handle edge case: `StatefulSet` with heterogeneous replica counts — honour the per-workload
   `spec.replicas` value; do not average.
10. Raise `ValueError` with a clear message if a parsed document kind is not one of
    `Deployment | StatefulSet | Service | ConfigMap`; unknown kinds are skipped with a warning.

**Relevant Context:**
- [`bob_optimizer/model/domain.py`](bob_optimizer/model/domain.py) — `Service`, `ResourceRequest`,
  `ServiceDependency`, `ClusterTopology`, `NodeCapacity`; note that `NodeCapacity` nodes are NOT
  populated by the parser (cluster nodes come from kubectl/synthetic generator).
- [`bob_optimizer/parser/kubernetes.py`](bob_optimizer/parser/kubernetes.py) — file to implement.
- PyYAML `yaml.safe_load_all()` for multi-document YAML files.

**Status:** [ ] pending

---

## Sub-Task 2 — Docker Compose Parser (`compose.py`)

**Intent:** Parse a `docker-compose.yml` (v2 or v3 schema) and produce a `ClusterTopology`.
This is the local-dev ingestion path and must handle the structural differences from k8s
(no namespaces, `depends_on` instead of env-URL inference, Compose resource syntax).

**Expected Outcomes:**
- `parse_compose(path: Path, *, cluster_name: str = "compose-cluster") -> ClusterTopology`
  is importable and callable.
- Each top-level service key becomes one `Service`; image, replicas (`deploy.replicas`),
  CPU (`deploy.resources.limits.cpus` as float × 1000 m; fallback to requests), and
  memory (`deploy.resources.limits.memory`, handling `"128m"`, `"1g"` suffixes) are extracted.
- `depends_on` (list or dict form) produces `ServiceDependency` edges with
  `calls_per_second = 1.0` (no RPS data in Compose; treated as unit-weight edges).
- Environment variable URL scanning (same regex as kubernetes.py) supplements `depends_on`
  to catch reverse proxy / sidecar patterns not captured by the dependency graph.
- No duplicate edges: if `depends_on` and env-URL scanning both detect the same
  `(source, target)` pair, deduplicate to a single `ServiceDependency`.
- `networks` key is parsed but only used to infer additional latency groupings via
  `NodeCapacity.zone`; network name is stored in `Service.labels["compose.network"]`.

**Todo List:**
1. Define `_DEFAULT_CPU_M = 100`, `_DEFAULT_MEM_MIB = 128` (same constants as kubernetes.py;
   import from a shared `_defaults.py` or duplicate — keep simple, no premature abstraction).
2. Implement `_parse_compose_cpu(value: str | float | None) -> int` — convert Compose CPU fraction
   (`"0.5"`, `0.25`) to millicores; fall back to default.
3. Implement `_parse_compose_memory(value: str | None) -> int` — convert `"128m"`, `"1g"`, `"512000000"`
   to MiB; fall back to default.
4. Implement `_extract_env_url_targets(env: list | dict, known: set[str]) -> set[str]` —
   shared regex: `r"https?://([a-z0-9\-]+)"` and `r"([a-z0-9\-]+)\.[a-z]+\.svc\.cluster\.local"`.
5. Implement `parse_compose(path: Path, *, cluster_name: str = "compose-cluster") -> ClusterTopology`.
   Two-pass: collect names first, then resolve dependencies.
6. Handle edge case: `depends_on` as a dict (v3 health-check form) vs. a list (v2 form).
7. Handle edge case: service with no `image` key (build-only service) — set
   `image = "build:<service-name>"`.
8. Handle edge case: `replicas` absent from `deploy` block — default to 1.

**Relevant Context:**
- [`bob_optimizer/parser/compose.py`](bob_optimizer/parser/compose.py) — file to implement.
- Same domain types as Sub-Task 1; `NodeCapacity` nodes are NOT populated here.

**Status:** [ ] pending

---

## Sub-Task 3 — Graph Builder (`graph_builder.py` / `graph.py`)

**Intent:** Build a `networkx.DiGraph` from a `ClusterTopology` where nodes represent service
instances and weighted edges represent RPC/REST call volumes. Also expose a helper that builds
the inter-node latency matrix as a NumPy array — this matrix is the hot path inside the QUBO
cost function.

**Expected Outcomes:**
- `build_service_graph(topology: ClusterTopology) -> nx.DiGraph` produces a directed graph where:
  - Each node key is a service name; node attributes carry `replicas`, `cpu_millicores`, `memory_mib`.
  - Each edge `(u, v)` carries `weight = calls_per_second`, `protocol`.
- `build_latency_matrix(topology: ClusterTopology) -> np.ndarray` returns a float64 array of
  shape `(n_nodes, n_nodes)` where `M[j, j'] = node_j.latency_to(node_j')`. Diagonal is 0.0.
- `build_resource_demand_vector(topology: ClusterTopology) -> np.ndarray` returns a float64 array
  of shape `(n_services,)` where element `i` is the total CPU millicores of service `i`
  (used by GreedyFFDSolver and the resource term).
- `build_capacity_vector(topology: ClusterTopology) -> np.ndarray` returns a float64 array of
  shape `(n_nodes,)` where element `j` is the allocatable CPU millicores of node `j`.

**Todo List:**
1. Import `networkx as nx` and the domain types; add module-level type alias
   `ServiceGraph = nx.DiGraph`.
2. Implement `build_service_graph(topology: ClusterTopology) -> nx.DiGraph`.
3. Implement `build_latency_matrix(topology: ClusterTopology) -> np.ndarray` — nested loop over
   `topology.nodes`; use `NodeCapacity.latency_to()` for each pair.
4. Implement `build_resource_demand_vector(topology: ClusterTopology) -> np.ndarray`.
5. Implement `build_capacity_vector(topology: ClusterTopology) -> np.ndarray`.
6. No caching or memoisation in this sub-task — caller owns lifecycle.

**Relevant Context:**
- [`bob_optimizer/model/graph.py`](bob_optimizer/model/graph.py) — file to implement.
- [`bob_optimizer/model/domain.py`](bob_optimizer/model/domain.py) — `ClusterTopology`,
  `NodeCapacity.latency_to()`.
- `networkx>=3.3` is already a declared dependency.

**Status:** [ ] pending

---

## Sub-Task 4 — QUBO Cost Engine (`qubo.py`)

**Intent:** Implement the multi-objective QUBO cost function as a factory that accepts a
`ClusterTopology` and returns a pure `ObjectiveFn`. This is the critical bridge between the
topology model and all three existing solvers — it must conform exactly to
`ObjectiveFn = Callable[[np.ndarray], float]`.

**Expected Outcomes:**
- `build_objective(topology: ClusterTopology) -> ObjectiveFn` returns a closure that:
  - Accepts `x: np.ndarray` of shape `(n_services * n_nodes,)` dtype int8.
  - Decodes `x` using `x_{i,j} = x[i * n_nodes + j]`.
  - Computes and returns `H_total` as a Python `float`.
- `decompose_cost(topology: ClusterTopology, x: np.ndarray) -> dict[str, float]` returns
  the individual cost components: `{"latency": ..., "resource": ..., "affinity": ..., "penalty": ...}`.
  Used by the `explain` CLI command and test assertions.
- The four cost terms match the spec §5.2 exactly:

  **H_latency** = Σ_{(i,k)∈E} w_ik · Σ_j Σ_{j'≠j} x_{i,j} · x_{k,j'} · d(j, j')

  **H_resource** = Σ_j ReLU(Σ_i x_{i,j} · r_i − C_j)²

  **H_affinity** = Σ_{(i,k) anti-affinity pairs} Σ_j x_{i,j} · x_{k,j}  (penalty when both on same node)

  **H_penalty** = Σ_i (Σ_j x_{i,j} − 1)²

  **H_total** = λ_lat · H_latency + λ_res · H_resource + λ_aff · H_affinity + λ_pen · H_penalty

- Pre-computes the latency matrix and dependency adjacency once at construction time (not on
  every call); the returned closure captures these pre-computed structures.
- Performance: no Python-level loops over `n_variables` inside the hot path — use NumPy
  reshape + matrix operations.

**Todo List:**
1. Add `from bob_optimizer.model.graph import build_latency_matrix` import.
2. Implement `_latency_cost(x_mat: np.ndarray, dep_matrix: np.ndarray, latency_mat: np.ndarray) -> float` —
   `x_mat` shape `(n_services, n_nodes)` is the 2-D view of x; `dep_matrix[i, k] = w_ik`;
   this is a pure NumPy triple product.
3. Implement `_resource_cost(x_mat: np.ndarray, demands: np.ndarray, capacities: np.ndarray) -> float` —
   node load = `x_mat.T @ demands`; overflow = `np.maximum(node_load - capacities, 0)`;
   return `float(np.sum(overflow ** 2))`.
4. Implement `_affinity_cost(x_mat: np.ndarray, anti_pairs: list[tuple[int, int]]) -> float` —
   iterate over pre-computed `anti_pairs = [(i, k), ...]`; for each pair compute
   `np.dot(x_mat[i], x_mat[k])`; sum. Acceptable to use a loop here because the number
   of anti-affinity pairs is small (O(services), not O(variables²)).
5. Implement `_penalty_cost(x_mat: np.ndarray) -> float` — `row_sums = x_mat.sum(axis=1)`;
   return `float(np.sum((row_sums - 1) ** 2))`.
6. Implement `build_objective(topology: ClusterTopology) -> ObjectiveFn` — the factory.
   Pre-compute: latency matrix, dependency weight matrix, demand vector, capacity vector,
   anti-affinity pairs list. Return a closure over these.
7. Implement `decompose_cost(topology: ClusterTopology, x: np.ndarray) -> dict[str, float]`.
8. Ensure the closure has `__name__ = "qubo_objective"` for debuggability.

**Relevant Context:**
- [`bob_optimizer/model/qubo.py`](bob_optimizer/model/qubo.py) — file to implement.
- [`bob_optimizer/solvers/base.py`](bob_optimizer/solvers/base.py) — `ObjectiveFn` type alias.
- [`bob_optimizer/model/domain.py`](bob_optimizer/model/domain.py) — `ClusterTopology` with
  `lambda_latency`, `lambda_resource`, `lambda_affinity`, `lambda_penalty`.
- PROJECT_SPEC.md §5.2 — authoritative mathematical spec.

**Status:** [ ] pending

---

## Sub-Task 5 — Synthetic Topology Generator (`tests/fixtures/synthetic_generator.py`)

**Intent:** Provide deterministic, parameterised factory functions that produce Small (8 services /
3 nodes), Medium (24 services / 9 nodes), and Enterprise (64 services / 15 nodes) `ClusterTopology`
instances spanning realistic multi-zone cluster configurations. Used by benchmark tests and the
`qubob benchmark` CLI command.

**Expected Outcomes:**
- Three public functions: `make_small_topology(seed: int = 0) -> ClusterTopology`,
  `make_medium_topology(seed: int = 0) -> ClusterTopology`,
  `make_enterprise_topology(seed: int = 0) -> ClusterTopology`.
- Nodes are distributed across three failure zones (`us-east-1a`, `us-east-1b`, `us-east-1c`).
- Inter-node latency is set with the three-tier RTT model:
  - Same node: 0.1 ms (loopback)
  - Same zone, different node: 1.5 ms (intra-zone)
  - Different zones: 8.0 ms (cross-zone)
- Services are randomly named (`svc-0`, `svc-1`, …) with randomised CPU (50–2000 m) and
  memory (64–4096 MiB) drawn from the seeded RNG.
- Dependencies form a sparse directed graph: each service depends on 1–3 randomly-chosen
  other services, with `calls_per_second` drawn from a log-uniform [0.1, 1000.0] range.
  Self-loops are excluded; the graph is validated before constructing `ClusterTopology`.
- `lambda_*` hyperparameters use `ClusterTopology` defaults (no override needed).
- Each function is deterministic: same `seed` always returns the same topology.

**Todo List:**
1. Create `tests/fixtures/` directory with `__init__.py`.
2. Implement `_make_nodes(n_nodes: int, rng: np.random.Generator) -> list[NodeCapacity]` —
   distributes nodes evenly across three zones; builds the full `latency_ms` dict for each
   node using the three-tier RTT constants (0.1 ms / 1.5 ms / 8.0 ms).
3. Implement `_make_services(n_services: int, rng: np.random.Generator) -> list[Service]` —
   random CPU, memory; names `svc-{i}`.
4. Implement `_make_dependencies(services: list[Service], rng: np.random.Generator, avg_degree: int = 2) -> list[ServiceDependency]` —
   sparse random dependency graph with no self-loops and no duplicate edges.
5. Implement `make_small_topology`, `make_medium_topology`, `make_enterprise_topology` as thin
   wrappers around the above helpers with hardcoded size parameters:
   - Small: 8 services, 3 nodes
   - Medium: 24 services, 9 nodes
   - Enterprise: 64 services, 15 nodes
6. Add module-level constants `_LOOPBACK_MS = 0.1`, `_INTRAZONE_MS = 1.5`, `_CROSSZONE_MS = 8.0`.

**Relevant Context:**
- [`bob_optimizer/model/domain.py`](bob_optimizer/model/domain.py) — all domain model classes.
- [`tests/test_solvers.py`](tests/test_solvers.py) — fixture patterns (`_make_rng`, helper
  functions at module level, class-based test groups).

**Status:** [ ] pending

---

## Sub-Task 6 — Test Suite (`tests/test_parsers_and_cost.py`)

**Intent:** Provide comprehensive unit and integration tests that give ≥ 95% line coverage
for Sub-Tasks 1–5 and guard against regressions. Follows the existing test conventions
(class-based grouping, `_make_*` helper functions, `np.testing.assert_allclose`).

**Expected Outcomes:**
- All tests pass with `pytest` on a clean checkout.
- Each parser sub-test uses inline YAML/dict fixtures (no external files) to avoid test
  infrastructure dependencies.
- QUBO cost function tests verify: H_penalty = 0 for one-hot inputs; H_penalty > 0 for
  multi-assigned inputs; H_latency monotonically increases when services are spread across
  zones; H_resource spikes when capacity is exceeded.
- Graph builder tests verify shape, dtype, and symmetry of the latency matrix.
- Synthetic generator tests verify determinism (same seed → same topology) and size contracts.
- Integration test wires a synthetic topology through `build_objective` and passes the
  resulting `ObjectiveFn` to all three existing solvers (`QIEASolver`, `ClassicalGASolver`,
  `GreedyFFDSolver`); asserts `SolverResult.best_cost < initial_random_cost` (convergence
  sanity).

**Todo List:**

### Parser Tests (`TestKubernetesParser`)
1. `test_deployment_basic` — minimal Deployment YAML → correct service name, image, replicas.
2. `test_resource_defaults` — missing `resources.requests` → CPU=100 m, MEM=128 MiB.
3. `test_cpu_parsing_forms` — `"250m"`, `"0.5"`, `"1"` all parse to correct millicores.
4. `test_memory_parsing_forms` — `"128Mi"`, `"1Gi"`, `"512000000"` parse to correct MiB.
5. `test_multi_container_pod_sum` — two containers with 250 m / 256 MiB each → 500 m / 512 MiB.
6. `test_statefulset_replicas` — `spec.replicas: 3` → `Service.replicas = 3`.
7. `test_anti_affinity_rule` — `podAntiAffinity` required rule → `anti_affinity_services` populated.
8. `test_node_affinity_required` — `nodeAffinity` required rule → `required_node_labels`.
9. `test_env_url_dependency_inference` — env var `http://payment-service` → dependency edge detected.
10. `test_unknown_kind_skipped` — CronJob document in file → skipped without error.

### Parser Tests (`TestComposeParser`)
11. `test_compose_service_basic` — service name, image, replicas from `deploy.replicas`.
12. `test_compose_resource_defaults` — no `deploy` block → CPU=100 m, MEM=128 MiB.
13. `test_depends_on_list` — `depends_on: [db]` → `ServiceDependency(source, "db")`.
14. `test_depends_on_dict` — `depends_on: {db: {condition: service_healthy}}` → same edge.
15. `test_no_duplicate_edges` — both `depends_on` and env URL point to same service → one edge.
16. `test_build_only_service` — no `image` key → `image = "build:<name>"`.

### Graph Builder Tests (`TestGraphBuilder`)
17. `test_service_graph_nodes` — graph has one node per service.
18. `test_service_graph_edges` — edges have `weight` and `protocol` attributes.
19. `test_latency_matrix_shape` — shape is `(n_nodes, n_nodes)`.
20. `test_latency_matrix_diagonal` — diagonal is all zeros.
21. `test_latency_matrix_symmetry` — `M[j, j'] == M[j', j]` for all pairs (both use `latency_to()`).
22. `test_demand_and_capacity_vectors` — correct shapes and non-negative values.

### QUBO Cost Engine Tests (`TestQUBOCost`)
23. `test_feasible_one_hot_zero_penalty` — valid one-hot assignment → `H_penalty = 0`.
24. `test_infeasible_double_assign_penalty` — service assigned to two nodes → `H_penalty > 0`.
25. `test_unassigned_penalty` — service assigned to no node → `H_penalty > 0`.
26. `test_collocated_lower_latency` — all services on same node < all services spread cross-zone.
27. `test_capacity_overflow_resource_cost` — node with 0 capacity and one service → `H_resource > 0`.
28. `test_no_overflow_resource_cost` — services fit within capacity → `H_resource = 0`.
29. `test_anti_affinity_violation` — two services with anti-affinity on same node → `H_affinity > 0`.
30. `test_anti_affinity_satisfied` — same pair on different nodes → `H_affinity = 0`.
31. `test_objective_fn_is_callable` — `build_objective(topology)` returns a callable.
32. `test_decompose_cost_keys` — `decompose_cost` returns dict with all four component keys.
33. `test_total_is_sum_of_components` — `H_total == Σ λ_i · H_i`.

### Synthetic Generator Tests (`TestSyntheticGenerator`)
34. `test_small_topology_sizes` — 8 services, 3 nodes.
35. `test_medium_topology_sizes` — 24 services, 9 nodes.
36. `test_enterprise_topology_sizes` — 64 services, 15 nodes.
37. `test_determinism` — `make_small_topology(0) == make_small_topology(0)` (by field comparison).
38. `test_different_seeds_differ` — `make_small_topology(0) != make_small_topology(1)`.
39. `test_three_zone_distribution` — each zone has at least one node in all three generators.
40. `test_latency_values` — intra-node 0.1 ms, intra-zone 1.5 ms, cross-zone 8.0 ms.

### Integration Tests (`TestEndToEndSolverIntegration`)
41. `test_qiea_solves_small_topology` — QIEA with small topology, 50 generations; cost improves.
42. `test_ga_solves_small_topology` — Classical GA with small topology; cost improves.
43. `test_greedy_solves_small_topology` — GreedyFFD with resource demand/capacity vectors.
44. `test_all_solvers_feasible` — all three produce one-hot valid solutions on medium topology.

**Relevant Context:**
- [`tests/test_solvers.py`](tests/test_solvers.py) — class-based test conventions, `_make_rng`,
  `np.testing.assert_allclose`, `@pytest.mark.parametrize`.
- [`bob_optimizer/solvers/base.py`](bob_optimizer/solvers/base.py) — `SolverResult`.
- [`bob_optimizer/model/domain.py`](bob_optimizer/model/domain.py) — all domain classes.

**Status:** [ ] pending

---

## Data Flow Diagram

```
Kubernetes YAML files ──────► parse_manifests() ─────────────────────┐
                                 (kubernetes.py)                       │
                                                                       ▼
docker-compose.yml ────────► parse_compose() ──────────► ClusterTopology
                                 (compose.py)                  (domain.py)
                                                                       │
                         ┌─────────────────────────────────────────────┤
                         │                                             │
                         ▼                                             ▼
               build_service_graph()                    build_latency_matrix()
                  (graph.py)                          build_resource_demand_vector()
               nx.DiGraph G=(V,E)                     build_capacity_vector()
                                                          (graph.py)
                                                               │
                                                               ▼
                                              build_objective(topology) ─── ObjectiveFn
                                                    (qubo.py)                    │
                                                                                 │
                                     ┌───────────────────────────────────────────┤
                                     ▼                         ▼                 ▼
                               QIEASolver                ClassicalGA        GreedyFFD
                               .solve(fn)                .solve(fn)         .solve(fn)
                                     │                         │                 │
                                     └─────────────────────────┴─────────────────┘
                                                               │
                                                               ▼
                                                        SolverResult
                                                       PlacementPlan
```

---

## Component Contracts Summary

| Component | Input | Output | Key Constraint |
|-----------|-------|--------|----------------|
| `parse_manifests` | `list[Path]` of YAML files | `ClusterTopology` | Nodes tuple is empty; caller provides nodes separately |
| `parse_compose` | `Path` to docker-compose.yml | `ClusterTopology` | Same — no nodes populated |
| `build_service_graph` | `ClusterTopology` | `nx.DiGraph` | One node per service; edge weight = calls_per_second |
| `build_latency_matrix` | `ClusterTopology` | `ndarray (n_nodes, n_nodes)` | Symmetric; diagonal = 0.0 |
| `build_objective` | `ClusterTopology` | `ObjectiveFn` | Pure; no side effects; lower is better |
| `decompose_cost` | `ClusterTopology`, `ndarray` | `dict[str, float]` | Four keys: latency, resource, affinity, penalty |
| `make_*_topology` | `seed: int` | `ClusterTopology` | Deterministic; includes nodes with latency matrix |

---

## File Breakdown

| File | Sub-Task | New / Modify |
|------|----------|--------------|
| `bob_optimizer/parser/kubernetes.py` | 1 | Implement (file exists, empty) |
| `bob_optimizer/parser/compose.py` | 2 | Implement (file exists, empty) |
| `bob_optimizer/model/graph.py` | 3 | Implement (file exists, empty) |
| `bob_optimizer/model/qubo.py` | 4 | Implement (file exists, empty) |
| `tests/fixtures/__init__.py` | 5 | Create new |
| `tests/fixtures/synthetic_generator.py` | 5 | Create new |
| `tests/test_parsers_and_cost.py` | 6 | Create new |
