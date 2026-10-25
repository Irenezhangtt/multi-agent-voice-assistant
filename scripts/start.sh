#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "请先运行 bash scripts/setup.sh 创建 Python 环境。" >&2
  exit 1
fi
exec .venv/bin/python -m voice_agent serve "$@"
