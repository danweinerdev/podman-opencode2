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
#   ./opencode-container.sh --image FQN    # build as FQN:<git-sha8> + FQN:latest, run FQN:<git-sha8>
#   ./opencode-container.sh shell          # drop into bash instead
#   ./opencode-container.sh -- <cmd...>    # run an arbitrary command
#
# --image takes a fully qualified name without a tag (registry:port/name is
# fine). The build context must be a git repository with at least one commit.
#
# Requirements: bash, jq, podman, git, sha256sum.

set -euo pipefail

# --- Guardrails --------------------------------------------------------------
die() { printf 'opencode-container: %s\n' "$*" >&2; exit 1; }
warn() { printf 'opencode-container: warning: %s\n' "$*" >&2; }

TEMP_FILES=()
cleanup_temp_files() {
  local path
  for path in "${TEMP_FILES[@]}"; do
    rm -f -- "${path}"
  done
  TEMP_FILES=()
}
trap cleanup_temp_files EXIT

command -v jq >/dev/null 2>&1 || die "jq is required (install it and retry)"
command -v podman >/dev/null 2>&1 || die "podman is required (install it and retry)"
command -v git >/dev/null 2>&1 || die "git is required (used to detect a shared git common dir)"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required (used for stable project paths)"

INVOCATION_ROOT="$(realpath -- "${PWD}")"
CONFIG_PATH="${INVOCATION_ROOT}/.opencode-sandbox.json"
GLOBAL_ROUTER_PATH="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode2/model-router.json"
GLOBAL_ROUTER_PRESENT=0
if [[ -e "${GLOBAL_ROUTER_PATH}" || -L "${GLOBAL_ROUTER_PATH}" ]]; then
  [[ -f "${GLOBAL_ROUTER_PATH}" ]] \
    || die "global model-router config is not a regular file: ${GLOBAL_ROUTER_PATH}"
  jq -e -s 'length == 1 and (.[0] | type == "object")' "${GLOBAL_ROUTER_PATH}" >/dev/null \
    || die "invalid global model-router config: ${GLOBAL_ROUTER_PATH}"
  GLOBAL_ROUTER_PATH="$(realpath -- "${GLOBAL_ROUTER_PATH}")" \
    || die "unable to canonicalize global model-router config: ${GLOBAL_ROUTER_PATH}"
  GLOBAL_ROUTER_PRESENT=1
fi

# Git's user-global config follows host HOME, independently of the workspace and
# the XDG paths used by OpenCode. Mount only the canonical file and point Git at
# a stable launcher-owned target so this also works with prebuilt images whose
# baked username differs from the host username.
GLOBAL_GIT_CONFIG_PATH="${HOME}/.gitconfig"
GLOBAL_GIT_CONFIG_PRESENT=0
if [[ -e "${GLOBAL_GIT_CONFIG_PATH}" || -L "${GLOBAL_GIT_CONFIG_PATH}" ]]; then
  [[ -f "${GLOBAL_GIT_CONFIG_PATH}" && -r "${GLOBAL_GIT_CONFIG_PATH}" ]] \
    || die "user-global Git config is not a readable regular file: ${GLOBAL_GIT_CONFIG_PATH}"
  GLOBAL_GIT_CONFIG_PATH="$(realpath -- "${GLOBAL_GIT_CONFIG_PATH}")" \
    || die "unable to canonicalize user-global Git config: ${GLOBAL_GIT_CONFIG_PATH}"
  GLOBAL_GIT_CONFIG_PRESENT=1
fi
GLOBAL_GIT_CONFIG_TARGET="/run/opencode/gitconfig"

CONFIG_PRESENT=0
if [[ -e "${CONFIG_PATH}" && ! -f "${CONFIG_PATH}" ]]; then
  die "sandbox config is not a regular file: ${CONFIG_PATH}"
elif [[ -f "${CONFIG_PATH}" ]]; then
  CONFIG_PRESENT=1
else
  if [[ "${GLOBAL_ROUTER_PRESENT}" -eq 1 ]]; then
    warn "no .opencode-sandbox.json found; using image opencode2:latest, workspace ${INVOCATION_ROOT}, and user-global routing config"
  else
    warn "no .opencode-sandbox.json found; using image opencode2:latest, workspace ${INVOCATION_ROOT}, and baked routing defaults"
  fi
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
Baked_roots=(
  /etc/opencode
  /opt/opencode
  /opt/mcp
  /run/opencode
  /src
  /workspace
)
# containers mode does not create the generated /workspace/<hash> mount, so a
# host workspace that genuinely lives below /workspace can be mirrored safely.
Containers_workspace_reserved_roots=(
  /etc/opencode
  /opt/opencode
  /opt/mcp
  /run/opencode
  /run/opencode-container-engine.sock
  /src
)

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
    HOME|PATH|GIT_CONFIG_GLOBAL|XDG_*|OPENCODE_*) return 0 ;;
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

overlaps_optional_canonical_file() {
  local path="$1"
  local present="$2"
  local protected_file="$3"
  [[ "${present}" -eq 1 ]] && paths_overlap "${path}" "${protected_file}"
}

overlaps_global_router() {
  overlaps_optional_canonical_file "$1" "${GLOBAL_ROUTER_PRESENT}" "${GLOBAL_ROUTER_PATH}"
}

overlaps_global_git_config() {
  overlaps_optional_canonical_file "$1" "${GLOBAL_GIT_CONFIG_PRESENT}" "${GLOBAL_GIT_CONFIG_PATH}"
}

# --- Config loading + validation --------------------------------------------
SCHEMA_VERSION="$(config_jq -r '.schema_version // 1')"
[[ "${SCHEMA_VERSION}" == "1" ]] || die "unsupported schema_version: ${SCHEMA_VERSION}"

CONTAINERS_TYPE="$(config_jq -r 'if has("containers") then (.containers | type) else "absent" end')"
[[ "${CONTAINERS_TYPE}" == "absent" || "${CONTAINERS_TYPE}" == "boolean" ]] \
  || die "containers must be a boolean"
CONTAINERS_ENABLED="$(config_jq -r 'if .containers == true then 1 else 0 end')"

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

# Workspace is mounted at /src for compatibility and normally at a stable,
# CWD-derived path for OpenCode2's project/session identity. containers mode
# uses the canonical host path instead so nested bind mounts remain meaningful.
WORKSPACE="$(config_jq -r '.workspace // empty')"
if [[ -z "${WORKSPACE}" ]]; then
  WORKSPACE="${INVOCATION_ROOT}"
fi
case "${WORKSPACE}" in
  /*) : ;;
  *) WORKSPACE="${INVOCATION_ROOT}/${WORKSPACE}" ;;
esac
WORKSPACE="$(realpath -e -- "${WORKSPACE}")" || die "workspace does not exist: ${WORKSPACE}"
if paths_overlap "${WORKSPACE}" "${Host_state_roots[@]}"; then
  die "workspace ${WORKSPACE} overlaps host OpenCode/.agents/.claude/.mcp state"
fi
if overlaps_global_router "${WORKSPACE}"; then
  die "workspace ${WORKSPACE} overlaps the user-global model-router config"
fi
if overlaps_global_git_config "${WORKSPACE}"; then
  die "workspace ${WORKSPACE} overlaps the user-global Git config"
fi

PROJECT_DIGEST="$(printf '%s' "${INVOCATION_ROOT}" | sha256sum)"
PROJECT_KEY="${PROJECT_DIGEST%% *}"
PROJECT_KEY="${PROJECT_KEY:0:16}"
if [[ "${CONTAINERS_ENABLED}" -eq 1 ]]; then
  # Bind paths sent through a mounted host container-engine socket are resolved
  # by the host daemon. Mirror the workspace's canonical host path so $PWD-based
  # nested mounts name a path that exists on that host.
  if paths_overlap "${WORKSPACE}" "${Containers_workspace_reserved_roots[@]}"; then
    die "containers workspace target ${WORKSPACE} shadows a baked config/plugin path"
  fi
  CONTAINER_WORKSPACE="${WORKSPACE}"
else
  CONTAINER_WORKSPACE="/workspace/${PROJECT_KEY}"
fi

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
if [[ "${CONTAINERS_ENABLED}" -eq 1 ]]; then
  WORKDIR="$(realpath -m -- "${WORKDIR}")" || die "invalid containers workdir: ${WORKDIR}"
  if [[ "${WORKDIR}" != "${CONTAINER_WORKSPACE}" && "${WORKDIR}" != "${CONTAINER_WORKSPACE}"/* ]]; then
    die "containers workdir ${WORKDIR} must stay within workspace ${CONTAINER_WORKSPACE}"
  fi
fi

# --- podman run flag assembly ------------------------------------------------
RUN_FLAGS=(--pull=never --rm --init --userns=keep-id --security-opt label=disable)

CONTAINER_SOCKET_SOURCE=""
CONTAINER_SOCKET_TARGET="/run/opencode-container-engine.sock"
if [[ "${CONTAINERS_ENABLED}" -eq 1 ]]; then
  SOCKET_CANDIDATES=()
  if [[ "${XDG_RUNTIME_DIR:-}" == /* ]]; then
    SOCKET_CANDIDATES+=("${XDG_RUNTIME_DIR}/podman/podman.sock")
  fi
  SOCKET_CANDIDATES+=("/run/user/$(id -u)/podman/podman.sock")
  if [[ "${XDG_RUNTIME_DIR:-}" == /* ]]; then
    SOCKET_CANDIDATES+=("${XDG_RUNTIME_DIR}/docker.sock")
  fi
  SOCKET_CANDIDATES+=("/run/user/$(id -u)/docker.sock")
  SOCKET_CANDIDATES+=("/var/run/docker.sock")
  SOCKET_CANDIDATES+=("/run/docker.sock")

  declare -A SEEN_CONTAINER_SOCKETS=()
  for candidate_path in "${SOCKET_CANDIDATES[@]}"; do
    [[ -S "${candidate_path}" && -r "${candidate_path}" && -w "${candidate_path}" ]] \
      || continue
    canonical_candidate="$(realpath -- "${candidate_path}")" \
      || die "unable to canonicalize container-engine socket: ${candidate_path}"
    [[ -z "${SEEN_CONTAINER_SOCKETS[${canonical_candidate}]+x}" ]] || continue
    SEEN_CONTAINER_SOCKETS["${canonical_candidate}"]=1
    CONTAINER_SOCKET_SOURCE="${canonical_candidate}"
    break
  done

  [[ -n "${CONTAINER_SOCKET_SOURCE}" ]] \
    || die "containers is enabled, but no accessible Podman user or Docker socket was found"
  RUN_FLAGS+=(-v "${CONTAINER_SOCKET_SOURCE}:${CONTAINER_SOCKET_TARGET}")

  # Either engine can grant socket access through a supplementary host group.
  # Preserve memberships only when owner, primary-group, and world permissions
  # do not already explain this host user's read/write access.
  socket_uid="$(stat -Lc '%u' -- "${CONTAINER_SOCKET_SOURCE}")" \
    || die "unable to inspect container-engine socket owner: ${CONTAINER_SOCKET_SOURCE}"
  socket_gid="$(stat -Lc '%g' -- "${CONTAINER_SOCKET_SOURCE}")" \
    || die "unable to inspect container-engine socket group: ${CONTAINER_SOCKET_SOURCE}"
  socket_mode="$(stat -Lc '%a' -- "${CONTAINER_SOCKET_SOURCE}")" \
    || die "unable to inspect container-engine socket mode: ${CONTAINER_SOCKET_SOURCE}"
  socket_mode_decimal=$((8#${socket_mode}))
  if [[ "${socket_uid}" != "$(id -u)" && "${socket_gid}" != "$(id -g)" ]] \
      && (( (socket_mode_decimal & 6) != 6 )); then
    case " $(id -G) " in
      *" ${socket_gid} "*) RUN_FLAGS+=(--group-add keep-groups) ;;
    esac
  fi
fi

# The pinned preview stores credentials and sessions under XDG data and UI
# preferences under XDG state. Keep both isolated in one reusable named volume.
DATA_VOLUME="$(config_jq -r --arg default "opencode2-data-${PROJECT_KEY}" ".persistence.data_volume // \$default")"
if [[ -n "${DATA_VOLUME}" ]]; then
  [[ "${DATA_VOLUME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
    || die "invalid persistence.data_volume: ${DATA_VOLUME}"
  if [[ "${CONTAINERS_ENABLED}" -eq 1 ]] \
      && paths_overlap "${CONTAINER_WORKSPACE}" /var/lib/opencode-data; then
    die "containers workspace ${CONTAINER_WORKSPACE} overlaps the OpenCode data mount"
  fi
  RUN_FLAGS+=(-v "${DATA_VOLUME}:/var/lib/opencode-data:U")
  RUN_FLAGS+=(-e XDG_DATA_HOME=/var/lib/opencode-data)
  RUN_FLAGS+=(-e XDG_STATE_HOME=/var/lib/opencode-data/state)
fi

# TTY only when interactive (stdin and stdout are both a terminal).
if [[ -t 0 && -t 1 ]]; then
  RUN_FLAGS+=(-it)
fi

# Keep /src as a compatibility alias while running OpenCode2 from the selected
# generated or host-identical project path.
RUN_FLAGS+=(-v "${WORKSPACE}:/src")
RUN_FLAGS+=(-v "${WORKSPACE}:${CONTAINER_WORKSPACE}")
RUN_FLAGS+=(-w "${WORKDIR}")
if [[ "${GLOBAL_GIT_CONFIG_PRESENT}" -eq 1 ]]; then
  RUN_FLAGS+=(-v "${GLOBAL_GIT_CONFIG_PATH}:${GLOBAL_GIT_CONFIG_TARGET}:ro")
  RUN_FLAGS+=(-e GIT_CONFIG_GLOBAL="${GLOBAL_GIT_CONFIG_TARGET}")
fi
if [[ "${GLOBAL_ROUTER_PRESENT}" -eq 1 ]]; then
  RUN_FLAGS+=(-v "${GLOBAL_ROUTER_PATH}:/run/opencode/model-router-global.json:ro")
  RUN_FLAGS+=(-e OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router-global.json)
fi
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
  abs_source="$(realpath -e -- "${source_path}")" || die "mount source does not exist: ${source}"
  if paths_overlap "${abs_source}" "${Host_state_roots[@]}"; then
    die "mount source ${abs_source} overlaps host OpenCode/.agents/.claude/.mcp state"
  fi
  if overlaps_global_router "${abs_source}"; then
    die "mount source ${abs_source} overlaps the user-global model-router config"
  fi
  if overlaps_global_git_config "${abs_source}"; then
    die "mount source ${abs_source} overlaps the user-global Git config"
  fi

  if [[ -z "${target}" ]]; then
    target="${abs_source}"
  fi
  [[ "${target}" == /* ]] || die "mount target must be absolute: ${target}"
  target="$(realpath -m -- "${target}")" || die "invalid mount target: ${target}"
  if paths_overlap "${target}" "${Baked_roots[@]}"; then
    die "mount target ${target} shadows a baked config/plugin path"
  fi
  if [[ "${CONTAINERS_ENABLED}" -eq 1 ]] \
      && paths_overlap "${target}" "${CONTAINER_WORKSPACE}"; then
    die "mount target ${target} overlaps the containers workspace mirror ${CONTAINER_WORKSPACE}"
  fi
  if [[ "${CONTAINERS_ENABLED}" -eq 1 ]] \
      && paths_overlap "${target}" "${CONTAINER_SOCKET_TARGET}"; then
    die "mount target ${target} shadows the managed container-engine socket"
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

if [[ "${CONTAINERS_ENABLED}" -eq 1 ]]; then
  # These values describe the launcher-managed socket and deliberately override
  # conflicting env.pass/env.set entries when container-engine access is opted in.
  container_socket_uri="unix://${CONTAINER_SOCKET_TARGET}"
  add_env CONTAINER_HOST "${container_socket_uri}" set
  add_env DOCKER_HOST "${container_socket_uri}" set
fi

for name in "${ENV_ORDER[@]}"; do
  if [[ "${ENV_MODE[${name}]}" == "pass" ]]; then
    # Let Podman inherit the value from this process without embedding a secret
    # in the visible command-line arguments.
    RUN_FLAGS+=(-e "${name}")
  else
    RUN_FLAGS+=(-e "${name}=${ENV_MAP[${name}]}")
  fi
done

# Linked worktrees and submodules store a gitdir pointer in .git. Validate it
# explicitly so stale metadata fails clearly instead of silently suppressing
# the existing common-directory mount.
GIT_FILE_PRESENT=0
if [[ -f "${WORKSPACE}/.git" ]]; then
  GIT_FILE_PRESENT=1
  GIT_FILE_ENTRY="$(<"${WORKSPACE}/.git")"
  GIT_FILE_ENTRY="${GIT_FILE_ENTRY%$'\r'}"
  [[ "${GIT_FILE_ENTRY}" != *$'\n'* && "${GIT_FILE_ENTRY}" == "gitdir: "* ]] \
    || die "malformed linked-worktree git file: ${WORKSPACE}/.git"
  GIT_DIR_REF="${GIT_FILE_ENTRY#gitdir: }"
  [[ -n "${GIT_DIR_REF}" ]] || die "linked-worktree git file has an empty gitdir: ${WORKSPACE}/.git"
  case "${GIT_DIR_REF}" in
    /*) GIT_DIR_PATH="${GIT_DIR_REF}" ;;
    *) GIT_DIR_PATH="${WORKSPACE}/${GIT_DIR_REF}" ;;
  esac
  GIT_DIR_PATH="$(realpath -e -- "${GIT_DIR_PATH}")" \
    || die "linked-worktree gitdir does not exist: ${GIT_DIR_REF}"
  [[ -d "${GIT_DIR_PATH}" ]] || die "linked-worktree gitdir is not a directory: ${GIT_DIR_PATH}"
fi

GIT_COMMON_DIR=""
if GIT_COMMON_REF="$(git -C "${WORKSPACE}" rev-parse --git-common-dir 2>/dev/null)"; then
  GIT_COMMON_DIR="${GIT_COMMON_REF}"
  case "${GIT_COMMON_DIR}" in
    /*) : ;;
    *) GIT_COMMON_DIR="${WORKSPACE}/${GIT_COMMON_DIR}" ;;
  esac
  GIT_COMMON_DIR="$(realpath -- "${GIT_COMMON_DIR}")" || GIT_COMMON_DIR=""
elif [[ "${GIT_FILE_PRESENT}" -eq 1 ]]; then
  die "unable to resolve linked-worktree common directory from ${WORKSPACE}/.git"
fi

if [[ -n "${GIT_COMMON_DIR}" \
    && "${GIT_COMMON_DIR}" != "${WORKSPACE}" \
    && "${GIT_COMMON_DIR}" != "${WORKSPACE}"/* ]]; then
  if paths_overlap "${GIT_COMMON_DIR}" "${Host_state_roots[@]}"; then
    die "git metadata directory ${GIT_COMMON_DIR} overlaps host OpenCode/.agents/.claude/.mcp state"
  fi
  if overlaps_global_router "${GIT_COMMON_DIR}"; then
    die "git metadata directory ${GIT_COMMON_DIR} overlaps the user-global model-router config"
  fi
  if overlaps_global_git_config "${GIT_COMMON_DIR}"; then
    die "git metadata directory ${GIT_COMMON_DIR} overlaps the user-global Git config"
  fi
  if paths_overlap "${GIT_COMMON_DIR}" "${Baked_roots[@]}"; then
    die "git metadata directory ${GIT_COMMON_DIR} shadows a baked config/plugin path"
  fi
  if [[ "${CONTAINERS_ENABLED}" -eq 1 ]] \
      && paths_overlap "${GIT_COMMON_DIR}" "${CONTAINER_WORKSPACE}"; then
    die "git metadata directory ${GIT_COMMON_DIR} overlaps the containers workspace mirror"
  fi
  if [[ -n "${DATA_VOLUME}" ]] \
      && paths_overlap "${GIT_COMMON_DIR}" /var/lib/opencode-data; then
    die "git metadata directory ${GIT_COMMON_DIR} overlaps the OpenCode data mount"
  fi
  if [[ "${CONTAINERS_ENABLED}" -eq 1 ]] \
      && paths_overlap "${GIT_COMMON_DIR}" "${CONTAINER_SOCKET_TARGET}"; then
    die "git metadata directory ${GIT_COMMON_DIR} shadows the managed container-engine socket"
  fi
  RUN_FLAGS+=(-v "${GIT_COMMON_DIR}:${GIT_COMMON_DIR}")
fi

# --- Build (only when needed, only from configured local build) --------------
REBUILD=0
IMAGE_FQN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild) REBUILD=1; shift ;;
    --image)
      [[ $# -ge 2 ]] || die "--image requires a value"
      IMAGE_FQN="$2"
      shift 2 ;;
    --image=*) IMAGE_FQN="${1#--image=}"; shift ;;
    *) break ;;
  esac
done

# --image selects a fully qualified name (no tag) to build from this
# repository. The image is tagged both with the first 8 characters of the
# build context's HEAD commit and with latest, and the hash-pinned reference
# is what gets run.
if [[ -n "${IMAGE_FQN}" ]]; then
  case "${IMAGE_FQN}" in
    *[[:space:]]*) die "--image contains whitespace: ${IMAGE_FQN}" ;;
  esac
  if [[ "${IMAGE_FQN##*/}" == *:* ]]; then
    die "--image must be a fully qualified name without a tag (got ${IMAGE_FQN}); the launcher tags <name>:<git-sha> and <name>:latest"
  fi
  GIT_SHA="$(git -C "${CONTEXT}" rev-parse --verify HEAD 2>/dev/null)" \
    || die "--image requires the build context to be a git repository with at least one commit: ${CONTEXT}"
  GIT_SHA="${GIT_SHA:0:8}"
  IMAGE="${IMAGE_FQN}:${GIT_SHA}"
  IMAGE_TAGS=("${IMAGE}" "${IMAGE_FQN}:latest")
else
  IMAGE_TAGS=("${IMAGE}")
fi

HAS_BUILD_CONFIG=0
[[ -n "$(config_jq -r '.build // empty')" ]] && HAS_BUILD_CONFIG=1

IMAGE_EXISTS=0
podman image exists "${IMAGE}" >/dev/null 2>&1 && IMAGE_EXISTS=1

if [[ "${REBUILD}" -eq 1 || "${IMAGE_EXISTS}" -eq 0 ]]; then
  [[ "${HAS_BUILD_CONFIG}" -eq 1 ]] || die "image ${IMAGE} is missing and no build block is configured"
  [[ -f "${CONTAINERFILE}" ]] || die "containerfile not found: ${CONTAINERFILE}"

  BUILD_FLAGS=(
    --build-arg "USER_UID=$(id -u)"
    --build-arg "USER_GID=$(id -g)"
    --build-arg "USERNAME=$(id -un)"
  )
  while IFS= read -r arg; do
    [[ -n "${arg}" ]] && BUILD_FLAGS+=(--build-arg "${arg}")
  done < <(config_jq -r '.build.args // {} | to_entries[] | "\(.key)=\(.value)"')

  # A user-wide local-provider catalog is optional and build-time only. Keep it
  # outside both this repository and the runtime mounts, validate its constrained
  # provider-only shape, and expose only that one file to the image build. The
  # digest is also a cache key and is verified again by the Containerfile.
  LOCAL_PROVIDERS_PATH="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode2/local-providers.json"
  LOCAL_PROVIDERS_SHA256="absent"
  if [[ -e "${LOCAL_PROVIDERS_PATH}" && ! -f "${LOCAL_PROVIDERS_PATH}" ]]; then
    die "local provider catalog is not a regular file: ${LOCAL_PROVIDERS_PATH}"
  elif [[ -f "${LOCAL_PROVIDERS_PATH}" ]]; then
    jq -e -s '
      length == 1
      and (.[0] |
        type == "object"
        and ((keys - ["$schema", "provider"]) | length == 0)
        and (.provider |
          type == "object"
          and length > 0
          and all(to_entries[];
            (.key | type == "string" and length > 0)
            and (.value |
              type == "object"
              and (.npm == "@ai-sdk/openai-compatible")
              and ((has("name") | not) or (.name | type == "string" and length > 0))
              and (.options | type == "object")
              and (.options.baseURL | type == "string" and length > 0)
              and (.models |
                type == "object"
                and length > 0
                and all(to_entries[];
                  (.key | type == "string" and length > 0)
                  and (.value |
                    type == "object"
                    and ((has("name") | not) or (.name | type == "string" and length > 0))
                    and ((has("limit") | not) or (.limit |
                      type == "object"
                      and ((has("context") | not) or (.context | type == "number" and . > 0))
                      and ((has("output") | not) or (.output | type == "number" and . > 0))
                    ))
                  )
                )
              )
            )
          )
        )
        and ([.. | objects | keys[]]
          | all(.[];
            test("^(api[-_]?key|authorization|headers|token|access[-_]?token|secret|client[-_]?secret|password|credential|credentials)$"; "i")
            | not
          ))
      )
    ' "${LOCAL_PROVIDERS_PATH}" >/dev/null \
      || die "invalid local provider catalog: ${LOCAL_PROVIDERS_PATH}"
    LOCAL_PROVIDERS_PATH="$(realpath -- "${LOCAL_PROVIDERS_PATH}")"
    LOCAL_PROVIDERS_BUILD_PATH="$(mktemp "${TMPDIR:-/tmp}/opencode2-local-providers.XXXXXX.json")"
    TEMP_FILES+=("${LOCAL_PROVIDERS_BUILD_PATH}")
    # Mount only a canonical serialization of the validated object. Besides
    # making formatting-only edits cache-neutral, this ensures duplicate keys
    # or other discarded parser input cannot survive as hidden image-layer data.
    jq -S . "${LOCAL_PROVIDERS_PATH}" > "${LOCAL_PROVIDERS_BUILD_PATH}"
    chmod 0600 "${LOCAL_PROVIDERS_BUILD_PATH}"
    LOCAL_PROVIDERS_DIGEST="$(sha256sum -- "${LOCAL_PROVIDERS_BUILD_PATH}")"
    LOCAL_PROVIDERS_SHA256="${LOCAL_PROVIDERS_DIGEST%% *}"
    BUILD_FLAGS+=(
      --security-opt label=disable
      --volume "${LOCAL_PROVIDERS_BUILD_PATH}:/run/opencode2-build-config/local-providers.json:ro"
    )
  fi
  BUILD_FLAGS+=(--build-arg "LOCAL_PROVIDERS_SHA256=${LOCAL_PROVIDERS_SHA256}")

  TAG_FLAGS=()
  for tag in "${IMAGE_TAGS[@]}"; do
    TAG_FLAGS+=(-t "${tag}")
  done

  printf 'opencode-container: building %s (containerfile %s, context %s)\n' \
    "${IMAGE_TAGS[*]}" "${CONTAINERFILE}" "${CONTEXT}"
  podman build \
    "${BUILD_FLAGS[@]}" \
    "${TAG_FLAGS[@]}" \
    -f "${CONTAINERFILE}" \
    "${CONTEXT}"
  cleanup_temp_files
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
