#!/usr/bin/env bash
# Daily runner for cron. Activates the venv and runs the pipeline.
# Logs to logs/daily-YYYY-MM-DD.log. Exit code propagates for cron mail.
#
# Usage:
#   scripts/run_daily.sh            # dry-run email (.eml written, nothing sent)
#   scripts/run_daily.sh --send     # actually send the email (needs .env)
set -euo pipefail

# Resolve repo root from this script's location (robust under cron).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

export PYTHONPATH="${REPO_ROOT}/src"
mkdir -p logs
LOG="logs/daily-$(date +%F).log"

{
  echo "=== $(date '+%F %T %Z') starting pipeline ($*) ==="
  ./.venv/bin/python -m portfolio_monitor.pipeline "$@" --verbose
  rc=$?
  echo "=== $(date '+%F %T %Z') finished (exit ${rc}) ==="
  exit ${rc}
} >>"${LOG}" 2>&1
