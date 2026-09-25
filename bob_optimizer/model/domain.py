"""Foundation domain models for QUBOB.

All models are immutable Pydantic v2 dataclasses (frozen=True) unless noted.
They form the shared vocabulary across parsers, the QUBO engine, solvers, and
the diff generator.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Resource primitives
# ---------------------------------------------------------------------------


class ResourceRequest(BaseModel):
    """CPU and memory resource request/limit for a single container."""

    model_config = ConfigDict(frozen=True)

    cpu_millicores: Annotated[int, Field(gt=0, description="CPU in millicores, e.g. 250 = 0.25 CPU")] = 100
    memory_mib: Annotated[int, Field(gt=0, description="Memory in MiB, e.g. 256")] = 128

    @property
    def cpu_cores(self) -> float:
        """Fractional CPU cores."""
        return self.cpu_millicores / 1000.0


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Service(BaseModel):
    """A single deployable microservice unit.

    Corresponds to a Kubernetes Deployment or a Docker Compose service entry.
    """

    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Field(min_length=1, description="Unique service identifier (k8s name or compose key)")]
    image: Annotated[str, Field(min_length=1, description="Container image reference, e.g. 'nginx:1.25'")] = "unknown:latest"
    replicas: Annotated[int, Field(ge=1, description="Desired replica count")] = 1
    resources: ResourceRequest = Field(default_factory=ResourceRequest)
    labels: dict[str, str] = Field(default_factory=dict, description="Kubernetes-style label map")
    annotations: dict[str, str] = Field(default_factory=dict, description="Arbitrary annotation map")
    namespace: str = Field(default="default", description="Kubernetes namespace")

    # Affinity hints parsed from k8s podAffinity/podAntiAffinity rules
    required_node_labels: dict[str, str] = Field(
        default_factory=dict,
        description="Node label selectors that MUST match (hard affinity)",
    )
    preferred_node_labels: dict[str, str] = Field(
        default_factory=dict,
        description="Node label selectors that SHOULD match (soft affinity)",
    )
    anti_affinity_services: list[str] = Field(
        default_factory=list,
        description="Service names that must NOT share a node with this service",
    )

    @field_validator("name")
    @classmethod
    def _name_no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError("Service name must not contain spaces")
        return v.lower()


# ---------------------------------------------------------------------------
# Node capacity
# ---------------------------------------------------------------------------


class NodeCapacity(BaseModel):
    """Physical or virtual Kubernetes node with available capacity.

    Capacity values represent *allocatable* resources (after OS/system pods).
    """

    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Field(min_length=1, description="Unique node name (kubectl get nodes)")]
    zone: str = Field(default="zone-a", description="Availability zone, e.g. 'us-east-1a'")
    region: str = Field(default="us-east-1", description="Cloud region")
    allocatable_cpu_millicores: Annotated[int, Field(gt=0)] = 4000
    allocatable_memory_mib: Annotated[int, Field(gt=0)] = 8192
    labels: dict[str, str] = Field(default_factory=dict, description="Node labels")

    # Measured round-trip latency to every other node (node_name → ms).
    # Populated by the parser from ping probes or cloud provider metadata.
    latency_ms: dict[str, float] = Field(
        default_factory=dict,
        description="Measured latency (ms) to peer nodes keyed by node name",
    )

    def latency_to(self, other: NodeCapacity) -> float:
        """Return measured latency to *other* node (ms).

        Falls back to 0.5 ms for same-node, or 5.0 ms if unmeasured (cross-zone
        pessimistic estimate).
        """
        if other.name == self.name:
            return 0.0
        return self.latency_ms.get(other.name, 5.0 if other.zone != self.zone else 0.5)

    @field_validator("name")
    @classmethod
    def _name_no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError("Node name must not contain spaces")
        return v.lower()


# ---------------------------------------------------------------------------
# Service dependency edge
# ---------------------------------------------------------------------------


class ServiceDependency(BaseModel):
    """A directed weighted call edge between two services.

    The weight captures the *relative* call frequency; solvers multiply it by
    the node-to-node latency to compute the latency cost for a placement.
    """

    model_config = ConfigDict(frozen=True)

    source: Annotated[str, Field(min_length=1, description="Calling service name")]
    target: Annotated[str, Field(min_length=1, description="Called service name")]
    calls_per_second: Annotated[float, Field(ge=0.0, description="Average RPS on this edge")] = 1.0
    protocol: str = Field(default="http", description="Transport protocol (http, grpc, tcp, …)")

    @model_validator(mode="after")
    def _no_self_loop(self) -> "ServiceDependency":
        if self.source == self.target:
            raise ValueError(f"Self-loop dependency on service '{self.source}' is not allowed")
        return self

    @property
    def weight(self) -> float:
        """Normalised edge weight used in QUBO cost matrix (same as calls_per_second)."""
        return self.calls_per_second


# ---------------------------------------------------------------------------
# Cluster topology
# ---------------------------------------------------------------------------


class ClusterTopology(BaseModel):
    """Complete description of the cluster and workload to optimise.

    This is the primary input to the QUBO assembler and all solvers.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(default="cluster", description="Human-readable cluster identifier")
    services: tuple[Service, ...] = Field(default_factory=tuple, description="All services to place")
    nodes: tuple[NodeCapacity, ...] = Field(default_factory=tuple, description="All candidate nodes")
    dependencies: tuple[ServiceDependency, ...] = Field(
        default_factory=tuple,
        description="Directed call-graph edges between services",
    )

    # QUBO weight hyper-parameters (λ values from the spec §5.2)
    lambda_latency: float = Field(default=1.0, ge=0.0, description="Weight for latency term H_latency")
    lambda_resource: float = Field(default=10.0, ge=0.0, description="Weight for resource term H_resource")
    lambda_affinity: float = Field(default=5.0, ge=0.0, description="Weight for affinity term H_affinity")
    lambda_penalty: float = Field(default=100.0, ge=0.0, description="Weight for one-hot penalty H_penalty")

    @model_validator(mode="after")
    def _validate_dependency_refs(self) -> "ClusterTopology":
        service_names = {s.name for s in self.services}
        for dep in self.dependencies:
            if dep.source not in service_names:
                raise ValueError(
                    f"Dependency source '{dep.source}' is not in the services list"
                )
            if dep.target not in service_names:
                raise ValueError(
                    f"Dependency target '{dep.target}' is not in the services list"
                )
        return self

    @property
    def n_services(self) -> int:
        """Number of services."""
        return len(self.services)

    @property
    def n_nodes(self) -> int:
        """Number of candidate nodes."""
        return len(self.nodes)

    @property
    def n_variables(self) -> int:
        """Total number of binary QUBO variables (N_services × N_nodes)."""
        return self.n_services * self.n_nodes

    def service_index(self, name: str) -> int:
        """Return the integer index of *name* in the services tuple."""
        for i, s in enumerate(self.services):
            if s.name == name:
                return i
        raise KeyError(f"Service '{name}' not found in topology")

    def node_index(self, name: str) -> int:
        """Return the integer index of *name* in the nodes tuple."""
        for i, n in enumerate(self.nodes):
            if n.name == name:
                return i
        raise KeyError(f"Node '{name}' not found in topology")

    def variable_index(self, service_name: str, node_name: str) -> int:
        """Return the flat QUBO variable index for (service, node) pair.

        Variable ordering: x_{i,j} → i * n_nodes + j
        """
        return self.service_index(service_name) * self.n_nodes + self.node_index(node_name)


# ---------------------------------------------------------------------------
# Placement plan (solver output)
# ---------------------------------------------------------------------------


class ServiceAssignment(BaseModel):
    """Assignment of a single service to a node."""

    model_config = ConfigDict(frozen=True)

    service_name: str = Field(description="Name of the assigned service")
    node_name: str = Field(description="Name of the target node")
    zone: str = Field(default="", description="Zone of the target node (denormalised for convenience)")


class PlacementPlan(BaseModel):
    """Full placement plan produced by a solver.

    Maps every service to exactly one node and records the associated cost
    components so the diff generator and explain command can reason about it.
    """

    model_config = ConfigDict(frozen=True)

    assignments: tuple[ServiceAssignment, ...] = Field(
        description="One assignment per service"
    )
    total_cost: float = Field(description="H_total evaluated at this placement")
    latency_cost: float = Field(default=0.0, description="H_latency component")
    resource_cost: float = Field(default=0.0, description="H_resource component")
    affinity_cost: float = Field(default=0.0, description="H_affinity component")
    penalty_cost: float = Field(default=0.0, description="H_penalty component (should be ~0 for feasible plans)")
    solver_name: str = Field(default="unknown", description="Solver that produced this plan")
    solve_time_s: float = Field(default=0.0, ge=0.0, description="Wall-clock solver time (seconds)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Solver-specific diagnostics")

    @model_validator(mode="after")
    def _unique_services(self) -> "PlacementPlan":
        names = [a.service_name for a in self.assignments]
        if len(names) != len(set(names)):
            raise ValueError("PlacementPlan contains duplicate service assignments")
        return self

    @property
    def is_feasible(self) -> bool:
        """True when the one-hot penalty is essentially zero (all services placed)."""
        return abs(self.penalty_cost) < 1e-6

    def node_for(self, service_name: str) -> str:
        """Return the node name assigned to *service_name*."""
        for a in self.assignments:
            if a.service_name == service_name:
                return a.node_name
        raise KeyError(f"Service '{service_name}' has no assignment in this plan")

    def services_on_node(self, node_name: str) -> list[str]:
        """Return all service names assigned to *node_name*."""
        return [a.service_name for a in self.assignments if a.node_name == node_name]
