#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
mode="${1:-run}"
if [[ $# -gt 0 ]]; then shift; fi
export LITELLM_LOCAL_MODEL_COST_MAP=true
case "$mode" in
  setup)
    if [[ ! -x .venv/bin/python ]]; then
      echo '请先运行 bash scripts/setup.sh。' >&2
      exit 1
    fi
    pin=$(.venv/bin/python -c 'import json; print(json.load(open("evals/tau-upstream.json"))["commit"])')
    if [[ ! -d .benchmarks/tau2-bench ]]; then
      mkdir -p .benchmarks
      git clone --filter=blob:none --no-checkout https://github.com/sierra-research/tau2-bench.git .benchmarks/tau2-bench
      git -C .benchmarks/tau2-bench fetch --depth 1 origin "$pin"
      git -C .benchmarks/tau2-bench checkout --detach "$pin"
    fi
    actual=$(git -C .benchmarks/tau2-bench rev-parse HEAD)
    if [[ "$actual" != "$pin" ]] || ! git -C .benchmarks/tau2-bench diff --quiet HEAD; then
      echo 'τ-bench checkout 与 pin 不一致或有修改；请保留修改后使用干净的指定版本。' >&2
      exit 1
    fi
    if [[ ! -x .tau-venv/bin/python ]]; then
      .venv/bin/python -m venv .tau-venv
    fi
    .tau-venv/bin/python -m pip install -r requirements-tau.lock -e .benchmarks/tau2-bench -e .
    exec .tau-venv/bin/python -m evals.tau_clinic --dry-run
    ;;
  run)
    if [[ ! -x .tau-venv/bin/python ]]; then
      echo '请先运行 bash scripts/tau-eval.sh setup。' >&2
      exit 1
    fi
    exec .tau-venv/bin/python -m evals.tau_clinic "$@"
    ;;
  *) echo 'Usage: bash scripts/tau-eval.sh [setup|run] [--dry-run | --cases ... --trials 2]' >&2; exit 2 ;;
esac
