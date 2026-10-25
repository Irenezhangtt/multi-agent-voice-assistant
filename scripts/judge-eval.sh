#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [[ ! -x .tau-venv/bin/python ]]; then
  echo '请先运行 bash scripts/tau-eval.sh setup。' >&2
  exit 1
fi
mode="${1:-run}"
if [[ $# -gt 0 ]]; then shift; fi
export LITELLM_LOCAL_MODEL_COST_MAP=true
case "$mode" in
  run) exec .tau-venv/bin/python -m evals.llm_judge "$@" ;;
  repair) exec .tau-venv/bin/python -m evals.judge_repair "$@" ;;
  *) echo 'Usage: bash scripts/judge-eval.sh [run|repair] [options]' >&2; exit 2 ;;
esac
