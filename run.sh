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

GIT_CONFIG_ENTRIES=0
add_git_config() {
  export "GIT_CONFIG_KEY_${GIT_CONFIG_ENTRIES}=$1"
  export "GIT_CONFIG_VALUE_${GIT_CONFIG_ENTRIES}=$2"
  GIT_CONFIG_ENTRIES=$((GIT_CONFIG_ENTRIES + 1))
  export GIT_CONFIG_COUNT="${GIT_CONFIG_ENTRIES}"
}

# Optional: install the pinned client from a repository you can read instead of the
# upstream early-access repository. pyproject.toml keeps the upstream pin either way.
if [[ -n "${AGENTS_API_SDK_MIRROR:-}" ]]; then
  MIRROR_URL="${AGENTS_API_SDK_MIRROR}"
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    MIRROR_URL="${MIRROR_URL/https:\/\/github.com\//https://x-access-token:${GITHUB_TOKEN}@github.com/}"
  fi
  add_git_config "url.${MIRROR_URL}.insteadOf" "${SDK_REPOSITORY}"
  printf 'installing the Agents API client from %s\n' "${AGENTS_API_SDK_MIRROR}"
fi

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  add_git_config "url.https://x-access-token:${GITHUB_TOKEN}@github.com/.insteadOf" "https://github.com/"
fi

if ! GIT_TERMINAL_PROMPT=0 git ls-remote "${SDK_REPOSITORY}" HEAD >/dev/null 2>&1; then
  fail "cannot read the Agents API client source pinned in pyproject.toml; export GITHUB_TOKEN with read access to it, or export AGENTS_API_SDK_MIRROR with a repository you can read"
fi

PINNED_SDK_SHA="$(sed -n 's/.*agents-api-python-preview\.git@\([0-9a-f]\{40\}\).*/\1/p' "${ROOT_DIR}/pyproject.toml")"
if [[ -n "${AGENTS_API_SDK_MIRROR:-}" && -n "${PINNED_SDK_SHA}" ]] \
  && ! GIT_TERMINAL_PROMPT=0 git ls-remote "${SDK_REPOSITORY}" | grep -q "^${PINNED_SDK_SHA}"; then
  printf 'warning: %s exposes no ref at the pinned commit %s\n' "${AGENTS_API_SDK_MIRROR}" "${PINNED_SDK_SHA}" >&2
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  printf 'creating Python environment\n'
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

printf 'installing pinned cookbook dependencies\n'
"${VENV_DIR}/bin/python" -m pip --disable-pip-version-check install -e "${ROOT_DIR}[dev]"

printf '%s\n' "${RUN_LABEL}"
exec "${VENV_DIR}/bin/python" "${ENTRYPOINT}"
