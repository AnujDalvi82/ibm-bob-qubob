"""Kubernetes Deployment/StatefulSet/Service manifest parser.

Parses one or more Kubernetes YAML documents and produces a ClusterTopology.
Only workload kinds (Deployment, StatefulSet) become Service domain objects;
k8s Service documents are used solely for port-to-protocol mapping.

Notes
-----
- NodeCapacity objects are NOT populated here.  Cluster nodes come from
  kubectl node discovery or the synthetic generator.
- replicas defaults to 1 when spec.replicas is absent.
- Multi-container pods: CPU and memory are *summed* across all containers;
  individual container names are stored in Service.annotations["containers"].
- Resource demands are scaled by replica count so H_resource is realistic.
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
# Module-level defaults
# ---------------------------------------------------------------------------

_DEFAULT_CPU_M: int = 100       # millicores
_DEFAULT_MEM_MIB: int = 128     # MiB
_DEFAULT_REPLICAS: int = 1

# Regex patterns for in-cluster service URL detection in env vars
_URL_PATTERN = re.compile(r"https?://([a-z0-9][a-z0-9\-]*[a-z0-9])")
_SVC_PATTERN = re.compile(r"([a-z0-9][a-z0-9\-]*[a-z0-9])\.[a-z][a-z0-9\-]*\.svc\.cluster\.local")

_WORKLOAD_KINDS = {"Deployment", "StatefulSet"}
_IGNORED_KINDS = {"Service", "ConfigMap", "Secret", "Namespace", "ServiceAccount"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_cpu(value: str | None) -> int:
    """Convert a Kubernetes CPU string to millicores.

    Handles: ``"250m"``, ``"0.5"``, ``"1"``, ``"2"`` — and None (→ default).
    """
    if value is None:
        return _DEFAULT_CPU_M
    v = str(value).strip()
    if v.endswith("m"):
        return max(1, int(v[:-1]))
    # Fractional or whole cores
    try:
        return max(1, int(round(float(v) * 1000)))
    except ValueError:
        return _DEFAULT_CPU_M


def _parse_memory(value: str | None) -> int:
    """Convert a Kubernetes memory string to MiB.

    Handles: ``"128Mi"``, ``"1Gi"``, ``"512000000"`` (bytes), ``"512M"`` — and None.
    """
    if value is None:
        return _DEFAULT_MEM_MIB
    v = str(value).strip()
    try:
        if v.endswith("Ki"):
            return max(1, int(v[:-2]) // 1024)
        if v.endswith("Mi"):
            return max(1, int(v[:-2]))
        if v.endswith("Gi"):
            return max(1, int(v[:-2]) * 1024)
        if v.endswith("Ti"):
            return max(1, int(v[:-2]) * 1024 * 1024)
        if v.endswith("K") or v.endswith("k"):
            return max(1, int(v[:-1]) // 1024)
        if v.endswith("M"):
            return max(1, int(v[:-1]))
        if v.endswith("G"):
            return max(1, int(v[:-1]) * 1024)
        # Plain bytes
        return max(1, int(v) // (1024 * 1024))
    except (ValueError, OverflowError):
        return _DEFAULT_MEM_MIB


def _parse_resource_request(container: dict[str, Any]) -> ResourceRequest:
    """Extract CPU and memory from a single container spec."""
    resources: dict[str, Any] = container.get("resources") or {}
    requests: dict[str, Any] = resources.get("requests") or {}
    return ResourceRequest(
        cpu_millicores=_parse_cpu(requests.get("cpu")),
        memory_mib=_parse_memory(requests.get("memory")),
    )


def _parse_affinity(
    spec: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Extract affinity constraints from a pod spec.

    Returns
    -------
    required_node_labels, preferred_node_labels, anti_affinity_services
    """
    affinity: dict[str, Any] = spec.get("affinity") or {}
    required_node_labels: dict[str, str] = {}
    preferred_node_labels: dict[str, str] = {}
    anti_affinity_services: list[str] = []

    # nodeAffinity
    node_affinity: dict[str, Any] = affinity.get("nodeAffinity") or {}
    required_node = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    for term in required_node.get("nodeSelectorTerms") or []:
        for expr in term.get("matchExpressions") or []:
            key: str = expr.get("key", "")
            values: list[str] = expr.get("values") or []
            if key and values:
                required_node_labels[key] = values[0]
        for lbl, val in (term.get("matchLabels") or {}).items():
            required_node_labels[str(lbl)] = str(val)

    preferred_node_terms = node_affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []
    for pref in preferred_node_terms:
        preference: dict[str, Any] = pref.get("preference") or {}
        for expr in preference.get("matchExpressions") or []:
            key = expr.get("key", "")
            values = expr.get("values") or []
            if key and values:
                preferred_node_labels[key] = values[0]

    # podAntiAffinity → anti_affinity_services
    pod_anti: dict[str, Any] = affinity.get("podAntiAffinity") or {}
    for term in (pod_anti.get("requiredDuringSchedulingIgnoredDuringExecution") or []):
        selector: dict[str, Any] = term.get("labelSelector") or {}
        for expr in selector.get("matchExpressions") or []:
            if expr.get("key") == "app" and expr.get("values"):
                for svc_name in expr["values"]:
                    anti_affinity_services.append(str(svc_name).lower())
        for lbl, val in (selector.get("matchLabels") or {}).items():
            if str(lbl) == "app":
                anti_affinity_services.append(str(val).lower())

    return required_node_labels, preferred_node_labels, anti_affinity_services


def _infer_dependencies(
    containers: list[dict[str, Any]],
    known_service_names: set[str],
    source_name: str,
) -> list[tuple[str, str]]:
    """Scan env vars for in-cluster URL patterns and return (source, target) pairs."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for container in containers:
        env_list: list[dict[str, Any]] = container.get("env") or []
        for entry in env_list:
            val = str(entry.get("value") or "")
            for m in _URL_PATTERN.finditer(val):
                target = m.group(1).lower()
                if target in known_service_names and target != source_name and target not in seen:
                    pairs.append((source_name, target))
                    seen.add(target)
            for m in _SVC_PATTERN.finditer(val):
                target = m.group(1).lower()
                if target in known_service_names and target != source_name and target not in seen:
                    pairs.append((source_name, target))
                    seen.add(target)
    return pairs


def _workload_to_service(doc: dict[str, Any], known_names: set[str]) -> Service:
    """Convert a Deployment or StatefulSet document to a Service domain object."""
    metadata: dict[str, Any] = doc.get("metadata") or {}
    spec: dict[str, Any] = doc.get("spec") or {}
    name: str = str(metadata.get("name", "unknown")).lower()
    namespace: str = str(metadata.get("namespace", "default"))
    labels: dict[str, str] = {str(k): str(v) for k, v in (metadata.get("labels") or {}).items()}
    annotations_raw: dict[str, str] = {
        str(k): str(v) for k, v in (metadata.get("annotations") or {}).items()
    }
    replicas: int = int(spec.get("replicas") or _DEFAULT_REPLICAS)

    template_spec: dict[str, Any] = (spec.get("template") or {}).get("spec") or {}
    containers: list[dict[str, Any]] = template_spec.get("containers") or []

    # Sum resources across all containers
    total_cpu = sum(_parse_resource_request(c).cpu_millicores for c in containers) or _DEFAULT_CPU_M
    total_mem = sum(_parse_resource_request(c).memory_mib for c in containers) or _DEFAULT_MEM_MIB

    # Scale by replicas so demand reflects total workload load
    scaled_cpu = total_cpu * replicas
    scaled_mem = total_mem * replicas

    # Collect port annotations
    ports: list[str] = []
    for c in containers:
        for p in c.get("ports") or []:
            if "containerPort" in p:
                ports.append(str(p["containerPort"]))
    if ports:
        annotations_raw["ports"] = ",".join(ports)
    if len(containers) > 1:
        annotations_raw["containers"] = ",".join(
            str(c.get("name", f"container-{i}")) for i, c in enumerate(containers)
        )

    req_node_labels, pref_node_labels, anti_affinity = _parse_affinity(template_spec)

    return Service(
        name=name,
        image=str((containers[0].get("image") if containers else None) or "unknown:latest"),
        replicas=replicas,
        resources=ResourceRequest(cpu_millicores=scaled_cpu, memory_mib=scaled_mem),
        labels=labels,
        annotations=annotations_raw,
        namespace=namespace,
        required_node_labels=req_node_labels,
        preferred_node_labels=pref_node_labels,
        anti_affinity_services=anti_affinity,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_manifests(
    paths: list[Path],
    *,
    cluster_name: str = "k8s-cluster",
) -> ClusterTopology:
    """Parse one or more Kubernetes YAML manifest files into a ClusterTopology.

    Parameters
    ----------
    paths:
        List of paths to YAML files (each may contain multiple documents).
    cluster_name:
        Human-readable name for the resulting topology.

    Returns
    -------
    ClusterTopology with populated services and dependencies; nodes tuple is
    empty — callers must supply node capacity separately.
    """
    all_docs: list[dict[str, Any]] = []
    for p in paths:
        with open(p) as fh:
            for doc in yaml.safe_load_all(fh):
                if isinstance(doc, dict):
                    all_docs.append(doc)

    # First pass: collect workload names
    known_names: set[str] = set()
    for doc in all_docs:
        kind = doc.get("kind", "")
        if kind in _WORKLOAD_KINDS:
            raw_name = str((doc.get("metadata") or {}).get("name", "")).lower()
            if raw_name:
                known_names.add(raw_name)

    # Second pass: build Service objects and infer dependencies
    services: list[Service] = []
    raw_deps: list[tuple[str, str]] = []

    for doc in all_docs:
        kind = doc.get("kind", "")
        if kind in _WORKLOAD_KINDS:
            svc = _workload_to_service(doc, known_names)
            services.append(svc)
            spec: dict[str, Any] = doc.get("spec") or {}
            template_spec: dict[str, Any] = (spec.get("template") or {}).get("spec") or {}
            containers: list[dict[str, Any]] = template_spec.get("containers") or []
            raw_deps.extend(_infer_dependencies(containers, known_names, svc.name))
        elif kind not in _IGNORED_KINDS and kind:
            # Unknown kind — skip silently (no warning in library code)
            pass

    # Deduplicate dependencies
    seen_edges: set[tuple[str, str]] = set()
    deps: list[ServiceDependency] = []
    for source, target in raw_deps:
        if (source, target) not in seen_edges:
            deps.append(ServiceDependency(source=source, target=target, calls_per_second=1.0))
            seen_edges.add((source, target))

    return ClusterTopology(
        name=cluster_name,
        services=tuple(services),
        nodes=(),
        dependencies=tuple(deps),
    )
