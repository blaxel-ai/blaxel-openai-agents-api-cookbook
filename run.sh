#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

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
  "--deploy-webhook")
    ENTRYPOINT="${ROOT_DIR}/webhook/deploy.py"
    RUN_LABEL="deploying the webhook handler to a Blaxel Sandbox"
    ;;
  "--reconnect")
    ENTRYPOINT="${ROOT_DIR}/webhook/reconnect.py"
    RUN_LABEL="running a webhook-managed session and reconnecting after deleting its worker"
    ;;
  *)
    fail "usage: ./run.sh [--handoff | --deploy-webhook | --reconnect]"
    ;;
esac
[[ $# -le 1 ]] || fail "usage: ./run.sh [--handoff | --deploy-webhook | --reconnect]"

command -v "${PYTHON_BIN}" >/dev/null 2>&1 || fail "${PYTHON_BIN} is not installed"

"${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 15) else 1)' \
  || fail "Python 3.11 through 3.14 is required"

[[ -n "${OPENAI_API_KEY:-}" ]] || fail "OPENAI_API_KEY is required"
if [[ -n "${OPENAI_EXECUTOR_API_KEY:-}" && "${OPENAI_EXECUTOR_API_KEY}" == "${OPENAI_API_KEY}" ]]; then
  fail "OPENAI_EXECUTOR_API_KEY must be a separate restricted key, not a copy of OPENAI_API_KEY"
fi
if [[ -n "${BL_API_KEY:-}" ]]; then
  [[ -n "${BL_WORKSPACE:-}" ]] || fail "BL_WORKSPACE is required alongside BL_API_KEY"
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  printf 'creating Python environment\n'
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

printf 'installing cookbook dependencies\n'
"${VENV_DIR}/bin/python" -m pip --disable-pip-version-check --quiet install --upgrade --force-reinstall -e "${ROOT_DIR}" \
  || fail "dependency installation failed; the Agents API client referenced in pyproject.toml must be reachable from this machine"

"${VENV_DIR}/bin/python" -c 'from runtime import resolve_blaxel_workspace; resolve_blaxel_workspace()' \
  || fail "Blaxel credentials unavailable: run 'bl login', or export BL_WORKSPACE and BL_API_KEY"

printf '%s\n' "${RUN_LABEL}"
exec "${VENV_DIR}/bin/python" "${ENTRYPOINT}"
