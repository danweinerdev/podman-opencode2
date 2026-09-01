#!/usr/bin/env bash
#
# opencode-container.sh — run the OpenCode2 container for this workspace.
#
# Reads $PWD/.opencode-sandbox.json (schema_version 1) and builds (only when
# needed) and runs the configured image under podman. The config is trusted
# repository input: it controls local image builds and container run settings,
# so review it like executable project tooling. Secret values are never echoed.
#
# Usage:
#   ./opencode-container.sh                # build if needed, then run the default command
#   ./opencode-container.sh --rebuild      # force-rebuild the image, then run
#   ./opencode-container.sh shell          # drop into bash instead
#   ./opencode-container.sh -- <cmd...>    # run an arbitrary command
#
# Requirements: bash, jq, podman.

set -euo pipefail

# --- Guardrails --------------------------------------------------------------
die() { printf 'opencode-container: %s\n' "$*" >&2; exit 1; }

command -v jq >/dev/null 2>&1 || die "jq is required (install it and retry)"
command -v podman >/dev/null 2>&1 || die "podman is required (install it and retry)"
command -v git >/dev/null 2>&1 || die "git is required (used to detect a shared git common dir)"

CONFIG_PATH="${PWD}/.opencode-sandbox.json"
[[ -f "${CONFIG_PATH}" ]] || die "missing sandbox config: ${CONFIG_PATH} (run from the workspace root)"

# Provider API keys auto-forwarded when set in the host environment. This is
# the common multi-provider list plus AZURE_OPENAI_API_KEY. Unset variables are
# skipped; nothing is synthesized.
PROVIDER_ENV_VARS=(
  OPENAI_API_KEY
  ANTHROPIC_API_KEY
  DEEPSEEK_API_KEY
  GROQ_API_KEY
  GOOGLE_API_KEY
  GEMINI_API_KEY
  GOOGLE_GENERATIVE_AI_API_KEY
  MISTRAL_API_KEY
  XAI_API_KEY
  OPENROUTER_API_KEY
  PERPLEXITY_API_KEY
  COHERE_API_KEY
  TOGETHER_API_KEY
  AZURE_OPENAI_API_KEY
)

# Baked config/plugin paths and launcher control mounts; additional mounts may
# not shadow these, including through lexical `..` path segments.
Baked_roots=(/etc/opencode /opt/opencode /opt/mcp /run/opencode)

# Host agent state the container must never reach (by mount). The launcher
# deliberately does not forward OpenCode, .agents, .claude, or .mcp state.
Host_state_roots=(
  "${XDG_CONFIG_HOME:-${HOME}/.config}/opencode"
  "${XDG_DATA_HOME:-${HOME}/.local/share}/opencode"
  "${XDG_STATE_HOME:-${HOME}/.local/state}/opencode"
  "${XDG_CACHE_HOME:-${HOME}/.cache}/opencode"
  "${HOME}/.agents"
  "${HOME}/.claude"
  "${HOME}/.mcp"
)
for index in "${!Host_state_roots[@]}"; do
  Host_state_roots[index]="$(realpath -m -- "${Host_state_roots[index]}")"
done

is_reserved_env_name() {
  local name="$1"
  case "${name}" in
    HOME|PATH|XDG_*|OPENCODE_*) return 0 ;;
  esac
  return 1
}

is_valid_env_name() {
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

paths_overlap() {
  # paths_overlap <path> <root...> -> 0 if either side equals or contains the other
  local path="$1"
  shift
  local root
  for root in "$@"; do
    # Appending /* to / produces //*, which does not match ordinary absolute
    # paths. Root necessarily overlaps every absolute protected path.
    [[ "${path}" == "/" || "${root}" == "/" ]] && return 0
    if [[ "${path}" == "${root}" || "${path}" == "${root}"/* || "${root}" == "${path}"/* ]]; then
      return 0
    fi
  done
  return 1
}

# --- Config loading + validation --------------------------------------------
SCHEMA_VERSION="$(jq -r '.schema_version // 1' "${CONFIG_PATH}")"
[[ "${SCHEMA_VERSION}" == "1" ]] || die "unsupported schema_version: ${SCHEMA_VERSION}"

IMAGE="$(jq -r '.image // empty' "${CONFIG_PATH}")"
[[ -n "${IMAGE}" ]] || die "config must define \"image\""

# Optional build block.
CONTAINERFILE="$(jq -r '.build.containerfile // "Containerfile"' "${CONFIG_PATH}")"
CONTEXT="$(jq -r '.build.context // empty' "${CONFIG_PATH}")"
if [[ -z "${CONTEXT}" ]]; then
  CONTEXT="$(dirname "${CONTAINERFILE}")"
fi
# Resolve build paths against the workspace (CWD).
case "${CONTAINERFILE}" in
  /*) : ;;
  *) CONTAINERFILE="${PWD}/${CONTAINERFILE}" ;;
esac
case "${CONTEXT}" in
  /*) : ;;
  *) CONTEXT="${PWD}/${CONTEXT}" ;;
esac

# Workspace (always mounted at /src) and container workdir (default /src).
WORKSPACE="$(jq -r '.workspace // empty' "${CONFIG_PATH}")"
if [[ -z "${WORKSPACE}" ]]; then
  WORKSPACE="${PWD}"
fi
case "${WORKSPACE}" in
  /*) : ;;
  *) WORKSPACE="${PWD}/${WORKSPACE}" ;;
esac
WORKSPACE="$(realpath -- "${WORKSPACE}")" || die "workspace does not exist: ${WORKSPACE}"
if paths_overlap "${WORKSPACE}" "${Host_state_roots[@]}"; then
  die "workspace ${WORKSPACE} overlaps host OpenCode/.agents/.claude/.mcp state"
fi

WORKDIR="$(jq -r '.workdir // empty' "${CONFIG_PATH}")"
if [[ -z "${WORKDIR}" ]]; then
  WORKDIR="/src"
else
  case "${WORKDIR}" in
    /*) : ;;               # absolute workdir is used as-is
    *) WORKDIR="/src/${WORKDIR}" ;;  # relative workdir resolves under /src
  esac
fi

# --- podman run flag assembly ------------------------------------------------
RUN_FLAGS=(--pull=never --rm --init --userns=keep-id --security-opt label=disable)

# TTY only when interactive (stdin and stdout are both a terminal).
if [[ -t 0 && -t 1 ]]; then
  RUN_FLAGS+=(-it)
fi

# Workspace is mounted at /src; the sandbox config is mounted read-only at
# /run/opencode/sandbox.json, whose path the model-router plugin reads from
# OPENCODE_MODEL_ROUTER_CONFIG.
RUN_FLAGS+=(-v "${WORKSPACE}:/src")
RUN_FLAGS+=(-v "${CONFIG_PATH}:/run/opencode/sandbox.json:ro")
RUN_FLAGS+=(-w "${WORKDIR}")
RUN_FLAGS+=(-e OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json)

# Network mode.
NETWORK="$(jq -r '.network // empty' "${CONFIG_PATH}")"
if [[ -n "${NETWORK}" ]]; then
  RUN_FLAGS+=(--network "${NETWORK}")
fi

# Capabilities.
while IFS= read -r cap; do
  [[ -n "${cap}" ]] && RUN_FLAGS+=(--cap-add "${cap}")
done < <(jq -r '.capabilities[]? // empty' "${CONFIG_PATH}")

# Restricted runtime arguments: only a small allow-list of passthrough flags.
while IFS= read -r arg; do
  [[ -n "${arg}" ]] || continue
  case "${arg}" in
    --add-host=*|--pids-limit=*|--ulimit=*) RUN_FLAGS+=("${arg}") ;;
    *) die "runtime_args entry is not allowed: ${arg}" ;;
  esac
done < <(jq -r '.runtime_args[]? // empty' "${CONFIG_PATH}")

# Additional mounts. Relative host sources resolve against CWD; an omitted
# target mirrors the resolved absolute source path.
while IFS= read -r entry; do
  [[ -n "${entry}" ]] || continue
  source="$(jq -r '.source // empty' <<<"${entry}")"
  [[ -n "${source}" ]] || die "mount entry missing \"source\""
  target="$(jq -r '.target // empty' <<<"${entry}")"
  read_only="$(jq -r '.read_only // false' <<<"${entry}")"

  case "${source}" in
    /*) source_path="${source}" ;;
    *) source_path="${WORKSPACE}/${source}" ;;
  esac
  abs_source="$(realpath -- "${source_path}")" || die "mount source does not exist: ${source}"
  if paths_overlap "${abs_source}" "${Host_state_roots[@]}"; then
    die "mount source ${abs_source} overlaps host OpenCode/.agents/.claude/.mcp state"
  fi

  if [[ -z "${target}" ]]; then
    target="${abs_source}"
  fi
  [[ "${target}" == /* ]] || die "mount target must be absolute: ${target}"
  target="$(realpath -m -- "${target}")" || die "invalid mount target: ${target}"
  if paths_overlap "${target}" "${Baked_roots[@]}"; then
    die "mount target ${target} shadows a baked config/plugin path"
  fi

  flag="${abs_source}:${target}"
  if [[ "${read_only}" == "true" ]]; then
    flag="${flag}:ro"
  fi
  RUN_FLAGS+=(-v "${flag}")
done < <(jq -c '.mounts[]? // empty' "${CONFIG_PATH}")

# Environment: only provider vars that are actually set are forwarded, plus any
# explicitly passed/set vars. Reserved names are rejected.
declare -A ENV_MAP=()
declare -A ENV_MODE=()
declare -a ENV_ORDER=()

add_env() {
  local name="$1"
  local value="$2"
  local mode="$3"
  is_valid_env_name "${name}" || die "invalid environment variable name: ${name}"
  is_reserved_env_name "${name}" && die "reserved environment variable: ${name}"
  if [[ -z "${ENV_MAP[${name}]+x}" ]]; then
    ENV_ORDER+=("${name}")
  fi
  ENV_MAP["${name}"]="${value}"
  ENV_MODE["${name}"]="${mode}"
}

for name in "${PROVIDER_ENV_VARS[@]}"; do
  if [[ -v "${name}" ]]; then
    add_env "${name}" "" pass
  fi
done

while IFS= read -r name; do
  [[ -n "${name}" ]] || continue
  is_reserved_env_name "${name}" && die "reserved environment variable in env.pass: ${name}"
  if [[ -v "${name}" ]]; then
    add_env "${name}" "" pass
  fi
done < <(jq -r '.env.pass[]? // empty' "${CONFIG_PATH}")

while IFS= read -r kv; do
  [[ -n "${kv}" ]] || continue
  name="${kv%%=*}"
  value="${kv#*=}"
  [[ -n "${name}" ]] || die "env.set entry missing a name"
  add_env "${name}" "${value}" set
done < <(jq -r '.env.set // {} | to_entries[] | "\(.key)=\(.value)"' "${CONFIG_PATH}")

for name in "${ENV_ORDER[@]}"; do
  if [[ "${ENV_MODE[${name}]}" == "pass" ]]; then
    # Let Podman inherit the value from this process without embedding a secret
    # in the visible command-line arguments.
    RUN_FLAGS+=(-e "${name}")
  else
    RUN_FLAGS+=(-e "${name}=${ENV_MAP[${name}]}")
  fi
done

# Shared git common dir: a linked worktree keeps objects/refs in a sibling
# common dir; expose it at the same absolute path so git works in-container.
if GIT_COMMON_DIR="$(git -C "${WORKSPACE}" rev-parse --git-common-dir 2>/dev/null)"; then
  case "${GIT_COMMON_DIR}" in
    /*) : ;;
    *) GIT_COMMON_DIR="${WORKSPACE}/${GIT_COMMON_DIR}" ;;
  esac
  GIT_COMMON_DIR="$(cd "${GIT_COMMON_DIR}" 2>/dev/null && pwd)" || GIT_COMMON_DIR=""
  if [[ -n "${GIT_COMMON_DIR}" && "${GIT_COMMON_DIR}/" != "${WORKSPACE}/"* ]]; then
    if paths_overlap "${GIT_COMMON_DIR}" "${Host_state_roots[@]}"; then
      die "git common directory ${GIT_COMMON_DIR} overlaps host OpenCode/.agents/.claude/.mcp state"
    fi
    if paths_overlap "${GIT_COMMON_DIR}" "${Baked_roots[@]}"; then
      die "git common directory ${GIT_COMMON_DIR} shadows a baked config/plugin path"
    fi
    RUN_FLAGS+=(-v "${GIT_COMMON_DIR}:${GIT_COMMON_DIR}")
  fi
fi

# --- Build (only when needed, only from configured local build) --------------
REBUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild) REBUILD=1; shift ;;
    *) break ;;
  esac
done

HAS_BUILD_CONFIG=0
[[ -n "$(jq -r '.build // empty' "${CONFIG_PATH}")" ]] && HAS_BUILD_CONFIG=1

IMAGE_EXISTS=0
podman image exists "${IMAGE}" >/dev/null 2>&1 && IMAGE_EXISTS=1

if [[ "${REBUILD}" -eq 1 || "${IMAGE_EXISTS}" -eq 0 ]]; then
  [[ "${HAS_BUILD_CONFIG}" -eq 1 ]] || die "image ${IMAGE} is missing and no build block is configured"
  [[ -f "${CONTAINERFILE}" ]] || die "containerfile not found: ${CONTAINERFILE}"

  BUILD_ARG_FLAGS=(
    --build-arg "USER_UID=$(id -u)"
    --build-arg "USER_GID=$(id -g)"
    --build-arg "USERNAME=$(id -un)"
  )
  while IFS= read -r arg; do
    [[ -n "${arg}" ]] && BUILD_ARG_FLAGS+=(--build-arg "${arg}")
  done < <(jq -r '.build.args // {} | to_entries[] | "\(.key)=\(.value)"' "${CONFIG_PATH}")

  printf 'opencode-container: building %s (containerfile %s, context %s)\n' \
    "${IMAGE}" "${CONTAINERFILE}" "${CONTEXT}"
  podman build \
    "${BUILD_ARG_FLAGS[@]}" \
    -t "${IMAGE}" \
    -f "${CONTAINERFILE}" \
    "${CONTEXT}"
fi

# --- Command selection -------------------------------------------------------
DEFAULT_CMD=()
mapfile -t DEFAULT_CMD < <(jq -r '.command[]? // empty' "${CONFIG_PATH}")

if [[ ${#DEFAULT_CMD[@]} -gt 0 ]]; then
  CMD=("${DEFAULT_CMD[@]}")
else
  CMD=(opencode2 --standalone)
fi

if [[ $# -gt 0 ]]; then
  case "$1" in
    shell) CMD=(bash -l) ;;
    --) shift; CMD=("$@") ;;
    *) CMD=("$@") ;;
  esac
fi

# --- Run ---------------------------------------------------------------------
exec podman run "${RUN_FLAGS[@]}" "${IMAGE}" "${CMD[@]}"
