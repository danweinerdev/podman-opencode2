#!/usr/bin/env bash
#
# opencode-container.sh — run the OpenCode2 container for this workspace.
#
# Uses $PWD/.opencode-sandbox.json (schema_version 1) when present, otherwise
# warns and runs the baked image defaults against the current workspace. A
# sandbox config is trusted repository input: it controls local image builds and
# container run settings, so review it like executable project tooling. Secret
# values are never echoed.
#
# Usage:
#   ./opencode-container.sh                # build if needed, then run the default command
#   ./opencode-container.sh --rebuild      # force-rebuild the image, then run
#   ./opencode-container.sh shell          # drop into bash instead
#   ./opencode-container.sh -- <cmd...>    # run an arbitrary command
#
# Requirements: bash, jq, podman, git, sha256sum.

set -euo pipefail

# --- Guardrails --------------------------------------------------------------
die() { printf 'opencode-container: %s\n' "$*" >&2; exit 1; }
warn() { printf 'opencode-container: warning: %s\n' "$*" >&2; }

command -v jq >/dev/null 2>&1 || die "jq is required (install it and retry)"
command -v podman >/dev/null 2>&1 || die "podman is required (install it and retry)"
command -v git >/dev/null 2>&1 || die "git is required (used to detect a shared git common dir)"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required (used for stable project paths)"

INVOCATION_ROOT="$(realpath -- "${PWD}")"
CONFIG_PATH="${INVOCATION_ROOT}/.opencode-sandbox.json"
CONFIG_PRESENT=0
if [[ -e "${CONFIG_PATH}" && ! -f "${CONFIG_PATH}" ]]; then
  die "sandbox config is not a regular file: ${CONFIG_PATH}"
elif [[ -f "${CONFIG_PATH}" ]]; then
  CONFIG_PRESENT=1
else
  warn "no .opencode-sandbox.json found; using image opencode2:latest, workspace ${INVOCATION_ROOT}, and baked routing defaults"
fi

config_jq() {
  if [[ "${CONFIG_PRESENT}" -eq 1 ]]; then
    jq "$@" "${CONFIG_PATH}"
  else
    jq "$@" <<< '{}'
  fi
}

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

# Podman secret names mapped to the provider environment variables OpenCode2
# expects. Projects explicitly select names; openapi-api-key is retained as a
# compatibility alias for the canonical openai-api-key spelling.
PROVIDER_SECRET_SPECS=(
  "OPENAI_API_KEY:openai-api-key:openapi-api-key"
  "ANTHROPIC_API_KEY:anthropic-api-key"
  "DEEPSEEK_API_KEY:deepseek-api-key"
  "GROQ_API_KEY:groq-api-key"
  "GOOGLE_API_KEY:google-api-key"
  "GEMINI_API_KEY:gemini-api-key"
  "GOOGLE_GENERATIVE_AI_API_KEY:google-generative-ai-api-key"
  "MISTRAL_API_KEY:mistral-api-key"
  "XAI_API_KEY:xai-api-key"
  "OPENROUTER_API_KEY:openrouter-api-key"
  "PERPLEXITY_API_KEY:perplexity-api-key"
  "COHERE_API_KEY:cohere-api-key"
  "TOGETHER_API_KEY:together-api-key"
  "AZURE_OPENAI_API_KEY:azure-openai-api-key"
)

# Baked config/plugin paths and launcher control mounts; additional mounts may
# not shadow these, including through lexical `..` path segments.
Baked_roots=(/etc/opencode /opt/opencode /opt/mcp /run/opencode /src /workspace)

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
SCHEMA_VERSION="$(config_jq -r '.schema_version // 1')"
[[ "${SCHEMA_VERSION}" == "1" ]] || die "unsupported schema_version: ${SCHEMA_VERSION}"

IMAGE="$(config_jq -r '.image // "opencode2:latest"')"

# Optional build block.
CONTAINERFILE="$(config_jq -r '.build.containerfile // "Containerfile"')"
CONTEXT="$(config_jq -r '.build.context // empty')"
if [[ -z "${CONTEXT}" ]]; then
  CONTEXT="$(dirname "${CONTAINERFILE}")"
fi
# Resolve build paths against the workspace (CWD).
case "${CONTAINERFILE}" in
  /*) : ;;
  *) CONTAINERFILE="${INVOCATION_ROOT}/${CONTAINERFILE}" ;;
esac
case "${CONTEXT}" in
  /*) : ;;
  *) CONTEXT="${INVOCATION_ROOT}/${CONTEXT}" ;;
esac

# Workspace is mounted at /src for compatibility and at a stable, CWD-derived
# path for OpenCode2's project/session identity. This keeps one central data
# volume usable across projects without every project appearing as /src.
WORKSPACE="$(config_jq -r '.workspace // empty')"
if [[ -z "${WORKSPACE}" ]]; then
  WORKSPACE="${INVOCATION_ROOT}"
fi
case "${WORKSPACE}" in
  /*) : ;;
  *) WORKSPACE="${INVOCATION_ROOT}/${WORKSPACE}" ;;
esac
WORKSPACE="$(realpath -- "${WORKSPACE}")" || die "workspace does not exist: ${WORKSPACE}"
if paths_overlap "${WORKSPACE}" "${Host_state_roots[@]}"; then
  die "workspace ${WORKSPACE} overlaps host OpenCode/.agents/.claude/.mcp state"
fi

PROJECT_DIGEST="$(printf '%s' "${INVOCATION_ROOT}" | sha256sum)"
PROJECT_KEY="${PROJECT_DIGEST%% *}"
PROJECT_KEY="${PROJECT_KEY:0:16}"
CONTAINER_WORKSPACE="/workspace/${PROJECT_KEY}"

WORKDIR="$(config_jq -r '.workdir // empty')"
if [[ -z "${WORKDIR}" ]]; then
  WORKDIR="${CONTAINER_WORKSPACE}"
else
  case "${WORKDIR}" in
    /src) WORKDIR="${CONTAINER_WORKSPACE}" ;;
    /src/*) WORKDIR="${CONTAINER_WORKSPACE}/${WORKDIR#/src/}" ;;
    /*) : ;; # other absolute workdirs are used as-is
    *) WORKDIR="${CONTAINER_WORKSPACE}/${WORKDIR}" ;;
  esac
fi

# --- podman run flag assembly ------------------------------------------------
RUN_FLAGS=(--pull=never --rm --init --userns=keep-id --security-opt label=disable)

# The pinned preview stores credentials and sessions in the same SQLite
# database. Keep that database intact in one reusable named volume rather than
# copying credential rows into a project bind mount.
DATA_VOLUME="$(config_jq -r --arg default "opencode2-data-${PROJECT_KEY}" ".persistence.data_volume // \$default")"
if [[ -n "${DATA_VOLUME}" ]]; then
  [[ "${DATA_VOLUME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
    || die "invalid persistence.data_volume: ${DATA_VOLUME}"
  RUN_FLAGS+=(-v "${DATA_VOLUME}:/var/lib/opencode-data:U")
  RUN_FLAGS+=(-e XDG_DATA_HOME=/var/lib/opencode-data)
fi

# TTY only when interactive (stdin and stdout are both a terminal).
if [[ -t 0 && -t 1 ]]; then
  RUN_FLAGS+=(-it)
fi

# Keep /src as a compatibility alias while running OpenCode2 from the stable
# project path used to distinguish sessions in the shared data volume.
RUN_FLAGS+=(-v "${WORKSPACE}:/src")
RUN_FLAGS+=(-v "${WORKSPACE}:${CONTAINER_WORKSPACE}")
RUN_FLAGS+=(-w "${WORKDIR}")
if [[ "${CONFIG_PRESENT}" -eq 1 ]]; then
  RUN_FLAGS+=(-v "${CONFIG_PATH}:/run/opencode/sandbox.json:ro")
  RUN_FLAGS+=(-e OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json)
fi

# Network mode.
NETWORK="$(config_jq -r '.network // empty')"
if [[ -n "${NETWORK}" ]]; then
  RUN_FLAGS+=(--network "${NETWORK}")
fi

# Capabilities.
while IFS= read -r cap; do
  [[ -n "${cap}" ]] && RUN_FLAGS+=(--cap-add "${cap}")
done < <(config_jq -r '.capabilities[]? // empty')

# Restricted runtime arguments: only a small allow-list of passthrough flags.
while IFS= read -r arg; do
  [[ -n "${arg}" ]] || continue
  case "${arg}" in
    --add-host=*|--pids-limit=*|--ulimit=*) RUN_FLAGS+=("${arg}") ;;
    *) die "runtime_args entry is not allowed: ${arg}" ;;
  esac
done < <(config_jq -r '.runtime_args[]? // empty')

# Additional mounts. Relative host sources resolve against the workspace; an omitted
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
done < <(config_jq -c '.mounts[]? // empty')

# Environment: only provider vars that are actually set are forwarded, plus any
# explicitly passed/set vars. Reserved names are rejected.
declare -A ENV_MAP=()
declare -A ENV_MODE=()
declare -A SECRET_TARGETS=()
declare -A SECRET_NAME_TARGETS=()
declare -a ENV_ORDER=()

for spec in "${PROVIDER_SECRET_SPECS[@]}"; do
  IFS=: read -r target_env primary_secret alternate_secret <<< "${spec}"
  SECRET_NAME_TARGETS["${primary_secret}"]="${target_env}"
  [[ -n "${alternate_secret}" ]] && SECRET_NAME_TARGETS["${alternate_secret}"]="${target_env}"
done

# User-global Podman secrets are never injected into arbitrary projects merely
# because they exist. A sandbox config must opt in by known secret name.
while IFS= read -r requested_secret; do
  [[ -n "${requested_secret}" ]] || continue
  target_env="${SECRET_NAME_TARGETS[${requested_secret}]-}"
  [[ -n "${target_env}" ]] || die "unknown provider_secrets entry: ${requested_secret}"
  [[ -z "${SECRET_TARGETS[${target_env}]+x}" ]] \
    || die "multiple provider secrets target ${target_env}"
  podman secret exists "${requested_secret}" >/dev/null 2>&1 \
    || die "configured Podman secret does not exist: ${requested_secret}"
  RUN_FLAGS+=(--secret "${requested_secret},type=env,target=${target_env}")
  SECRET_TARGETS["${target_env}"]="${requested_secret}"
done < <(config_jq -r '.provider_secrets[]? // empty')

add_env() {
  local name="$1"
  local value="$2"
  local mode="$3"
  is_valid_env_name "${name}" || die "invalid environment variable name: ${name}"
  is_reserved_env_name "${name}" && die "reserved environment variable: ${name}"
  # A Podman secret is the less-exposed source and wins over inherited or
  # literal values targeting the same provider variable.
  [[ -n "${SECRET_TARGETS[${name}]+x}" ]] && return 0
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
done < <(config_jq -r '.env.pass[]? // empty')

while IFS= read -r kv; do
  [[ -n "${kv}" ]] || continue
  name="${kv%%=*}"
  value="${kv#*=}"
  [[ -n "${name}" ]] || die "env.set entry missing a name"
  add_env "${name}" "${value}" set
done < <(config_jq -r '.env.set // {} | to_entries[] | "\(.key)=\(.value)"')

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
[[ -n "$(config_jq -r '.build // empty')" ]] && HAS_BUILD_CONFIG=1

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
  done < <(config_jq -r '.build.args // {} | to_entries[] | "\(.key)=\(.value)"')

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
mapfile -t DEFAULT_CMD < <(config_jq -r '.command[]? // empty')

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
