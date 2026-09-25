# QUBOB × watsonx Integration — Implementation Plan

## Overview

Add production-ready watsonx Orchestrate integration to the QUBOB project. This comprises four
independent deliverables processed one at a time:

1. **`ARCHITECTURE.md`** — Mermaid diagrams, full mathematical formulation, security model.
2. **`watsonx/` package** — OpenAPI 3.0 spec, Orchestrate skill JSON, and a quickstart README.
3. **REST API extension** in `bob_optimizer/dashboard/app.py` — two new action endpoints.
4. **Test suite** — `tests/test_api_watsonx.py` covering the new endpoints and schema.

Non-goals: watsonx.ai model invocation, IAM token issuance infra, live Kubernetes cluster access.

---

## Sub-Task 1 — ARCHITECTURE.md

### Intent
Create a dedicated, self-contained architecture reference at the repository root that captures the
full system design: data flow, class relationships, mathematical foundations, security posture, and
classical-CPU execution guarantees. This becomes the single source of truth for any engineer
integrating QUBOB with watsonx Orchestrate.

### Expected Outcomes
- File `ARCHITECTURE.md` exists at repo root.
- Contains a Mermaid sequence diagram showing the full Slack → watsonx Assistant →
  watsonx Orchestrate → QUBOB REST API → Kubernetes path (numbered steps, all participants).
- Contains a Mermaid class diagram covering: `ClusterTopology`, `Service`, `NodeCapacity`,
  `ServiceDependency`, `ResourceRequest`, `PlacementPlan`, `ServiceAssignment`, `QUBOAPIServer`,
  `BaseSolver`/`QIEASolver`/`ClassicalGASolver`/`GreedyFFDSolver`, and `OrchestrateSkill`.
- Contains the complete mathematical formulation:
  - H_total = λ_lat·H_lat + λ_res·H_res + λ_aff·H_aff + λ_pen·H_pen with each term expanded.
  - Han & Kim rotation angle rule: Δθ_i formula, sign convention, and adaptive-scale extension.
  - Shannon entropy diversity measure: H = −Σ(p·log₂p + q·log₂q), catastrophe threshold δ.
  - QUBO variable encoding: x_{i,j} ↔ x[i·N_nodes + j], one-hot constraint.
- Contains a security section: Bearer JWT flow, IBM Cloud IAM, mTLS for server-to-server,
  cluster_id abstraction (no filesystem paths exposed), API key vault pattern.
- Contains an offline-first / classical-CPU guarantee section explaining that QIEA, GA, and
  Greedy all run on standard CPU with NumPy; no quantum hardware or cloud service required.

### Todo List
- [ ] Create `ARCHITECTURE.md` at repo root.
- [ ] Write "System Overview" section with a brief narrative.
- [ ] Add the Mermaid sequence diagram (Slack → K8s, 18 numbered steps).
- [ ] Add the Mermaid class diagram (all domain models + API + solver hierarchy).
- [ ] Write "Mathematical Formulation" section with H_total expansion and each sub-term.
- [ ] Write "QIEA Internals" sub-section: Han & Kim rotation rule, adaptive scale, Δθ table.
- [ ] Write "Shannon Entropy & Catastrophe" sub-section: diversity formula, σ_x operator.
- [ ] Write "Security & Credential Flow" section.
- [ ] Write "Offline-First Classical CPU Guarantees" section.
- [ ] Write "Component Reference" table mapping each class to its source file.

### Relevant Context
- QIEA rotation: `bob_optimizer/solvers/qiea.py` — `_DELTA_THETA_TABLE`, `DynamicRotationGate.apply()`.
- Shannon diversity: `bob_optimizer/solvers/qiea.py:114` — `QBitVector.diversity()`.
- Catastrophe operator: `QuantumCatastropheOperator` in `bob_optimizer/solvers/qiea.py`.
- Domain model field names: `bob_optimizer/model/domain.py` (all Pydantic models).
- Cost terms: `bob_optimizer/model/qubo.py` — `_latency_cost`, `_resource_cost`,
  `_affinity_cost`, `_penalty_cost`, `build_objective`.
- λ defaults: ClusterTopology — lambda_latency=1.0, lambda_resource=10.0,
  lambda_affinity=5.0, lambda_penalty=100.0.

### Status
[ ] pending

---

## Sub-Task 2 — watsonx/ Integration Package

### Intent
Produce the three artefacts that an enterprise developer needs to register QUBOB as an external
skill in watsonx Orchestrate and connect it to Slack or watsonx Assistant. These files are
static/configuration — they do not require code execution to generate.

### Expected Outcomes
- Directory `watsonx/` exists with three files: `openapi_spec.json`, `qubob_orchestrate_skill.json`,
  and `README.md`.
- `openapi_spec.json` is a valid OpenAPI 3.0.3 document with:
  - `servers[0].url` set to a placeholder `https://qubob.your-org.example.com/api/v1`.
  - Three paths: `POST /analyze`, `POST /optimize`, `GET /plan/{cluster_id}`.
  - `operationId` values matching the names watsonx Orchestrate will use: `qubob_analyze`,
    `qubob_optimize`, `qubob_get_plan`.
  - Request/response schemas that exactly match the Pydantic field names in `domain.py`
    (`service_name`, `node_name`, `zone`, `total_cost`, `latency_cost`, etc.).
  - `BearerAuth` security scheme (HTTP Bearer JWT).
- `qubob_orchestrate_skill.json` is a watsonx Orchestrate skill descriptor with:
  - Three actions mapped to the three operationIds.
  - Input/output parameter display names and descriptions suitable for the
    Orchestrate skill builder UI.
  - `auth` block referencing the Bearer credential key name `QUBOB_API_KEY`.
- `watsonx/README.md` covers:
  - Prerequisites (IBM Cloud account, watsonx Orchestrate instance, Slack workspace).
  - Step-by-step instructions to deploy the QUBOB API server (`bob-opt dashboard`).
  - How to import `openapi_spec.json` into the Orchestrate External App skill wizard.
  - How to import `qubob_orchestrate_skill.json` as a pre-configured skill.
  - How to connect the skill to a Slack workspace via the Orchestrate Slack integration.
  - Example conversational prompts that trigger `qubob_analyze` and `qubob_optimize`.

### Todo List
- [ ] Create `watsonx/` directory.
- [ ] Write `watsonx/openapi_spec.json` — full OpenAPI 3.0.3 document.
- [ ] Write `watsonx/qubob_orchestrate_skill.json` — Orchestrate skill descriptor.
- [ ] Write `watsonx/README.md` — quickstart guide.

### Relevant Context
- Field names for request/response schemas come from `bob_optimizer/model/domain.py`:
  - `ServiceAssignment`: `service_name`, `node_name`, `zone`.
  - `PlacementPlan`: `assignments`, `total_cost`, `latency_cost`, `resource_cost`,
    `affinity_cost`, `penalty_cost`, `solver_name`, `solve_time_s`, `is_feasible`.
  - Algorithm choices: `["qiea", "ga", "greedy"]` from `bob_optimizer/cli/main.py:64`.
  - Default values: `algorithm="greedy"`, `generations=200`, `seed=42`.
- The `cluster_id` request field is an abstraction that the API server maps to a manifest path;
  it is never a raw filesystem path in the JSON.
- No watsonx SDK imports — these are pure JSON configuration files.

### Status
[ ] pending

---

## Sub-Task 3 — REST API Extension in app.py

### Intent
Add two new action endpoints (`POST /api/v1/analyze` and `POST /api/v1/optimize`) to the existing
FastAPI application in `bob_optimizer/dashboard/app.py`. These endpoints wrap the same CLI logic
already in `main.py` so watsonx Orchestrate can invoke QUBOB autonomously over HTTP. The existing
dashboard routes (`GET /`, `GET /api/topology`) must remain unchanged.

### Expected Outcomes
- `POST /api/v1/analyze` accepts `{"cluster_id": str, "manifest_type": "auto"|"kubernetes"|"compose"}`
  and returns topology summary (n_services, n_nodes, n_dependencies, bottlenecks list,
  latency_matrix).
- `POST /api/v1/optimize` accepts `{"cluster_id": str, "algorithm": str, "generations": int,
  "seed": int}` and returns placement plan (assignments list, cost breakdown,
  latency_reduction_pct, solver_name, solve_time_s).
- `GET /openapi.json` is served automatically by FastAPI and reflects the new routes.
- Both endpoints use Pydantic request/response models (not bare dicts) so FastAPI generates
  accurate OpenAPI schema.
- A `CLUSTER_REGISTRY` dict (environment-variable-overridable) maps `cluster_id` string keys
  to filesystem paths — this prevents raw paths from being exposed in the API.
- A `404` response with a clear message is returned if `cluster_id` is not in the registry.
- FastAPI and uvicorn remain optional imports (guarded by try/except as existing code does);
  Pydantic request/response models use `from __future__ import annotations` to avoid import
  errors when FastAPI is absent.
- `create_app()` is the only changed public function; `run_dashboard()` is unchanged.

### Todo List
- [ ] Add Pydantic request models: `AnalyzeRequest`, `OptimizeRequest` at the top of `app.py`.
- [ ] Add Pydantic response models: `AnalyzeResponse`, `OptimizeResponse` at the top of `app.py`.
- [ ] Add `CLUSTER_REGISTRY` dict populated from an env var `QUBOB_CLUSTER_REGISTRY_JSON`
  with a hardcoded fallback for local dev (mapping `"demo"` to a synthetic topology).
- [ ] Implement `_run_analyze(request: AnalyzeRequest) -> AnalyzeResponse` helper that:
    - Looks up the cluster path from `CLUSTER_REGISTRY`.
    - Calls `_collect_manifest_paths`, `_parse_topology`, `_inject_demo_nodes` (reusing
      the same helpers already in `main.py` but importing them here).
    - Computes bottleneck edges (top-5 by RPS, same logic as `analyze` CLI command).
    - Builds the latency matrix via `build_latency_matrix`.
    - Returns `AnalyzeResponse`.
- [ ] Implement `_run_optimize(request: OptimizeRequest) -> OptimizeResponse` helper that:
    - Looks up the cluster path.
    - Calls the appropriate solver using `_get_solver` from `main.py`.
    - Calls `build_objective`, `decompose_cost`, `_solution_to_plan`.
    - Computes `latency_reduction_pct` vs naive baseline.
    - Returns `OptimizeResponse`.
- [ ] Register `POST /api/v1/analyze` and `POST /api/v1/optimize` routes in `create_app()`.
- [ ] Ensure the import guard for FastAPI remains intact.

### Relevant Context
- `bob_optimizer/dashboard/app.py:611` — `create_app()` factory to extend.
- `bob_optimizer/cli/main.py` — reuse `_collect_manifest_paths`, `_parse_topology`,
  `_inject_demo_nodes`, `_get_solver`, `_solution_to_plan`, `decompose_cost`.
- `bob_optimizer/model/graph.py` — `build_latency_matrix(topology)` for latency matrix.
- `bob_optimizer/solvers/base.py:18` — `SolverResult` fields: `best_solution`,
  `best_cost`, `solve_time_s`.
- Solver `solve()` signature: `(objective, n_variables, n_nodes, *, seed, n_generations?)`.
- `_SOLVER_CHOICES = ["qiea", "ga", "greedy"]` from `main.py:64`.
- FastAPI and uvicorn are currently optional — keep import guard pattern.
- `httpx` is not in `pyproject.toml` dev deps; it must be added as a test dependency.

### Status
[ ] pending

---

## Sub-Task 4 — Test Suite: tests/test_api_watsonx.py

### Intent
Add a dedicated pytest module for the two new REST endpoints and the FastAPI OpenAPI schema.
Tests must be isolated, fast, and follow the existing project test patterns (no conftest.py,
inline helper functions, pytest.raises for error cases, numpy assertions where relevant).

### Expected Outcomes
- `tests/test_api_watsonx.py` exists and all tests pass with `pytest tests/test_api_watsonx.py`.
- Tests use `httpx.AsyncClient` with `transport=ASGITransport(app=create_app())` (TestClient
  pattern from fastapi.testclient is acceptable as a simpler alternative since app is sync-first).
- The following test cases are covered:
  - `test_analyze_demo_cluster` — POST to `/api/v1/analyze` with `cluster_id="demo"` returns
    HTTP 200, JSON with `n_services > 0`, `n_nodes > 0`, non-empty `bottlenecks` list.
  - `test_analyze_unknown_cluster` — POST with unknown `cluster_id` returns HTTP 404.
  - `test_optimize_demo_greedy` — POST to `/api/v1/optimize` with `algorithm="greedy"` returns
    HTTP 200, `assignments` list length equals `n_services`, all `node_name` values non-empty.
  - `test_optimize_demo_qiea` — same with `algorithm="qiea"`, `generations=50`.
  - `test_optimize_unknown_cluster` — returns HTTP 404.
  - `test_optimize_invalid_algorithm` — POST with `algorithm="bogus"` returns HTTP 422
    (Pydantic validation error).
  - `test_openapi_schema_has_analyze_path` — GET `/openapi.json` returns 200; JSON contains
    `/api/v1/analyze` key under `paths`.
  - `test_openapi_schema_has_optimize_path` — same for `/api/v1/optimize`.
  - `test_openapi_schema_operation_ids` — both `qubob_analyze` and `qubob_optimize` appear
    as `operationId` values in the schema.
  - `test_existing_topology_route_unchanged` — GET `/api/topology` still returns 200 with
    `services` key (regression guard for existing dashboard route).
- `httpx` and `fastapi[standard]` (or `fastapi` + `uvicorn`) must be added to
  `[project.optional-dependencies]` under a new `api` or added to `dev` extras in
  `pyproject.toml`.

### Todo List
- [ ] Add `httpx>=0.27` and `fastapi>=0.111` and `uvicorn>=0.29` to `pyproject.toml` dev deps
  (or a new `api` optional-dependency group).
- [ ] Create `tests/test_api_watsonx.py`.
- [ ] Write a `_make_test_client()` helper using `fastapi.testclient.TestClient(create_app())`.
- [ ] Implement all 10 test cases listed above.
- [ ] Verify `pytest tests/test_api_watsonx.py -v` passes (no existing tests broken).

### Relevant Context
- Test pattern: see `tests/test_diff_and_cli.py` for CLI runner-based tests; the new file
  follows the same class-free, function-per-test style.
- `bob_optimizer/dashboard/app.py:611` — `create_app()` returns the FastAPI app under test.
- `CLUSTER_REGISTRY` must include `"demo"` key pointing to a synthetic topology (or the
  existing pre-computed `_DEMO_TOPO`) so tests run without filesystem access.
- FastAPI `TestClient` is part of `starlette` (shipped with `fastapi`) — no extra dep needed
  beyond adding `fastapi` itself to dev extras.
- Existing tests import nothing from `dashboard/app.py`; this is the first test file to do so.

### Status
[ ] pending

---

## Dependency Notes

- Sub-Tasks 1 and 2 are fully independent and can be implemented in any order.
- Sub-Task 3 (API extension) must complete before Sub-Task 4 (tests), since the tests import
  `create_app()` from the modified `app.py`.
- Sub-Task 1 and 2 have no code dependencies on Sub-Tasks 3 or 4.

## Acceptance Criteria (Global)

- `pytest tests/` passes with no new failures after all four sub-tasks are complete.
- `ruff check bob_optimizer/dashboard/app.py tests/test_api_watsonx.py` passes (line-length
  100, ruff rules E/F/I/UP/B/SIM).
- `ARCHITECTURE.md` renders correctly in GitHub markdown (Mermaid diagrams display).
- `watsonx/openapi_spec.json` is valid JSON and can be parsed without errors.
- No existing routes in `app.py` are removed or renamed.
