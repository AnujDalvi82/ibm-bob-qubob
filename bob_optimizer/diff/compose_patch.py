"""Docker Compose placement constraint patch generator.

Takes a ``PlacementPlan`` produced by a QUBOB solver and updates Docker
Compose YAML files with node placement constraints and explicit network
links that reflect the optimal service co-location.

Specifically, for each service in the Compose file whose name appears in the
``PlacementPlan``, the patch injects or merges:

  - ``services.<name>.deploy.placement.constraints`` — list of
    ``"node.hostname == <node_name>"`` and (when a zone is known)
    ``"node.labels.zone == <zone>"`` constraints compatible with
    Docker Swarm and Compose v3 deploy specs.
  - ``services.<name>.deploy.labels`` — adds ``qubob.node`` and
    ``qubob.zone`` labels for observability.

Produces standard unified diff output and supports safe in-place patching
with ``.bak`` backups.

Public API
----------
generate_compose_patch  : (path, plan) → unified diff string
apply_compose_patches   : (paths, plan, backup) → list[Path] written
"""

from __future__ import annotations

import copy
import difflib
import shutil
from pathlib import Path
from typing import Any

import yaml

from bob_optimizer.model.domain import PlacementPlan


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _node_for_service(service_name: str, plan: PlacementPlan) -> str | None:
    """Return the assigned node name for *service_name*, or None."""
    for a in plan.assignments:
        if a.service_name == service_name:
            return a.node_name
    return None


def _zone_for_service(service_name: str, plan: PlacementPlan) -> str | None:
    """Return the assigned zone for *service_name*, or None."""
    for a in plan.assignments:
        if a.service_name == service_name:
            return a.zone or None
    return None


def _build_placement_constraints(node_name: str, zone: str | None) -> list[str]:
    """Build Compose placement constraint strings.

    Parameters
    ----------
    node_name:
        Target Docker Swarm node hostname.
    zone:
        Target availability zone label (optional).

    Returns
    -------
    List of constraint strings compatible with ``deploy.placement.constraints``.
    """
    constraints: list[str] = [f"node.hostname == {node_name}"]
    if zone:
        constraints.append(f"node.labels.zone == {zone}")
    return constraints


def _patch_compose_service(
    svc_cfg: dict[str, Any],
    node_name: str,
    zone: str | None,
) -> dict[str, Any]:
    """Return a deep-copied service config with placement constraints injected.

    Parameters
    ----------
    svc_cfg:
        The service definition dict from the Compose file.
    node_name:
        Target node hostname.
    zone:
        Target zone label (may be None).

    Returns
    -------
    New dict — original is never mutated.
    """
    cfg = copy.deepcopy(svc_cfg)

    deploy: dict[str, Any] = copy.deepcopy(cfg.get("deploy") or {})

    # Inject placement.constraints
    placement: dict[str, Any] = copy.deepcopy(deploy.get("placement") or {})
    placement["constraints"] = _build_placement_constraints(node_name, zone)
    deploy["placement"] = placement

    # Inject observability labels
    labels: dict[str, str] | list[str] = deploy.get("labels") or {}
    if isinstance(labels, list):
        # Convert list form to dict for easy merging
        label_dict: dict[str, str] = {}
        for item in labels:
            if "=" in str(item):
                k, v = str(item).split("=", 1)
                label_dict[k.strip()] = v.strip()
        labels = label_dict
    else:
        labels = dict(labels)

    labels["qubob.node"] = node_name
    if zone:
        labels["qubob.zone"] = zone
    deploy["labels"] = labels

    cfg["deploy"] = deploy
    return cfg


def _load_compose(path: Path) -> dict[str, Any]:
    """Load a Compose file and return its parsed dict."""
    with open(path) as fh:
        result: dict[str, Any] = yaml.safe_load(fh) or {}
    return result


def _dump_compose(data: dict[str, Any]) -> str:
    """Serialise a Compose dict back to a YAML string."""
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_compose_patch(
    path: Path,
    plan: PlacementPlan,
) -> str:
    """Generate a unified diff for a single Docker Compose file.

    Reads the YAML at *path*, applies placement constraints for every service
    whose name appears in *plan*, and returns a unified diff string.

    Parameters
    ----------
    path:
        Path to a ``docker-compose.yml`` (or ``.yaml``) file.
    plan:
        Optimal placement produced by a QUBOB solver.

    Returns
    -------
    Unified diff string.  Empty string if no patches were needed.
    """
    original_data = _load_compose(path)
    patched_data = copy.deepcopy(original_data)

    compose_services: dict[str, Any] = patched_data.get("services") or {}
    any_patched = False

    for svc_name, svc_cfg in compose_services.items():
        name_lower = str(svc_name).lower()
        node_name = _node_for_service(name_lower, plan)
        if node_name is None:
            continue
        zone = _zone_for_service(name_lower, plan)
        compose_services[svc_name] = _patch_compose_service(
            svc_cfg or {}, node_name, zone
        )
        any_patched = True

    if not any_patched:
        return ""

    original_text = _dump_compose(original_data)
    patched_text = _dump_compose(patched_data)

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


def apply_compose_patches(
    paths: list[Path],
    plan: PlacementPlan,
    *,
    backup: bool = True,
) -> list[Path]:
    """Apply placement patches to a list of Docker Compose files.

    For each file in *paths*:
    - Optionally creates a ``<file>.bak`` backup of the original.
    - Writes the patched YAML in-place.

    Parameters
    ----------
    paths:
        Paths to docker-compose YAML files.
    plan:
        Optimal placement produced by a QUBOB solver.
    backup:
        When True (default) create ``.bak`` copies before overwriting.

    Returns
    -------
    List of ``Path`` objects for every file that was actually modified.
    """
    modified: list[Path] = []

    for path in paths:
        original_data = _load_compose(path)
        patched_data = copy.deepcopy(original_data)

        compose_services: dict[str, Any] = patched_data.get("services") or {}
        any_patched = False

        for svc_name, svc_cfg in compose_services.items():
            name_lower = str(svc_name).lower()
            node_name = _node_for_service(name_lower, plan)
            if node_name is None:
                continue
            zone = _zone_for_service(name_lower, plan)
            compose_services[svc_name] = _patch_compose_service(
                svc_cfg or {}, node_name, zone
            )
            any_patched = True

        if not any_patched:
            continue

        if backup:
            bak_path = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak_path)

        with open(path, "w") as fh:
            fh.write(_dump_compose(patched_data))

        modified.append(path)

    return modified
