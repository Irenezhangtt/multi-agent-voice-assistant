#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
mode="${1:-prepare}"
case "$mode" in
  prepare|run) ;;
  *) echo "Usage: bash scripts/harbor-eval.sh [prepare|run]" >&2; exit 2 ;;
esac
export_root="${HARBOR_EXPORT_DIR:-$(pwd)/reports/harbor/export}"
export_args=(--output "$export_root")
if [[ -n "${HARBOR_DATASET:-}" ]]; then
  export_args+=(--dataset "$HARBOR_DATASET")
fi
.venv/bin/python -m evals.harbor_export "${export_args[@]}"
if [[ "$mode" == prepare ]]; then
  echo "Harbor tasks 已导出，尚未执行容器。"
  exit 0
fi
if [[ ! -x .harbor-venv/bin/harbor ]]; then
  echo "请先创建独立 .harbor-venv，并安装 requirements-harbor.lock。详见 docs/harbor-evals.md。" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "缺少 Docker。任务已准备好，尚未开始任何 Sandbox trial。请安装并启动 Docker 后重试。" >&2
  exit 1
fi
docker info >/dev/null
export HARBOR_TELEMETRY_ENABLED=false
export LITELLM_LOCAL_MODEL_COST_MAP=true
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$export_root${PYTHONPATH:+:$PYTHONPATH}"
exec .harbor-venv/bin/harbor run -c "$export_root/job.json"
