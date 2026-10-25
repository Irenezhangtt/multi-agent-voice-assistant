#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  agent_python="${VOICE_AGENT_PYTHON:-}"
  if [[ -z "$agent_python" ]]; then
    for candidate in python3.12 python3.11 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
        agent_python="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$agent_python" ]]; then
    echo "需要 Python 3.11+（建议 3.12）。可通过 VOICE_AGENT_PYTHON 指定解释器路径。" >&2
    exit 1
  fi
  "$agent_python" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
.venv/bin/python -m voice_agent doctor
