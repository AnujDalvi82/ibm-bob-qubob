"""QUBOB Interactive Visual Dashboard.

A self-contained FastAPI web application that visualises:
  1. Service dependency graph (SVG/Canvas, nodes + traffic weights)
  2. Before vs After latency heatmap (cross-zone hops eliminated)
  3. Cluster node packing bars (CPU & RAM utilisation)
  4. Live quantum convergence curves (QIEA vs GA vs Greedy)

Launch:
    qubob dashboard                  # uses demo ecommerce topology
    qubob dashboard --port 9000
    qubob dashboard --host 0.0.0.0

Or directly:
    python -m bob_optimizer.dashboard.app
"""
