# QUBOB × watsonx Orchestrate — Quickstart Guide

Integrate QUBOB's quantum-inspired microservice placement optimizer into watsonx Orchestrate
so enterprise developers can trigger cluster optimization from Slack or watsonx Assistant.

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| IBM Cloud account | With a watsonx Orchestrate instance |
| watsonx Orchestrate | Skill Builder access |
| Slack workspace | With the watsonx Orchestrate Slack App installed |
| Python | ≥ 3.13 |
| QUBOB installed | `pip install -e ".[api]"` from the repo root |
| `fastapi` + `uvicorn` | Installed via the `api` extra above |

---

## Step 1 — Deploy the QUBOB API Server

### 1a. Install API dependencies

```bash
# From the repository root
pip install -e ".[api]"
# or with uv:
uv sync --extra api
```

### 1b. Configure the cluster registry

The server maps logical `cluster_id` strings to manifest directories via the
`QUBOB_CLUSTER_REGISTRY_JSON` environment variable. **Never put raw filesystem paths in
API request payloads** — the registry is the security boundary.

```bash
export QUBOB_CLUSTER_REGISTRY_JSON='{
  "staging-k8s":  "/var/manifests/staging",
  "prod-k8s":     "/var/manifests/prod",
  "ecommerce":    "examples/ecommerce"
}'
```

Built-in fallbacks (always available without configuration):

| `cluster_id` | Behaviour |
|---|---|
| `"demo"` | Synthetic 10-service ecommerce topology (no manifests needed) |
| `"ecommerce"` | Points to `examples/ecommerce/` in the repo (if path exists, else synthetic) |

### 1c. Start the server

```bash
bob-opt dashboard --host 0.0.0.0 --port 8080 --no-browser
```

The dashboard serves both the interactive UI at `/` and the REST API at `/api/v1/`.

### 1d. Verify the server is running

```bash
curl http://localhost:8080/openapi.json | python -m json.tool | head -20
curl -X POST http://localhost:8080/api/v1/analyze \
     -H "Content-Type: application/json" \
     -d '{"cluster_id": "demo"}'
```

---

## Step 2 — Import the OpenAPI Spec into watsonx Orchestrate

1. Open your **watsonx Orchestrate** instance.
2. Navigate to **Skills** → **Add skill** → **From API**.
3. Choose **Import OpenAPI file** and upload `watsonx/openapi_spec.json`.
4. Edit the `servers[0].url` field to match your deployed server URL
   (e.g. `https://qubob.your-org.example.com/api/v1`).
5. Click **Next**. Orchestrate will parse the three operations:
   - `qubob_analyze` — Analyze Cluster Topology
   - `qubob_optimize` — Optimize Service Placement
   - `qubob_get_plan` — Get Cached Placement Plan
6. Review the auto-generated input/output parameter mappings and click **Save skill**.

---

## Step 3 — Import the Pre-Configured Skill Definition

For a richer configuration with display names, parameter descriptions, and example prompts,
import the pre-built skill descriptor instead of (or in addition to) the OpenAPI spec:

1. In watsonx Orchestrate → **Skills** → **Import skill** → **Upload JSON**.
2. Upload `watsonx/qubob_orchestrate_skill.json`.
3. Review and confirm the three actions and their parameter mappings.

---

## Step 4 — Configure Authentication

1. In the imported skill, open **Authentication** settings.
2. Set auth type to **Bearer token**.
3. Enter your QUBOB API server's authentication token in the **credential vault** under the
   key `QUBOB_API_KEY`.

> **Security note:** The credential vault stores secrets encrypted at rest. The token is
> injected as `Authorization: Bearer <token>` at invocation time and never logged.

---

## Step 5 — Connect to Slack

1. In watsonx Orchestrate → **Channels** → **Slack**.
2. Follow the guided Slack App installation flow for your workspace.
3. Invite the Orchestrate bot to a channel: `/invite @watsonx-orchestrate`.
4. Test with a mention:

```
@watsonx-orchestrate Analyze the staging Kubernetes cluster topology
@watsonx-orchestrate Optimize staging-k8s using QIEA with 500 generations
```

---

## Step 6 — Connect to watsonx Assistant (Optional)

1. In watsonx Assistant → **Integrations** → **watsonx Orchestrate**.
2. Link your Orchestrate instance.
3. Enable the **QUBOB Placement Optimizer** skill in the integration panel.
4. In your Assistant dialog, add an action that invokes `qubob_analyze` or `qubob_optimize`
   based on recognized user intent.

---

## Example Conversational Flows

### Flow 1 — Pre-Release Optimization

```
Developer: @watsonx-orchestrate optimize staging before prod release
Orchestrate: Analyzing staging-k8s...
             Found 12 services and 3 nodes.
             Top bottleneck: api-gateway → order-service (450 RPS).
             Which solver? QIEA (best quality) or greedy (fastest)?
Developer: qiea, 500 generations
Orchestrate: Running QIEA optimizer...
             Done! QIEA reduced weighted latency cost by 38.2% in 4.7s.
             Plan is feasible (all 12 services assigned).
             Apply nodeAffinity patches to staging manifests?
Developer: yes
Orchestrate: Applied. 12 files patched with .bak backups. Ready for prod promotion.
```

### Flow 2 — CI/CD Smoke Check

```
Developer: @watsonx-orchestrate run greedy optimizer on demo cluster
Orchestrate: Running Greedy FFD on demo...
             Done in 0.003s. Cost reduction: 22.1%. Plan feasible.
             Assignments: api-gw→node-0, order-svc→node-1, auth-svc→node-2...
```

---

## Troubleshooting

| Symptom | Resolution |
|---------|-----------|
| `404 cluster_id not found` | Add the cluster to `QUBOB_CLUSTER_REGISTRY_JSON` |
| `422 Unprocessable Entity` | Check that `algorithm` is one of `qiea`, `ga`, `greedy` |
| `401 Unauthorized` | Verify `QUBOB_API_KEY` is set correctly in the Orchestrate vault |
| `Connection refused` on import | Ensure the QUBOB server is running and accessible from Orchestrate |
| OpenAPI import fails | Validate `watsonx/openapi_spec.json` with a JSON Schema validator |
| Slack bot not responding | Re-invite the bot: `/invite @watsonx-orchestrate` |

---

## File Reference

| File | Purpose |
|------|---------|
| `watsonx/openapi_spec.json` | OpenAPI 3.0.3 specification — import directly into Orchestrate |
| `watsonx/qubob_orchestrate_skill.json` | Pre-configured skill descriptor with display names and example prompts |
| `watsonx/README.md` | This guide |
| `ARCHITECTURE.md` | Full system architecture, math formulation, security model |
| `bob_optimizer/dashboard/app.py` | FastAPI application — `POST /api/v1/analyze`, `POST /api/v1/optimize` |
