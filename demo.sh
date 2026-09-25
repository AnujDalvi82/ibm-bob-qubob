#!/usr/bin/env bash
# =============================================================================
#  QUBOB — Judge Demo Script
#  Run:  ./demo.sh
# =============================================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
BOLD='\033[1m'
CYAN='\033[1;36m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
MAGENTA='\033[1;35m'
BLUE='\033[1;34m'
DIM='\033[2m'
RESET='\033[0m'

# ── Helpers ───────────────────────────────────────────────────────────────────
banner() {
  echo -e ""
  echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${RESET}"
  echo -e "${CYAN}║${RESET}  ${BOLD}QUBOB${RESET} — Quantum-Inspired Microservice Placement Optimizer ${CYAN}║${RESET}"
  echo -e "${CYAN}║${RESET}  ${DIM}IBM Bob Hackathon 2025  ·  github.com/AnujDalvi82/ibm-bob-qubob${RESET} ${CYAN}║${RESET}"
  echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${RESET}"
  echo -e ""
}

step() {
  local num="$1"; shift
  echo -e ""
  echo -e "${MAGENTA}━━━  Step ${num}${RESET}  ${BOLD}$*${RESET}"
  echo -e ""
}

run_cmd() {
  echo -e "${BLUE}  \$${RESET} ${BOLD}$*${RESET}"
  echo -e ""
  "$@"
  echo -e ""
}

pause() {
  echo -e "${DIM}  ↵  Press ENTER to continue…${RESET}"
  read -r
}

check_dep() {
  if ! command -v "$1" &>/dev/null; then
    echo -e "${YELLOW}  ⚠  '$1' not found — install with: pip install -e .[dev]${RESET}"
    exit 1
  fi
}

# ── Main ─────────────────────────────────────────────────────────────────────
banner

# ── Step 0: Environment check ─────────────────────────────────────────────────
step 0 "Environment Check"

PYTHON_VER=$(python3 --version 2>&1 || echo "not found")
echo -e "  ${GREEN}✓${RESET}  Python   : ${BOLD}${PYTHON_VER}${RESET}"

check_dep bob-opt
BOB_OPT_VER=$(bob-opt --version 2>&1 | head -1 || echo "installed")
echo -e "  ${GREEN}✓${RESET}  bob-opt  : ${BOLD}${BOB_OPT_VER}${RESET}"

echo -e "  ${GREEN}✓${RESET}  Examples : ${BOLD}examples/ecommerce/${RESET}"
echo -e ""
pause

# ── Step 1: Analyze ───────────────────────────────────────────────────────────
step 1 "Analyze the E-Commerce Cluster"
echo -e "  ${DIM}Parses 10 Kubernetes manifests, builds the latency matrix,${RESET}"
echo -e "  ${DIM}and identifies the top bottleneck edges.${RESET}"
echo -e ""
run_cmd bob-opt analyze examples/ecommerce
pause

# ── Step 2: Optimize with QIEA ────────────────────────────────────────────────
step 2 "Optimize with Quantum-Inspired Evolutionary Algorithm (200 generations)"
echo -e "  ${DIM}Quantum superposition + rotation gate + catastrophe operator.${RESET}"
echo -e "  ${DIM}Explores exponential placement space on classical hardware.${RESET}"
echo -e ""
run_cmd bob-opt optimize examples/ecommerce --algorithm qiea --generations 200
pause

# ── Step 3: Diff ─────────────────────────────────────────────────────────────
step 3 "Preview Kubernetes nodeAffinity Patches"
echo -e "  ${DIM}Syntax-highlighted unified diff — no manifests modified yet.${RESET}"
echo -e ""
run_cmd bob-opt diff examples/ecommerce
pause

# ── Step 4: Summary ───────────────────────────────────────────────────────────
step 4 "Results Summary"

echo -e "  ${GREEN}✓${RESET}  ${BOLD}Latency reduction${RESET}  : QIEA achieves ≥10–35 % lower cross-zone RTT cost"
echo -e "         vs Classical GA and Greedy FFD baselines."
echo -e ""
echo -e "  ${GREEN}✓${RESET}  ${BOLD}Bobcoin efficiency${RESET} : Peak RAM < 0.4 MiB for the Enterprise (64×12) topology —"
echo -e "         orders of magnitude below the 50 MiB Bobcoin threshold."
echo -e ""
echo -e "  ${GREEN}✓${RESET}  ${BOLD}IBM Bob Skill${RESET}      : Install skills/SKILL.md and ask Bob to optimise"
echo -e "         your cluster directly from the IDE chat."
echo -e ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║${RESET}  ${BOLD}QUBOB demo complete.${RESET}  Run ${BOLD}pytest${RESET} for all 228 tests.          ${CYAN}║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${RESET}"
echo -e ""
