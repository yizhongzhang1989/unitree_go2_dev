#!/usr/bin/env bash
# go2_service.sh — Manage Unitree Go2 robot services via the SDK API
#
# Wraps go2_service_api.py with LD_LIBRARY_PATH set correctly.
# No SSH or direct robot access required — communicates over DDS.
#
# Usage:
#   ./go2_service.sh list                         — list all services
#   ./go2_service.sh status <name>                — status of one service
#   ./go2_service.sh stop   <name>                — stop a service
#   ./go2_service.sh start  <name>                — start a service
#
# Optional: set IFACE to your network interface if needed (default: auto)
#   IFACE=enx606d3cbabf1b ./go2_service.sh list
#
# Common services:
#   mcf           — motion control framework (stop this before low-level control)
#   sport_mode    — sport controller
#   obstacles_avoid — obstacle avoidance

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/go2_service_api.py"

# Build argument list: command [service] [interface]
ARGS=("$@")
if [[ -n "${IFACE:-}" ]]; then
  ARGS+=("$IFACE")
fi

exec env LD_LIBRARY_PATH=/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} \
  python3 "$PYTHON_SCRIPT" "${ARGS[@]}"
