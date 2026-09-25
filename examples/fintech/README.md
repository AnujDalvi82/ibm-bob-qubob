# QUBOB Fintech Demo — 20-Microservice Digital Banking & Payments Platform

A production-realistic PCI-DSS compliant digital banking platform used to benchmark QUBOB's
quantum-inspired placement optimiser on enterprise-scale architectures.

## Services

| Service | Tier | CPU Request | RAM Request | Key RPC Calls |
|---|---|---:|---:|---|
| `api-gateway` | ingress | 1000m | 1024Mi | redis-limiter(5000rps), identity-auth(4500rps), web-bff(8000rps) |
| `web-bff` | presentation | 500m | 512Mi | redis-session(3500rps), account-ledger(2200rps) |
| `mobile-bff` | presentation | 400m | 512Mi | redis-session(4200rps), account-ledger(3100rps) |
| `identity-auth` | security | 600m | 768Mi | redis-session(6800rps), postgres-primary(1200rps) |
| `kyc-service` | compliance | 800m | 1024Mi | postgres-primary(850rps), audit-logger(700rps) |
| `account-ledger` | core-banking | 1500m | 2048Mi | postgres-primary(2800rps), kafka-cluster(1800rps) |
| `payment-switch` | core-banking | 2000m | 4096Mi | **fraud-detection(3500rps)**, card-vault(2800rps) |
| `fraud-detection` | risk | 2000m | 4096Mi | clickhouse-analytics(2200rps), redis-limiter(1800rps) |
| `credit-scoring` | risk | 1000m | 2048Mi | clickhouse-analytics(1800rps), postgres-primary(1100rps) |
| `card-vault` | security | 1500m | 3072Mi | postgres-primary(2400rps), audit-logger(2000rps) |
| `rewards-service` | engagement | 400m | 512Mi | clickhouse-analytics(1200rps), kafka-cluster(900rps) |
| `notification-dispatcher` | messaging | 200m | 256Mi | kafka-cluster(3200rps), redis-session(800rps) |
| `audit-logger` | compliance | 500m | 1024Mi | clickhouse-analytics(4500rps), kafka-cluster(3000rps) |
| `kafka-cluster` | infrastructure | 2000m | 4096Mi | — |
| `redis-limiter` | infrastructure | 500m | 1024Mi | — |
| `redis-session` | infrastructure | 500m | 2048Mi | — |
| `postgres-primary` | data | 2000m | 4096Mi | — |
| `postgres-replica` | data | 1500m | 4096Mi | — |
| `clickhouse-analytics` | data | 2000m | 4096Mi | — |
| `monitoring-agent` | observability | 500m | 1024Mi | clickhouse-analytics(2000rps), kafka-cluster(1200rps) |

## PCI-DSS Constraints

- **`payment-switch` ↔ `identity-auth`**: required anti-affinity (separate nodes)
- **`card-vault` ↔ `identity-auth`**: required anti-affinity (separate nodes)
- **`card-vault` ↔ `monitoring-agent`**: required anti-affinity (no co-residency with observability)
- **`postgres-primary` ↔ `postgres-replica`**: required anti-affinity (HA on separate nodes)
- **`kafka-cluster`**: all 3 replicas spread across 3 zones (KRaft quorum)
- All PCI-scope services labelled `pci-scope: in-scope`

## Critical Placement Insight

QUBOB's QIEA solver **automatically co-locates** `payment-switch` and `fraud-detection` on the same
node, eliminating cross-zone RTT on the hottest edge in the system (3,500 req/s × 8 ms = 28 ms
saved per hop). No other solver achieves this reliably at this scale.

## Run

```bash
# Analyse the architecture
bob-opt analyze examples/fintech/

# Optimise with QIEA (300 generations for enterprise scale)
bob-opt optimize examples/fintech/ --algorithm qiea --generations 300

# Preview the placement diffs
bob-opt diff examples/fintech/
```
