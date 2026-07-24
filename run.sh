#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SDK_REPOSITORY="https://github.com/OpenAI-Early-Access/agents-api-python-preview.git"

fail() {
  printf 'preflight error: %s\n' "$*" >&2
  exit 1
}

case "${1:-}" in
  "")
    ENTRYPOINT="${ROOT_DIR}/main.py"
    RUN_LABEL="running one OpenAI Agents API session on a Blaxel Sandbox"
    ;;
  "--handoff")
    ENTRYPOINT="${ROOT_DIR}/handoff.py"
    RUN_LABEL="running a fresh-session Agent Drive handoff"
    ;;
  *)
    fail "usage: ./run.sh [--handoff]"
    ;;
esac
[[ $# -le 1 ]] || fail "usage: ./run.sh [--handoff]"

command -v "${PYTHON_BIN}" >/dev/null 2>&1 || fail "${PYTHON_BIN} is not installed"

"${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 15) else 1)' \
  || fail "Python 3.11 through 3.14 is required"

[[ -n "${OPENAI_API_KEY:-}" ]] || fail "OPENAI_API_KEY is required"
[[ -n "${BL_WORKSPACE:-}" ]] || fail "BL_WORKSPACE is required"
[[ -n "${BL_API_KEY:-}" ]] || fail "BL_API_KEY is required"

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0="url.https://x-access-token:${GITHUB_TOKEN}@github.com/.insteadOf"
  export GIT_CONFIG_VALUE_0="https://github.com/"
fi

if ! GIT_TERMINAL_PROMPT=0 git ls-remote "${SDK_REPOSITORY}" HEAD >/dev/null 2>&1; then
  fail "cannot read the Agents API client source pinned in pyproject.toml; if it needs authentication, export GITHUB_TOKEN with read access"
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  printf 'creating Python environment\n'
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

printf 'installing pinned cookbook dependencies\n'
"${VENV_DIR}/bin/python" -m pip --disable-pip-version-check install -e "${ROOT_DIR}[dev]"

printf '%s\n' "${RUN_LABEL}"
exec "${VENV_DIR}/bin/python" "${ENTRYPOINT}"
