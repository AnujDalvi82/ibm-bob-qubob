"""Kubernetes nodeAffinity and topologySpreadConstraints patch generator.

Takes a ``ClusterTopology`` and an optimal ``PlacementPlan`` produced by any
QUBOB solver, reads the original Kubernetes YAML manifests, and injects:

  a) ``spec.template.spec.affinity.nodeAffinity`` — hard ``requiredDuring…``
     term targeting the optimal node name and zone label.
  b) ``spec.template.spec.topologySpreadConstraints`` — ensures replicas are
     spread across failure domains (zones) for high availability.

Produces standard unified diff output (compatible with ``patch -p0`` and
``git apply``) and optionally writes patched files to disk with ``.bak``
backups of the originals.

Public API
----------
generate_patch      : (path, plan, topology) → unified diff string
apply_patches       : (paths, plan, topology, backup) → list[Path] written
"""

from __future__ import annotations

import copy
import difflib
import shutil
from pathlib import Path
from typing import Any

import yaml

from bob_optimizer.model.domain import ClusterTopology, PlacementPlan

# ---------------------------------------------------------------------------
# Kubernetes workload kinds that carry pod templates
# ---------------------------------------------------------------------------

_PATCHABLE_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}

# Node label key used for zone-level targeting
_ZONE_LABEL_KEY = "topology.kubernetes.io/zone"
# Node label key for the specific node name
_NODE_LABEL_KEY = "kubernetes.io/hostname"

# topologySpreadConstraints max-skew for zone spreading
_ZONE_SPREAD_MAX_SKEW = 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _node_for_service(service_name: str, plan: PlacementPlan) -> str | None:
    """Return the node name assigned to *service_name* in *plan*, or None."""
    for a in plan.assignments:
        if a.service_name == service_name:
            return a.node_name
    return None


def _zone_for_service(service_name: str, plan: PlacementPlan) -> str | None:
    """Return the zone assigned to *service_name* in *plan*, or None."""
    for a in plan.assignments:
        if a.service_name == service_name:
            return a.zone or None
    return None


def _node_affinity_block(node_name: str, zone: str | None) -> dict[str, Any]:
    """Build a ``nodeAffinity`` dict targeting *node_name* (and *zone* if known).

    Uses ``requiredDuringSchedulingIgnoredDuringExecution`` so placement is
    enforced by the k8s scheduler, not merely preferred.
    """
    match_expressions: list[dict[str, Any]] = [
        {
            "key": _NODE_LABEL_KEY,
            "operator": "In",
            "values": [node_name],
        }
    ]
    if zone:
        match_expressions.append(
            {
                "key": _ZONE_LABEL_KEY,
                "operator": "In",
                "values": [zone],
            }
        )
    return {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {"matchExpressions": match_expressions}
            ]
        }
    }


def _topology_spread_constraints(zone: str | None) -> list[dict[str, Any]]:
    """Build ``topologySpreadConstraints`` for zone-level HA spreading.

    Returns one constraint that spreads replicas across
    ``topology.kubernetes.io/zone`` with ``maxSkew=1`` and
    ``whenUnsatisfiable=DoNotSchedule``.
    """
    constraint: dict[str, Any] = {
        "maxSkew": _ZONE_SPREAD_MAX_SKEW,
        "topologyKey": _ZONE_LABEL_KEY,
        "whenUnsatisfiable": "DoNotSchedule",
        "labelSelector": {},
    }
    return [constraint]


def _patch_pod_spec(
    pod_spec: dict[str, Any],
    node_name: str,
    zone: str | None,
) -> dict[str, Any]:
    """Return a *deep copy* of *pod_spec* with nodeAffinity and spread constraints injected.

    Parameters
    ----------
    pod_spec:
        The ``spec.template.spec`` dict from a Deployment/StatefulSet.
    node_name:
        The target node name from the placement plan.
    zone:
        The target availability zone (may be empty string or None).

    Returns
    -------
    New dict — original is never mutated.
    """
    spec = copy.deepcopy(pod_spec)

    # ---- nodeAffinity ---------------------------------------------------
    affinity: dict[str, Any] = spec.get("affinity") or {}
    affinity = copy.deepcopy(affinity)
    affinity["nodeAffinity"] = _node_affinity_block(node_name, zone or None)
    spec["affinity"] = affinity

    # ---- topologySpreadConstraints --------------------------------------
    spec["topologySpreadConstraints"] = _topology_spread_constraints(zone or None)

    return spec


def _patch_document(
    doc: dict[str, Any],
    plan: PlacementPlan,
) -> dict[str, Any] | None:
    """Patch a single Kubernetes YAML document.

    Returns the patched document (deep copy) if it is a patchable workload
    whose service name appears in *plan*, otherwise returns ``None``.
    """
    kind: str = doc.get("kind", "")
    if kind not in _PATCHABLE_KINDS:
        return None

    metadata: dict[str, Any] = doc.get("metadata") or {}
    svc_name: str = str(metadata.get("name", "")).lower()
    if not svc_name:
        return None

    node_name = _node_for_service(svc_name, plan)
    if node_name is None:
        return None

    zone = _zone_for_service(svc_name, plan) or ""

    patched = copy.deepcopy(doc)
    spec: dict[str, Any] = patched.get("spec") or {}
    template: dict[str, Any] = spec.get("template") or {}
    pod_spec: dict[str, Any] = template.get("spec") or {}

    new_pod_spec = _patch_pod_spec(pod_spec, node_name, zone)
    template["spec"] = new_pod_spec
    spec["template"] = template
    patched["spec"] = spec
    return patched


# ---------------------------------------------------------------------------
# YAML round-trip helpers
# ---------------------------------------------------------------------------


def _load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    """Load all YAML documents from *path*, returning only dicts."""
    with open(path) as fh:
        return [doc for doc in yaml.safe_load_all(fh) if isinstance(doc, dict)]


def _dump_yaml_documents(docs: list[dict[str, Any]]) -> str:
    """Serialise a list of YAML documents to a multi-document YAML string."""
    parts: list[str] = []
    for doc in docs:
        parts.append(yaml.dump(doc, default_flow_style=False, sort_keys=False))
    return "---\n" + "\n---\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_patch(
    path: Path,
    plan: PlacementPlan,
    topology: ClusterTopology | None = None,  # noqa: ARG001 — reserved for future use
) -> str:
    """Generate a unified diff for a single manifest file.

    Reads the YAML at *path*, applies placement patches for every patchable
    workload whose name appears in *plan*, and returns a unified diff string
    compatible with ``patch -p0`` and ``git apply``.

    Parameters
    ----------
    path:
        Path to a Kubernetes YAML manifest (may contain multiple documents).
    plan:
        Optimal placement produced by a QUBOB solver.
    topology:
        (Optional) The source ClusterTopology — reserved for future enrichment.

    Returns
    -------
    Unified diff string.  Empty string if no patches were needed.
    """
    original_docs = _load_yaml_documents(path)
    patched_docs: list[dict[str, Any]] = []
    any_patched = False

    for doc in original_docs:
        patched = _patch_document(doc, plan)
        if patched is not None:
            patched_docs.append(patched)
            any_patched = True
        else:
            patched_docs.append(doc)

    if not any_patched:
        return ""

    original_text = _dump_yaml_documents(original_docs)
    patched_text = _dump_yaml_documents(patched_docs)

    filename = str(path)
    diff_lines = list(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            patched_text.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
        )
    )
    return "\n".join(diff_lines)


def apply_patches(
    manifest_paths: list[Path],
    plan: PlacementPlan,
    topology: ClusterTopology | None = None,
    *,
    backup: bool = True,
) -> list[Path]:
    """Apply placement patches to a list of manifest files.

    For each file in *manifest_paths*:
    - Optionally create a ``<file>.bak`` backup of the original.
    - Write the patched YAML in-place.

    Parameters
    ----------
    manifest_paths:
        Paths to Kubernetes YAML manifests.
    plan:
        Optimal placement produced by a QUBOB solver.
    topology:
        (Optional) Source ClusterTopology.
    backup:
        When True (default) create ``.bak`` copies before overwriting.

    Returns
    -------
    List of ``Path`` objects for every file that was actually modified.
    """
    modified: list[Path] = []

    for path in manifest_paths:
        original_docs = _load_yaml_documents(path)
        patched_docs: list[dict[str, Any]] = []
        any_patched = False

        for doc in original_docs:
            patched = _patch_document(doc, plan)
            if patched is not None:
                patched_docs.append(patched)
                any_patched = True
            else:
                patched_docs.append(doc)

        if not any_patched:
            continue

        if backup:
            bak_path = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak_path)

        with open(path, "w") as fh:
            fh.write(_dump_yaml_documents(patched_docs))

        modified.append(path)

    return modified
