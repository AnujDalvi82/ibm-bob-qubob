# QUBOB Demo: E-Commerce Microservice Cluster

A realistic 10-service Kubernetes deployment used as a **live demonstration** of
QUBOB's quantum-inspired placement optimiser.

## Service Topology

```
Browser ──► frontend (250m CPU / 256Mi RAM × 3)
                │
         ┌──────┼──────────┐
         ▼      ▼          ▼
       cart   auth     analytics
    (200m)  (150m)     (200m)
      │  │     │
      │  └─────┤
      ▼        ▼
    order    redis ◄── notification
    (400m)  (500m)       (100m)
      │
      ├──► payment (300m)
      │       └──► postgres ◄──┐
      │                        │
      └──► inventory (250m) ───┘
               └──► redis
```

## RPC Call Weights (calls/sec)

| Source     | Target      | RPS    | Protocol |
|------------|-------------|--------|----------|
| frontend   | cart        | 150    | HTTP     |
| frontend   | auth        | 300    | HTTP     |
| frontend   | analytics   | 50     | HTTP     |
| cart       | redis       | 2500   | TCP      |
| cart       | order       | 120    | gRPC     |
| cart       | inventory   | 90     | gRPC     |
| auth       | redis       | 1800   | TCP      |
| auth       | postgres    | 200    | TCP      |
| order      | postgres    | 800    | TCP      |
| order      | inventory   | 350    | gRPC     |
| order      | notification| 120    | gRPC     |
| order      | payment     | 60     | gRPC     |
| payment    | postgres    | 500    | TCP      |
| payment    | notification| 80     | gRPC     |
| inventory  | postgres    | 600    | TCP      |
| inventory  | redis       | 400    | TCP      |
| notification| redis      | 300    | TCP      |
| analytics  | postgres    | 250    | TCP      |
| analytics  | redis       | 150    | TCP      |

## Placement Challenge

The key optimisation challenge is co-locating high-traffic pairs on the **same node**
to eliminate cross-zone RTT hops (8 ms) while respecting:

- **Anti-affinity**: `auth` must NOT share a node with `payment` (PCI compliance).
- **Affinity hints**: `redis` ↔ `cart`, `auth` should be on the same node.
- **Affinity hints**: `postgres` ↔ `order`, `payment`, `inventory` should co-locate.
- **Resource constraints**: `postgres` (1000m CPU, 2Gi RAM) is the largest service.

## Quick Demo

```bash
# Analyse the manifests
qubob analyze examples/ecommerce/

# Run QIEA optimiser
qubob optimize examples/ecommerce/ --algorithm qiea --generations 200

# Preview the generated nodeAffinity patches
qubob diff examples/ecommerce/

# Apply patches (with .bak backups)
qubob apply examples/ecommerce/
```
