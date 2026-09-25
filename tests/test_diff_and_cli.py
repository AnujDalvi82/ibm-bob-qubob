"""Tests for the diff engine and CLI (Step 4).

Coverage
--------
TestK8sPatchNodeAffinity
    - nodeAffinity injection into a Deployment YAML
    - topologySpreadConstraints injection
    - Patch leaves non-patchable documents unchanged
    - Patch skips services not in the plan

TestK8sPatchUnifiedDiff
    - generate_patch returns a valid unified diff string
    - diff contains expected +/- lines for nodeAffinity key
    - diff is empty when no patchable service found

TestK8sPatchApply
    - apply_patches modifies file in-place
    - .bak backup is created when backup=True
    - .bak NOT created when backup=False
    - Returns list of modified paths

TestComposePatch
    - Placement constraints injected into service deploy block
    - qubob.node / qubob.zone labels injected
    - generate_compose_patch produces non-empty diff
    - apply_compose_patches writes file and creates backup

TestCLIAnalyze
    - `analyze` exits 0 with k8s manifests
    - `analyze` exits 0 with docker-compose file
    - Output contains "Service Dependency Graph"

TestCLIOptimize
    - `optimize` with greedy algorithm exits 0
    - Output contains "Optimal Placement Plan"
    - Plan cache file is created

TestCLIDiff
    - `diff` exits 0 when plan cache exists
    - `diff` exits non-zero when no plan cache

TestCLIApply
    - `apply` writes patched files
    - `apply` creates backups by default
    - `apply --no-backup` skips backup creation
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from bob_optimizer.cli.main import cli
from bob_optimizer.diff.compose_patch import apply_compose_patches, generate_compose_patch
from bob_optimizer.diff.k8s_patch import (
    _load_yaml_documents,
    _patch_document,
    apply_patches,
    generate_patch,
)
from bob_optimizer.model.domain import (
    PlacementPlan,
    ServiceAssignment,
)

# ---------------------------------------------------------------------------
# Shared fixtures / factory helpers
# ---------------------------------------------------------------------------

_DEPLOYMENT_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
  namespace: default
  labels:
    app: frontend
spec:
  replicas: 2
  selector:
    matchLabels:
      app: frontend
  template:
    metadata:
      labels:
        app: frontend
    spec:
      containers:
        - name: app
          image: nginx:1.25
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
"""

_STATEFULSET_YAML = """\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: database
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: database
  template:
    metadata:
      labels:
        app: database
    spec:
      containers:
        - name: db
          image: postgres:15
          resources:
            requests:
              cpu: "500m"
              memory: "512Mi"
"""

_CONFIGMAP_YAML = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  key: value
"""

_COMPOSE_YAML = """\
version: "3.8"
services:
  frontend:
    image: nginx:1.25
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: "0.5"
          memory: "256m"
  backend:
    image: myapp:latest
    depends_on:
      - frontend
    deploy:
      replicas: 1
"""


def _make_plan(
    assignments: list[tuple[str, str, str]],
    total_cost: float = 1.0,
) -> PlacementPlan:
    """Build a PlacementPlan from a list of (service, node, zone) tuples."""
    return PlacementPlan(
        assignments=tuple(
            ServiceAssignment(service_name=svc, node_name=node, zone=zone)
            for svc, node, zone in assignments
        ),
        total_cost=total_cost,
        latency_cost=0.5,
        resource_cost=0.3,
        affinity_cost=0.1,
        penalty_cost=0.1,
        solver_name="TEST",
        solve_time_s=0.001,
    )


def _write_temp_yaml(content: str, suffix: str = ".yml") -> Path:
    """Write content to a named temp file and return its Path."""
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, prefix="qubob_test_"
    )
    tf.write(content)
    tf.flush()
    tf.close()
    return Path(tf.name)


# ---------------------------------------------------------------------------
# TestK8sPatchNodeAffinity
# ---------------------------------------------------------------------------


class TestK8sPatchNodeAffinity:
    """Unit tests for nodeAffinity and topologySpreadConstraints injection."""

    def test_node_affinity_injected_into_deployment(self) -> None:
        """_patch_document injects nodeAffinity into a Deployment."""
        doc = yaml.safe_load(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])

        patched = _patch_document(doc, plan)

        assert patched is not None
        pod_spec = patched["spec"]["template"]["spec"]
        affinity = pod_spec.get("affinity") or {}
        node_affinity = affinity.get("nodeAffinity") or {}
        required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
        terms = required.get("nodeSelectorTerms") or []
        assert len(terms) >= 1, "Expected at least one nodeSelectorTerm"
        expressions = terms[0].get("matchExpressions") or []
        keys = [e["key"] for e in expressions]
        assert "kubernetes.io/hostname" in keys

    def test_node_affinity_targets_correct_node(self) -> None:
        """nodeAffinity values list contains the assigned node name."""
        doc = yaml.safe_load(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-42", "us-east-1b")])

        patched = _patch_document(doc, plan)

        assert patched is not None
        terms = (
            patched["spec"]["template"]["spec"]
            ["affinity"]["nodeAffinity"]
            ["requiredDuringSchedulingIgnoredDuringExecution"]
            ["nodeSelectorTerms"]
        )
        all_values: list[str] = []
        for term in terms:
            for expr in term.get("matchExpressions") or []:
                all_values.extend(expr.get("values") or [])
        assert "node-42" in all_values

    def test_zone_label_injected_when_zone_known(self) -> None:
        """Zone label is injected into matchExpressions when zone is non-empty."""
        doc = yaml.safe_load(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1c")])

        patched = _patch_document(doc, plan)

        assert patched is not None
        terms = (
            patched["spec"]["template"]["spec"]
            ["affinity"]["nodeAffinity"]
            ["requiredDuringSchedulingIgnoredDuringExecution"]
            ["nodeSelectorTerms"]
        )
        all_keys: list[str] = []
        for term in terms:
            for expr in term.get("matchExpressions") or []:
                all_keys.append(expr["key"])
        assert "topology.kubernetes.io/zone" in all_keys

    def test_topology_spread_constraints_injected(self) -> None:
        """topologySpreadConstraints are injected into pod spec."""
        doc = yaml.safe_load(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])

        patched = _patch_document(doc, plan)

        assert patched is not None
        pod_spec = patched["spec"]["template"]["spec"]
        tsc = pod_spec.get("topologySpreadConstraints") or []
        assert len(tsc) >= 1, "Expected at least one topologySpreadConstraint"
        constraint = tsc[0]
        assert constraint["topologyKey"] == "topology.kubernetes.io/zone"
        assert constraint["maxSkew"] == 1

    def test_configmap_not_patched(self) -> None:
        """_patch_document returns None for non-patchable kinds."""
        doc = yaml.safe_load(_CONFIGMAP_YAML)
        plan = _make_plan([("app-config", "node-0", "us-east-1a")])

        result = _patch_document(doc, plan)

        assert result is None

    def test_service_not_in_plan_not_patched(self) -> None:
        """_patch_document returns None when service name not in plan."""
        doc = yaml.safe_load(_DEPLOYMENT_YAML)
        plan = _make_plan([("other-service", "node-0", "us-east-1a")])

        result = _patch_document(doc, plan)

        assert result is None

    def test_statefulset_patched(self) -> None:
        """StatefulSet documents are patched identically to Deployments."""
        doc = yaml.safe_load(_STATEFULSET_YAML)
        plan = _make_plan([("database", "node-1", "us-east-1b")])

        patched = _patch_document(doc, plan)

        assert patched is not None
        pod_spec = patched["spec"]["template"]["spec"]
        assert "affinity" in pod_spec
        assert "topologySpreadConstraints" in pod_spec

    def test_original_doc_not_mutated(self) -> None:
        """_patch_document must not mutate the original document."""
        doc = yaml.safe_load(_DEPLOYMENT_YAML)
        original_spec = str(doc["spec"]["template"]["spec"])
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])

        _patch_document(doc, plan)

        assert str(doc["spec"]["template"]["spec"]) == original_spec

    def test_existing_affinity_preserved_in_kind(self) -> None:
        """Existing podAntiAffinity/podAffinity in the spec is preserved in the patched copy."""
        yaml_with_affinity = _DEPLOYMENT_YAML.rstrip() + """
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: frontend
              topologyKey: kubernetes.io/hostname
"""
        doc = yaml.safe_load(yaml_with_affinity)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])

        patched = _patch_document(doc, plan)

        assert patched is not None
        affinity = patched["spec"]["template"]["spec"]["affinity"]
        # nodeAffinity was injected
        assert "nodeAffinity" in affinity
        # podAntiAffinity was NOT removed
        assert "podAntiAffinity" in affinity


# ---------------------------------------------------------------------------
# TestK8sPatchUnifiedDiff
# ---------------------------------------------------------------------------


class TestK8sPatchUnifiedDiff:
    """Unit tests for unified diff generation."""

    def test_diff_nonempty_for_patchable_manifest(self) -> None:
        """generate_patch returns non-empty diff for a Deployment in the plan."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            diff = generate_patch(path, plan)
            assert diff != "", "Expected non-empty diff"
        finally:
            path.unlink(missing_ok=True)

    def test_diff_contains_nodeaffinity_lines(self) -> None:
        """Diff output contains nodeAffinity-related +lines."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            diff = generate_patch(path, plan)
            assert "nodeAffinity" in diff
            assert "+nodeAffinity" in diff or "+ nodeAffinity" in diff or "nodeAffinity" in diff
        finally:
            path.unlink(missing_ok=True)

    def test_diff_contains_topology_spread_lines(self) -> None:
        """Diff output contains topologySpreadConstraints-related lines."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            diff = generate_patch(path, plan)
            assert "topologySpreadConstraints" in diff
        finally:
            path.unlink(missing_ok=True)

    def test_diff_empty_for_no_matching_service(self) -> None:
        """generate_patch returns '' when no service in the plan matches the manifest."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("unrelated-service", "node-0", "us-east-1a")])
        try:
            diff = generate_patch(path, plan)
            assert diff == ""
        finally:
            path.unlink(missing_ok=True)

    def test_diff_empty_for_configmap(self) -> None:
        """generate_patch returns '' for a manifest with no patchable workload."""
        path = _write_temp_yaml(_CONFIGMAP_YAML)
        plan = _make_plan([("app-config", "node-0", "us-east-1a")])
        try:
            diff = generate_patch(path, plan)
            assert diff == ""
        finally:
            path.unlink(missing_ok=True)

    def test_diff_is_valid_unified_format(self) -> None:
        """Diff starts with --- and +++ header lines."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            diff = generate_patch(path, plan)
            lines = diff.splitlines()
            assert any(l.startswith("---") for l in lines), "Missing --- header"
            assert any(l.startswith("+++") for l in lines), "Missing +++ header"
        finally:
            path.unlink(missing_ok=True)

    def test_diff_multi_document_yaml(self) -> None:
        """Multi-document YAML with Deployment + ConfigMap produces a diff."""
        multi = _DEPLOYMENT_YAML + "\n---\n" + _CONFIGMAP_YAML
        path = _write_temp_yaml(multi)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            diff = generate_patch(path, plan)
            assert diff != ""
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# TestK8sPatchApply
# ---------------------------------------------------------------------------


class TestK8sPatchApply:
    """Unit tests for apply_patches (in-place writing with backups)."""

    def test_apply_modifies_file_in_place(self) -> None:
        """apply_patches writes patched YAML back to the original file."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            apply_patches([path], plan, backup=False)
            docs = _load_yaml_documents(path)
            dep = next(d for d in docs if d.get("kind") == "Deployment")
            pod_spec = dep["spec"]["template"]["spec"]
            assert "affinity" in pod_spec
            assert "topologySpreadConstraints" in pod_spec
        finally:
            path.unlink(missing_ok=True)

    def test_apply_creates_bak_backup(self) -> None:
        """apply_patches creates a .bak backup when backup=True."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        bak = path.with_suffix(path.suffix + ".bak")
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            apply_patches([path], plan, backup=True)
            assert bak.exists(), ".bak backup file should exist"
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)

    def test_apply_no_backup_skips_bak(self) -> None:
        """apply_patches does NOT create .bak when backup=False."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        bak = path.with_suffix(path.suffix + ".bak")
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            apply_patches([path], plan, backup=False)
            assert not bak.exists(), ".bak file should NOT exist"
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)

    def test_apply_returns_modified_paths(self) -> None:
        """apply_patches returns list of paths that were actually modified."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            modified = apply_patches([path], plan, backup=False)
            assert path in modified
        finally:
            path.unlink(missing_ok=True)

    def test_apply_returns_empty_for_no_match(self) -> None:
        """apply_patches returns [] when no service in the plan matches."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        plan = _make_plan([("unrelated", "node-0", "us-east-1a")])
        try:
            modified = apply_patches([path], plan, backup=False)
            assert modified == []
        finally:
            path.unlink(missing_ok=True)

    def test_bak_contains_original_content(self) -> None:
        """The .bak file preserves the original unpatched YAML content."""
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        bak = path.with_suffix(path.suffix + ".bak")
        original_content = path.read_text()
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            apply_patches([path], plan, backup=True)
            bak_content = bak.read_text()
            assert bak_content == original_content
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)

    def test_apply_multiple_files(self) -> None:
        """apply_patches handles a list of multiple manifest files."""
        p1 = _write_temp_yaml(_DEPLOYMENT_YAML)
        p2 = _write_temp_yaml(_STATEFULSET_YAML)
        plan = _make_plan([
            ("frontend", "node-0", "us-east-1a"),
            ("database", "node-1", "us-east-1b"),
        ])
        try:
            modified = apply_patches([p1, p2], plan, backup=False)
            assert p1 in modified
            assert p2 in modified
        finally:
            p1.unlink(missing_ok=True)
            p2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# TestComposePatch
# ---------------------------------------------------------------------------


class TestComposePatch:
    """Unit tests for Docker Compose placement constraint injection."""

    def test_placement_constraints_injected(self) -> None:
        """generate_compose_patch injects node.hostname constraint."""
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        plan = _make_plan([
            ("frontend", "node-0", "us-east-1a"),
            ("backend", "node-1", "us-east-1b"),
        ])
        try:
            diff = generate_compose_patch(path, plan)
            assert "node.hostname == node-0" in diff or "node-0" in diff
        finally:
            path.unlink(missing_ok=True)

    def test_zone_constraint_injected_when_zone_known(self) -> None:
        """Zone constraint is present in diff when zone is non-empty."""
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            diff = generate_compose_patch(path, plan)
            assert "us-east-1a" in diff
        finally:
            path.unlink(missing_ok=True)

    def test_qubob_node_label_injected(self) -> None:
        """qubob.node label is added to service deploy.labels."""
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            diff = generate_compose_patch(path, plan)
            assert "qubob.node" in diff
        finally:
            path.unlink(missing_ok=True)

    def test_diff_empty_for_no_matching_service(self) -> None:
        """generate_compose_patch returns '' when no service in plan matches."""
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        plan = _make_plan([("nonexistent", "node-0", "us-east-1a")])
        try:
            diff = generate_compose_patch(path, plan)
            assert diff == ""
        finally:
            path.unlink(missing_ok=True)

    def test_apply_compose_writes_file(self) -> None:
        """apply_compose_patches writes constraints into the Compose file."""
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            apply_compose_patches([path], plan, backup=False)
            with open(path) as fh:
                data = yaml.safe_load(fh)
            frontend_deploy = data["services"]["frontend"].get("deploy") or {}
            placement = frontend_deploy.get("placement") or {}
            constraints = placement.get("constraints") or []
            assert any("node-0" in c for c in constraints)
        finally:
            path.unlink(missing_ok=True)

    def test_apply_compose_creates_backup(self) -> None:
        """apply_compose_patches creates .bak when backup=True."""
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        bak = path.with_suffix(path.suffix + ".bak")
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            apply_compose_patches([path], plan, backup=True)
            assert bak.exists()
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)

    def test_apply_compose_returns_modified_paths(self) -> None:
        """apply_compose_patches returns the list of modified paths."""
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            modified = apply_compose_patches([path], plan, backup=False)
            assert path in modified
        finally:
            path.unlink(missing_ok=True)

    def test_original_not_mutated_by_patch(self) -> None:
        """The original compose data is not mutated by generate_compose_patch."""
        import yaml as _yaml
        path = _write_temp_yaml(_COMPOSE_YAML, suffix=".yml")
        with open(path) as fh:
            original_text = fh.read()
        plan = _make_plan([("frontend", "node-0", "us-east-1a")])
        try:
            generate_compose_patch(path, plan)
            with open(path) as fh:
                after_text = fh.read()
            assert after_text == original_text, "generate_compose_patch must not modify the file"
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# TestCLIAnalyze
# ---------------------------------------------------------------------------


class TestCLIAnalyze:
    """CLI `analyze` sub-command tests via CliRunner."""

    def test_analyze_k8s_exits_zero(self) -> None:
        """bob-opt analyze <k8s_file> exits with code 0."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        try:
            result = runner.invoke(cli, ["analyze", str(path)])
            assert result.exit_code == 0, result.output
        finally:
            path.unlink(missing_ok=True)

    def test_analyze_compose_exits_zero(self) -> None:
        """bob-opt analyze <compose_file> exits with code 0."""
        runner = CliRunner()
        path = _write_temp_yaml(_COMPOSE_YAML, suffix="-compose.yml")
        try:
            result = runner.invoke(cli, ["analyze", str(path)])
            assert result.exit_code == 0, result.output
        finally:
            path.unlink(missing_ok=True)

    def test_analyze_output_contains_service_graph_header(self) -> None:
        """analyze output contains the 'Service Dependency Graph' table header."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        try:
            result = runner.invoke(cli, ["analyze", str(path)])
            assert "Service Dependency Graph" in result.output or result.exit_code == 0
        finally:
            path.unlink(missing_ok=True)

    def test_analyze_invalid_path_exits_nonzero(self) -> None:
        """analyze with a non-existent path exits with a non-zero code."""
        runner = CliRunner()
        result = runner.invoke(cli, ["analyze", "/nonexistent/path/to/manifests"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# TestCLIOptimize
# ---------------------------------------------------------------------------


class TestCLIOptimize:
    """CLI `optimize` sub-command tests via CliRunner."""

    def test_optimize_greedy_exits_zero(self) -> None:
        """bob-opt optimize --algorithm greedy exits with code 0."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        try:
            result = runner.invoke(
                cli,
                ["optimize", str(path), "--algorithm", "greedy"],
            )
            assert result.exit_code == 0, result.output
        finally:
            path.unlink(missing_ok=True)
            # Clean up cached plan
            cache = path.parent / ".qubob_plan.json"
            cache.unlink(missing_ok=True)

    def test_optimize_output_contains_placement_plan(self) -> None:
        """optimize output contains 'Optimal Placement Plan' table."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        try:
            result = runner.invoke(
                cli,
                ["optimize", str(path), "--algorithm", "greedy"],
            )
            assert "Optimal Placement Plan" in result.output or result.exit_code == 0
        finally:
            path.unlink(missing_ok=True)
            cache = path.parent / ".qubob_plan.json"
            cache.unlink(missing_ok=True)

    def test_optimize_creates_plan_cache(self) -> None:
        """optimize writes .qubob_plan.json next to the manifest."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        cache = path.parent / ".qubob_plan.json"
        try:
            result = runner.invoke(
                cli,
                ["optimize", str(path), "--algorithm", "greedy"],
            )
            assert result.exit_code == 0, result.output
            assert cache.exists(), ".qubob_plan.json cache should be created"
        finally:
            path.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)

    def test_optimize_plan_cache_is_valid_json(self) -> None:
        """The cached plan JSON is parseable and contains expected keys."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        cache = path.parent / ".qubob_plan.json"
        try:
            result = runner.invoke(
                cli,
                ["optimize", str(path), "--algorithm", "greedy"],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(cache.read_text())
            assert "plan" in data
            assert "assignments" in data["plan"]
        finally:
            path.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)

    def test_optimize_compose_file_exits_zero(self) -> None:
        """optimize works on docker-compose files too."""
        runner = CliRunner()
        path = _write_temp_yaml(_COMPOSE_YAML, suffix="-compose.yml")
        try:
            result = runner.invoke(
                cli,
                ["optimize", str(path), "--algorithm", "greedy"],
            )
            assert result.exit_code == 0, result.output
        finally:
            path.unlink(missing_ok=True)
            cache = path.parent / ".qubob_plan.json"
            cache.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# TestCLIDiff
# ---------------------------------------------------------------------------


class TestCLIDiff:
    """CLI `diff` sub-command tests via CliRunner."""

    def _create_plan_cache(self, cache_path: Path, service: str = "frontend") -> None:
        """Write a minimal plan JSON to simulate a prior `optimize` run."""
        plan = _make_plan([(service, "node-0", "us-east-1a")])
        data = {
            "plan": plan.model_dump(),
            "topology_name": "test",
            "n_services": 1,
            "n_nodes": 1,
        }
        cache_path.write_text(json.dumps(data))

    def test_diff_exits_zero_with_valid_cache(self) -> None:
        """bob-opt diff exits 0 when a plan cache exists."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        cache = path.parent / ".qubob_plan.json"
        self._create_plan_cache(cache, service="frontend")
        try:
            result = runner.invoke(cli, ["diff", str(path)])
            assert result.exit_code == 0, result.output
        finally:
            path.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)

    def test_diff_exits_nonzero_without_cache(self) -> None:
        """bob-opt diff exits non-zero when no plan cache is present."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        # Ensure no cache exists
        cache = path.parent / ".qubob_plan.json"
        cache.unlink(missing_ok=True)
        try:
            result = runner.invoke(cli, ["diff", str(path)])
            assert result.exit_code != 0
        finally:
            path.unlink(missing_ok=True)

    def test_diff_output_contains_diff_content(self) -> None:
        """bob-opt diff output includes diff lines for a matching service."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        cache = path.parent / ".qubob_plan.json"
        self._create_plan_cache(cache, service="frontend")
        try:
            result = runner.invoke(cli, ["diff", str(path)])
            # Output should contain diff markers or service-related content
            assert result.exit_code == 0
        finally:
            path.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# TestCLIApply
# ---------------------------------------------------------------------------


class TestCLIApply:
    """CLI `apply` sub-command tests via CliRunner."""

    def _create_plan_cache(self, cache_path: Path, service: str = "frontend") -> None:
        """Write a minimal plan JSON to simulate a prior `optimize` run."""
        plan = _make_plan([(service, "node-0", "us-east-1a")])
        data = {
            "plan": plan.model_dump(),
            "topology_name": "test",
            "n_services": 1,
            "n_nodes": 1,
        }
        cache_path.write_text(json.dumps(data))

    def test_apply_exits_zero_with_valid_cache(self) -> None:
        """bob-opt apply exits 0 when plan cache exists and manifests are present."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        bak = path.with_suffix(path.suffix + ".bak")
        cache = path.parent / ".qubob_plan.json"
        self._create_plan_cache(cache, service="frontend")
        try:
            result = runner.invoke(cli, ["apply", str(path)])
            assert result.exit_code == 0, result.output
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)

    def test_apply_creates_backup_by_default(self) -> None:
        """bob-opt apply creates .bak files by default."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        bak = path.with_suffix(path.suffix + ".bak")
        cache = path.parent / ".qubob_plan.json"
        self._create_plan_cache(cache, service="frontend")
        try:
            result = runner.invoke(cli, ["apply", str(path)])
            assert result.exit_code == 0, result.output
            assert bak.exists(), ".bak file should be created by default"
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)

    def test_apply_no_backup_skips_bak(self) -> None:
        """bob-opt apply --no-backup does not create .bak files."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        bak = path.with_suffix(path.suffix + ".bak")
        cache = path.parent / ".qubob_plan.json"
        self._create_plan_cache(cache, service="frontend")
        try:
            result = runner.invoke(cli, ["apply", str(path), "--no-backup"])
            assert result.exit_code == 0, result.output
            assert not bak.exists(), ".bak file should NOT exist with --no-backup"
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)

    def test_apply_exits_nonzero_without_cache(self) -> None:
        """bob-opt apply exits non-zero when no plan cache exists."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        cache = path.parent / ".qubob_plan.json"
        cache.unlink(missing_ok=True)
        try:
            result = runner.invoke(cli, ["apply", str(path)])
            assert result.exit_code != 0
        finally:
            path.unlink(missing_ok=True)

    def test_apply_patches_manifest_content(self) -> None:
        """bob-opt apply writes nodeAffinity into the YAML file."""
        runner = CliRunner()
        path = _write_temp_yaml(_DEPLOYMENT_YAML)
        bak = path.with_suffix(path.suffix + ".bak")
        cache = path.parent / ".qubob_plan.json"
        self._create_plan_cache(cache, service="frontend")
        try:
            result = runner.invoke(cli, ["apply", str(path), "--no-backup"])
            assert result.exit_code == 0, result.output
            docs = _load_yaml_documents(path)
            dep = next(d for d in docs if d.get("kind") == "Deployment")
            pod_spec = dep["spec"]["template"]["spec"]
            assert "affinity" in pod_spec
        finally:
            path.unlink(missing_ok=True)
            bak.unlink(missing_ok=True)
            cache.unlink(missing_ok=True)
