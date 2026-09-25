"""Docker Compose v2/v3 manifest parser.

Parses a docker-compose.yml and produces a ClusterTopology.

Notes
-----
- NodeCapacity objects are NOT populated here.
- replicas is taken from ``deploy.replicas``; defaults to 1 if absent.
- dependencies are inferred from ``depends_on`` (list or dict form) AND from
  environment variable URL scanning; duplicates are deduplicated.
- Resource demands are scaled by replica count (same convention as kubernetes.py).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from bob_optimizer.model.domain import (
    ClusterTopology,
    ResourceRequest,
    Service,
    ServiceDependency,
)

# ---------------------------------------------------------------------------
# Module-level defaults (identical to kubernetes.py by design)
# ---------------------------------------------------------------------------

_DEFAULT_CPU_M: int = 100    # millicores
_DEFAULT_MEM_MIB: int = 128  # MiB
_DEFAULT_REPLICAS: int = 1

# Shared URL-pattern regex (same as kubernetes.py)
_URL_PATTERN = re.compile(r"https?://([a-z0-9][a-z0-9\-]*[a-z0-9])")
_SVC_PATTERN = re.compile(
    r"([a-z0-9][a-z0-9\-]*[a-z0-9])\.[a-z][a-z0-9\-]*\.svc\.cluster\.local"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_compose_cpu(value: Any) -> int:
    """Convert a Compose CPU value to millicores.

    Compose uses fractional cores: ``"0.5"``, ``0.25``, ``"1"``.
    Falls back to ``_DEFAULT_CPU_M`` for None or unparseable values.
    """
    if value is None:
        return _DEFAULT_CPU_M
    try:
        return max(1, int(round(float(str(value)) * 1000)))
    except (ValueError, TypeError):
        return _DEFAULT_CPU_M


def _parse_compose_memory(value: Any) -> int:
    """Convert a Compose memory value to MiB.

    Handles: ``"128m"`` (MB), ``"1g"`` (GB), ``"512000000"`` (bytes), int bytes.
    Falls back to ``_DEFAULT_MEM_MIB`` for None or unparseable values.
    """
    if value is None:
        return _DEFAULT_MEM_MIB
    v = str(value).strip().lower()
    try:
        if v.endswith("g"):
            return max(1, int(float(v[:-1]) * 1024))
        if v.endswith("gb"):
            return max(1, int(float(v[:-2]) * 1024))
        if v.endswith("m"):
            return max(1, int(float(v[:-1])))
        if v.endswith("mb"):
            return max(1, int(float(v[:-2])))
        if v.endswith("k") or v.endswith("kb"):
            suffix_len = 2 if v.endswith("kb") else 1
            return max(1, int(float(v[:-suffix_len])) // 1024)
        # Plain integer treated as bytes
        return max(1, int(float(v)) // (1024 * 1024))
    except (ValueError, OverflowError):
        return _DEFAULT_MEM_MIB


def _extract_env_url_targets(
    env: list[Any] | dict[str, Any] | None,
    known: set[str],
    source: str,
) -> set[str]:
    """Scan environment block for in-cluster service references.

    Parameters
    ----------
    env:
        Either a list of ``"KEY=VALUE"`` strings / ``{name: ..., value: ...}`` dicts,
        or a mapping ``{KEY: VALUE}``.
    known:
        Set of known service names to match against.
    source:
        Name of the calling service (excluded from results).

    Returns
    -------
    Set of target service names found in env vars.
    """
    targets: set[str] = set()
    values: list[str] = []

    if isinstance(env, dict):
        values = [str(v) for v in env.values() if v is not None]
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, str):
                # "KEY=VALUE" form
                if "=" in item:
                    values.append(item.split("=", 1)[1])
            elif isinstance(item, dict):
                val = item.get("value")
                if val is not None:
                    values.append(str(val))

    for val in values:
        for m in _URL_PATTERN.finditer(val):
            t = m.group(1).lower()
            if t in known and t != source:
                targets.add(t)
        for m in _SVC_PATTERN.finditer(val):
            t = m.group(1).lower()
            if t in known and t != source:
                targets.add(t)

    return targets


def _depends_on_names(depends_on: Any) -> list[str]:
    """Extract service names from ``depends_on`` in list or dict form."""
    if depends_on is None:
        return []
    if isinstance(depends_on, list):
        return [str(d).lower() for d in depends_on]
    if isinstance(depends_on, dict):
        return [str(k).lower() for k in depends_on]
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_compose(
    path: Path,
    *,
    cluster_name: str = "compose-cluster",
) -> ClusterTopology:
    """Parse a docker-compose.yml file into a ClusterTopology.

    Parameters
    ----------
    path:
        Path to the docker-compose.yml (or .yaml) file.
    cluster_name:
        Human-readable name for the resulting topology.

    Returns
    -------
    ClusterTopology with populated services and dependencies; nodes tuple is
    empty — callers must supply node capacity separately.
    """
    with open(path) as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    compose_services: dict[str, Any] = raw.get("services") or {}

    # First pass: collect all service names
    known_names: set[str] = {str(k).lower() for k in compose_services}

    services: list[Service] = []
    raw_deps: list[tuple[str, str]] = []

    for svc_key, svc_cfg in compose_services.items():
        name: str = str(svc_key).lower()
        cfg: dict[str, Any] = svc_cfg or {}

        image_val = cfg.get("image")
        image: str = str(image_val) if image_val else f"build:{name}"

        deploy: dict[str, Any] = cfg.get("deploy") or {}
        replicas: int = int(deploy.get("replicas") or _DEFAULT_REPLICAS)

        resources_cfg: dict[str, Any] = deploy.get("resources") or {}
        limits: dict[str, Any] = resources_cfg.get("limits") or {}
        cpu_requests_cfg: dict[str, Any] = resources_cfg.get("reservations") or {}

        # Prefer limits over reservations for sizing (conservative)
        cpu_raw = limits.get("cpus") or cpu_requests_cfg.get("cpus")
        mem_raw = limits.get("memory") or cpu_requests_cfg.get("memory")

        cpu_m = _parse_compose_cpu(cpu_raw)
        mem_mib = _parse_compose_memory(mem_raw)

        # Scale by replicas
        scaled_cpu = cpu_m * replicas
        scaled_mem = mem_mib * replicas

        # Network membership → label
        labels: dict[str, str] = {}
        networks = cfg.get("networks")
        if isinstance(networks, list) and networks:
            labels["compose.network"] = str(networks[0]).lower()
        elif isinstance(networks, dict):
            first_net = next(iter(networks), None)
            if first_net:
                labels["compose.network"] = str(first_net).lower()

        services.append(
            Service(
                name=name,
                image=image,
                replicas=replicas,
                resources=ResourceRequest(cpu_millicores=scaled_cpu, memory_mib=scaled_mem),
                labels=labels,
            )
        )

        # depends_on edges
        for target in _depends_on_names(cfg.get("depends_on")):
            if target in known_names and target != name:
                raw_deps.append((name, target))

        # env-URL edges
        for target in _extract_env_url_targets(cfg.get("environment"), known_names, name):
            raw_deps.append((name, target))

    # Deduplicate dependency edges
    seen_edges: set[tuple[str, str]] = set()
    deps: list[ServiceDependency] = []
    for source, target in raw_deps:
        if (source, target) not in seen_edges and target in known_names:
            deps.append(ServiceDependency(source=source, target=target, calls_per_second=1.0))
            seen_edges.add((source, target))

    return ClusterTopology(
        name=cluster_name,
        services=tuple(services),
        nodes=(),
        dependencies=tuple(deps),
    )
