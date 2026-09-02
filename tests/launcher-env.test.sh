#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

mkdir -p "${TMP}/bin" "${TMP}/workspace" "${TMP}/test-home"
export HOME="${TMP}/test-home"

make_unix_socket() {
  local path="$1"
  mkdir -p "$(dirname "${path}")"
  rm -f -- "${path}"
  python3 - "${path}" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_UNIX)
sock.bind(sys.argv[1])
sock.close()
PY
}

cat > "${TMP}/bin/podman" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "image" && "${2:-}" == "exists" ]]; then
  [[ "${PODMAN_IMAGE_EXISTS:-1}" == "1" ]]
  exit
fi

if [[ "${1:-}" == "secret" && "${2:-}" == "exists" ]]; then
  [[ ",${AVAILABLE_SECRETS-}," == *,"${3:-}",* ]]
  exit
fi

if [[ "${1:-}" == "run" ]]; then
  printf '%s\n' "$@" > "${ARGV_LOG}"
  {
    printf 'OPENAI_API_KEY=%s\n' "${OPENAI_API_KEY-}"
    printf 'EMPTY_FORWARD_SET=%s\n' "${EMPTY_FORWARD+x}"
    printf 'EMPTY_FORWARD=%s\n' "${EMPTY_FORWARD-}"
  } > "${ENV_LOG}"
  exit 0
fi

if [[ "${1:-}" == "build" ]]; then
  printf '%s\n' "$@" > "${BUILD_ARGV_LOG}"
  exit 0
fi

printf 'unexpected podman invocation: %s\n' "$*" >&2
exit 1
EOF
chmod 0755 "${TMP}/bin/podman"

# Allow socket-fallback tests to hide the real user's conventional rootless
# Podman path without changing the launcher's production discovery behavior.
cat > "${TMP}/bin/id" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${FAKE_UID:-}" && "${1:-}" == "-u" ]]; then
  printf '%s\n' "${FAKE_UID}"
  exit 0
fi
exec /usr/bin/id "$@"
EOF
chmod 0755 "${TMP}/bin/id"

cat > "${TMP}/workspace/.opencode-sandbox.json" <<'EOF'
{
  "schema_version": 1,
  "image": "opencode2:test",
  "workspace": ".",
  "containers": false,
  "mounts": [],
  "env": {
    "pass": ["OPENAI_API_KEY", "EMPTY_FORWARD"],
    "set": { "TZ": "UTC" }
  },
  "network": "none",
  "capabilities": [],
  "runtime_args": [],
  "command": ["/bin/true"]
}
EOF

ARGV_LOG="${TMP}/argv.log"
ENV_LOG="${TMP}/env.log"
BUILD_ARGV_LOG="${TMP}/build-argv.log"
export ARGV_LOG ENV_LOG BUILD_ARGV_LOG

(
  cd "${TMP}/workspace"
  PATH="${TMP}/bin:${PATH}" \
    OPENAI_API_KEY="launch-secret-sentinel" \
    EMPTY_FORWARD="" \
    "${ROOT}/examples/opencode-container.sh"
)

grep -Fx -- "OPENAI_API_KEY" "${ARGV_LOG}" >/dev/null
grep -Fx -- "EMPTY_FORWARD" "${ARGV_LOG}" >/dev/null
grep -Fx -- "TZ=UTC" "${ARGV_LOG}" >/dev/null
grep -Fx -- "${TMP}/workspace/.opencode-sandbox.json:/run/opencode/sandbox.json:ro" "${ARGV_LOG}" >/dev/null
grep -Fx -- "OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json" "${ARGV_LOG}" >/dev/null
WORKSPACE_DIGEST="$(printf '%s' "${TMP}/workspace" | sha256sum)"
WORKSPACE_KEY="${WORKSPACE_DIGEST%% *}"
WORKSPACE_KEY="${WORKSPACE_KEY:0:16}"
grep -Fx -- "opencode2-data-${WORKSPACE_KEY}:/var/lib/opencode-data:U" "${ARGV_LOG}" >/dev/null
grep -Fx -- "XDG_DATA_HOME=/var/lib/opencode-data" "${ARGV_LOG}" >/dev/null
grep -Fx -- "${TMP}/workspace:/workspace/${WORKSPACE_KEY}" "${ARGV_LOG}" >/dev/null
grep -Fx -- "/workspace/${WORKSPACE_KEY}" "${ARGV_LOG}" >/dev/null
if grep -Fq -- "/run/opencode-container-engine.sock" "${ARGV_LOG}"; then
  printf 'launcher exposed a container-engine socket while containers was false\n' >&2
  exit 1
fi
[[ "$(grep -Fxc -- "OPENAI_API_KEY" "${ARGV_LOG}")" -eq 1 ]]
if grep -Fq -- "launch-secret-sentinel" "${ARGV_LOG}"; then
  printf 'provider secret leaked into podman argv\n' >&2
  exit 1
fi

grep -Fx -- "OPENAI_API_KEY=launch-secret-sentinel" "${ENV_LOG}" >/dev/null
grep -Fx -- "EMPTY_FORWARD_SET=x" "${ENV_LOG}" >/dev/null
grep -Fx -- "EMPTY_FORWARD=" "${ENV_LOG}" >/dev/null

# Opting into host container-engine access prefers the user Podman socket over
# Docker, manages both client variables, and mirrors the workspace at its host
# path so nested bind mounts do not reference the generated /workspace path.
mkdir -p "${TMP}/containers-workspace/subdir" "${TMP}/containers-runtime"
make_unix_socket "${TMP}/containers-runtime/podman/podman.sock"
make_unix_socket "${TMP}/containers-runtime/docker.sock"
cat > "${TMP}/containers-workspace/.opencode-sandbox.json" <<'EOF'
{
  "image": "opencode2:test",
  "containers": true,
  "workdir": "subdir",
  "env": {
    "set": {
      "CONTAINER_HOST": "tcp://must-not-win.invalid",
      "DOCKER_HOST": "tcp://must-not-win.invalid"
    }
  },
  "command": ["/bin/true"]
}
EOF
(
  cd "${TMP}/containers-workspace"
  HOME="${TMP}/test-home" XDG_RUNTIME_DIR="${TMP}/containers-runtime" \
    PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
)
grep -Fx -- "${TMP}/containers-runtime/podman/podman.sock:/run/opencode-container-engine.sock" \
  "${ARGV_LOG}" >/dev/null
if grep -Fq -- "${TMP}/containers-runtime/docker.sock:/run/opencode-container-engine.sock" \
  "${ARGV_LOG}"; then
  printf 'launcher preferred Docker over the user Podman socket\n' >&2
  exit 1
fi
grep -Fx -- "${TMP}/containers-workspace:/src" "${ARGV_LOG}" >/dev/null
grep -Fx -- "${TMP}/containers-workspace:${TMP}/containers-workspace" "${ARGV_LOG}" >/dev/null
grep -Fx -- "${TMP}/containers-workspace/subdir" "${ARGV_LOG}" >/dev/null
grep -Fx -- "CONTAINER_HOST=unix:///run/opencode-container-engine.sock" "${ARGV_LOG}" >/dev/null
grep -Fx -- "DOCKER_HOST=unix:///run/opencode-container-engine.sock" "${ARGV_LOG}" >/dev/null
if grep -Fq -- "must-not-win.invalid" "${ARGV_LOG}"; then
  printf 'workspace environment overrode launcher-managed container-engine variables\n' >&2
  exit 1
fi
if grep -Fq -- "/workspace/" "${ARGV_LOG}"; then
  printf 'containers mode retained a generated workspace path\n' >&2
  exit 1
fi

# Host-path workdirs and additional mounts may not escape or shadow the mirror
# whose identity nested container bind mounts rely on.
jq '.workdir = "../outside"' \
  "${TMP}/containers-workspace/.opencode-sandbox.json" \
  > "${TMP}/containers-workspace/.opencode-sandbox.json.new"
mv "${TMP}/containers-workspace/.opencode-sandbox.json.new" \
  "${TMP}/containers-workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/containers-workspace"
  HOME="${TMP}/test-home" XDG_RUNTIME_DIR="${TMP}/containers-runtime" \
    PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/containers-workdir.log"; then
  printf 'containers mode accepted a workdir outside the mirrored workspace\n' >&2
  exit 1
fi
grep -F -- "must stay within workspace" "${TMP}/containers-workdir.log" >/dev/null

jq --arg target "${TMP}/containers-workspace/subdir" \
  '.workdir = "." | .mounts = [{source: ".", target: $target}]' \
  "${TMP}/containers-workspace/.opencode-sandbox.json" \
  > "${TMP}/containers-workspace/.opencode-sandbox.json.new"
mv "${TMP}/containers-workspace/.opencode-sandbox.json.new" \
  "${TMP}/containers-workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/containers-workspace"
  HOME="${TMP}/test-home" XDG_RUNTIME_DIR="${TMP}/containers-runtime" \
    PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/containers-mount-overlap.log"; then
  printf 'containers mode accepted a mount overlapping the workspace mirror\n' >&2
  exit 1
fi
grep -F -- "overlaps the containers workspace mirror" \
  "${TMP}/containers-mount-overlap.log" >/dev/null

# With no user Podman socket, a rootless Docker socket is selected next.
mkdir -p "${TMP}/docker-workspace" "${TMP}/docker-runtime"
make_unix_socket "${TMP}/docker-runtime/docker.sock"
printf '%s\n' '{"image":"opencode2:test","containers":true,"command":["/bin/true"]}' \
  > "${TMP}/docker-workspace/.opencode-sandbox.json"
(
  cd "${TMP}/docker-workspace"
  HOME="${TMP}/test-home" XDG_RUNTIME_DIR="${TMP}/docker-runtime" FAKE_UID=424242 \
    PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
)
grep -Fx -- "${TMP}/docker-runtime/docker.sock:/run/opencode-container-engine.sock" \
  "${ARGV_LOG}" >/dev/null
if grep -Fxq -- "keep-groups" "${ARGV_LOG}"; then
  printf 'launcher added system-Docker group handling for a rootless Docker socket\n' >&2
  exit 1
fi

# Non-boolean opt-in values fail before Podman is invoked.
mkdir -p "${TMP}/containers-invalid"
printf '%s\n' '{"containers":"true"}' > "${TMP}/containers-invalid/.opencode-sandbox.json"
if (
  cd "${TMP}/containers-invalid"
  HOME="${TMP}/test-home" PATH="${TMP}/bin:${PATH}" \
    "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/containers-invalid.log"; then
  printf 'launcher accepted a non-boolean containers value\n' >&2
  exit 1
fi
grep -F -- "containers must be a boolean" "${TMP}/containers-invalid.log" >/dev/null

# On hosts without a system Docker socket, opting in with no available socket
# fails closed. (A real accessible system socket is legitimately the fallback.)
if [[ ! -S /var/run/docker.sock && ! -S /run/docker.sock ]]; then
  mkdir -p "${TMP}/containers-missing" "${TMP}/empty-runtime"
  printf '%s\n' '{"containers":true}' > "${TMP}/containers-missing/.opencode-sandbox.json"
  if (
    cd "${TMP}/containers-missing"
    HOME="${TMP}/test-home" XDG_RUNTIME_DIR="${TMP}/empty-runtime" FAKE_UID=424242 \
      PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
  ) 2>"${TMP}/containers-missing.log"; then
    printf 'launcher accepted containers mode without an accessible socket\n' >&2
    exit 1
  fi
  grep -F -- "no accessible Podman user or Docker socket was found" \
    "${TMP}/containers-missing.log" >/dev/null
fi

# Lexical traversal in a mount target must not bypass the baked-path guard.
jq '.mounts = [{"source": ".", "target": "/src/../opt/opencode"}]' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/workspace/.opencode-sandbox.json.new"
mv "${TMP}/workspace/.opencode-sandbox.json.new" "${TMP}/workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/workspace"
  PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/rejected.log"; then
  printf 'launcher accepted a mount target that traverses into a baked path\n' >&2
  exit 1
fi
grep -F -- "shadows a baked config/plugin path" "${TMP}/rejected.log" >/dev/null

# A workspace that contains protected host state must be rejected, not only a
# workspace located inside one of the protected directories.
mkdir -p "${TMP}/host-home/.config/opencode"
jq --arg workspace "${TMP}/host-home" \
  '.workspace = $workspace | .mounts = []' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/host-home/.opencode-sandbox.json"
if (
  cd "${TMP}/host-home"
  HOME="${TMP}/host-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/workspace-overlap.log"; then
  printf 'launcher accepted a workspace containing protected host state\n' >&2
  exit 1
fi
grep -F -- "overlaps host OpenCode/.agents/.claude/.mcp state" "${TMP}/workspace-overlap.log" >/dev/null

# Additional mount sources must reject the same ancestor relationship.
mkdir -p "${TMP}/mount-home/.agents"
jq --arg source "${TMP}/mount-home" \
  '.workspace = "." | .mounts = [{source: $source, target: "/mnt/host-home"}]' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/workspace/.opencode-sandbox.json.new"
mv "${TMP}/workspace/.opencode-sandbox.json.new" "${TMP}/workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/workspace"
  HOME="${TMP}/mount-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/mount-overlap.log"; then
  printf 'launcher accepted a mount source containing protected host state\n' >&2
  exit 1
fi
grep -F -- "overlaps host OpenCode/.agents/.claude/.mcp state" "${TMP}/mount-overlap.log" >/dev/null

# Root is an ancestor of every protected path; guard against the special //*
# shell-pattern case that would otherwise miss it.
mkdir -p "${TMP}/root-home/.config/opencode"
jq '.workspace = "." | .mounts = [{source: "/", target: "/mnt/host-root"}]' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/workspace/.opencode-sandbox.json.new"
mv "${TMP}/workspace/.opencode-sandbox.json.new" "${TMP}/workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/workspace"
  HOME="${TMP}/root-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/root-overlap.log"; then
  printf 'launcher accepted the host root as a mount source\n' >&2
  exit 1
fi
grep -F -- "overlaps host OpenCode/.agents/.claude/.mcp state" "${TMP}/root-overlap.log" >/dev/null

# Canonicalize protected roots too, so a symlinked host-state directory cannot
# be exposed by mounting its resolved target.
mkdir -p "${TMP}/symlink-home" "${TMP}/sensitive-target"
ln -s "${TMP}/sensitive-target" "${TMP}/symlink-home/.agents"
jq --arg source "${TMP}/sensitive-target" \
  '.workspace = "." | .mounts = [{source: $source, target: "/mnt/sensitive"}]' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/workspace/.opencode-sandbox.json.new"
mv "${TMP}/workspace/.opencode-sandbox.json.new" "${TMP}/workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/workspace"
  HOME="${TMP}/symlink-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/symlink-overlap.log"; then
  printf 'launcher accepted the resolved target of symlinked host state\n' >&2
  exit 1
fi
grep -F -- "overlaps host OpenCode/.agents/.claude/.mcp state" "${TMP}/symlink-overlap.log" >/dev/null

# A .git file must expose its external metadata root at the same absolute path.
# Cover Git's usual absolute pointer and an equivalent relative pointer.
mkdir -p "${TMP}/git-source"
git init -q --initial-branch=main "${TMP}/git-source"
git -C "${TMP}/git-source" \
  -c user.name=Test -c user.email=test@example.invalid \
  commit -qm initial --allow-empty
git -C "${TMP}/git-source" worktree add -q --detach "${TMP}/git-linked-worktree" HEAD
printf '%s\n' '{"image":"opencode2:test","command":["/bin/true"]}' \
  > "${TMP}/git-linked-worktree/.opencode-sandbox.json"
GIT_LINKED_DIR="$(git -C "${TMP}/git-linked-worktree" rev-parse --git-dir)"
GIT_LINKED_COMMON="$(git -C "${TMP}/git-linked-worktree" rev-parse --git-common-dir)"
(
  cd "${TMP}/git-linked-worktree"
  HOME="${TMP}/test-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
)
grep -Fx -- "${GIT_LINKED_COMMON}:${GIT_LINKED_COMMON}" "${ARGV_LOG}" >/dev/null

GIT_LINKED_RELATIVE="$(realpath --relative-to="${TMP}/git-linked-worktree" "${GIT_LINKED_DIR}")"
printf 'gitdir: %s\n' "${GIT_LINKED_RELATIVE}" > "${TMP}/git-linked-worktree/.git"
(
  cd "${TMP}/git-linked-worktree"
  HOME="${TMP}/test-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
)
grep -Fx -- "${GIT_LINKED_COMMON}:${GIT_LINKED_COMMON}" "${ARGV_LOG}" >/dev/null

# A stale .git pointer is an actionable error rather than a silent omission.
printf 'gitdir: ../missing-worktree-metadata\n' > "${TMP}/git-linked-worktree/.git"
if (
  cd "${TMP}/git-linked-worktree"
  HOME="${TMP}/test-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/git-stale-pointer.log"; then
  printf 'launcher accepted a stale linked-worktree gitdir pointer\n' >&2
  exit 1
fi
grep -F -- "linked-worktree gitdir does not exist" "${TMP}/git-stale-pointer.log" >/dev/null

# Linked worktree support must apply the same protection to the external Git
# metadata root before mounting it into the container.
mkdir -p "${TMP}/git-home/.agents"
git init -q --initial-branch=main "${TMP}/git-home/.agents/main"
git -C "${TMP}/git-home/.agents/main" \
  -c user.name=Test -c user.email=test@example.invalid \
  commit -qm initial --allow-empty
git -C "${TMP}/git-home/.agents/main" worktree add -q -b smoke "${TMP}/git-worktree"
jq '.workspace = "." | .mounts = []' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/git-worktree/.opencode-sandbox.json"
if (
  cd "${TMP}/git-worktree"
  HOME="${TMP}/git-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/git-common-overlap.log"; then
  printf 'launcher accepted a protected linked-worktree common directory\n' >&2
  exit 1
fi
grep -F -- "git metadata directory" "${TMP}/git-common-overlap.log" >/dev/null
grep -F -- "overlaps host OpenCode/.agents/.claude/.mcp state" "${TMP}/git-common-overlap.log" >/dev/null

# Effective custom XDG locations are host OpenCode state too.
mkdir -p "${TMP}/xdg-home" "${TMP}/custom-xdg-data/opencode"
jq --arg source "${TMP}/custom-xdg-data" \
  '.workspace = "." | .mounts = [{source: $source, target: "/mnt/xdg-data"}]' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/workspace/.opencode-sandbox.json.new"
mv "${TMP}/workspace/.opencode-sandbox.json.new" "${TMP}/workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/workspace"
  HOME="${TMP}/xdg-home" XDG_DATA_HOME="${TMP}/custom-xdg-data" \
    PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/xdg-overlap.log"; then
  printf 'launcher accepted an ancestor of custom XDG OpenCode state\n' >&2
  exit 1
fi
grep -F -- "overlaps host OpenCode/.agents/.claude/.mcp state" "${TMP}/xdg-overlap.log" >/dev/null

# Relative additional-mount sources resolve from the configured workspace, not
# from the directory containing the launcher config.
mkdir -p "${TMP}/relative-home" "${TMP}/relative-workspace/data"
jq --arg workspace "${TMP}/relative-workspace" \
  '.workspace = $workspace | .mounts = [{source: "data", target: "/mnt/data"}]' \
  "${TMP}/workspace/.opencode-sandbox.json" > "${TMP}/workspace/.opencode-sandbox.json.new"
mv "${TMP}/workspace/.opencode-sandbox.json.new" "${TMP}/workspace/.opencode-sandbox.json"
(
  cd "${TMP}/workspace"
  HOME="${TMP}/relative-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
)
grep -Fx -- "${TMP}/relative-workspace/data:/mnt/data" "${ARGV_LOG}" >/dev/null

# With no sandbox config, warn and use only the launcher's baked defaults. The
# absent control file must not be mounted or advertised to the router.
mkdir -p "${TMP}/default-home" "${TMP}/default-workspace"
ln -s "${ROOT}/examples/opencode-container.sh" "${TMP}/bin/opencode-container"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/default-warning.log"
grep -F -- "warning: no .opencode-sandbox.json found" "${TMP}/default-warning.log" >/dev/null
grep -Fx -- "${TMP}/default-workspace:/src" "${ARGV_LOG}" >/dev/null
grep -Fx -- "opencode2:latest" "${ARGV_LOG}" >/dev/null
grep -Fx -- "opencode2" "${ARGV_LOG}" >/dev/null
grep -Fx -- "--standalone" "${ARGV_LOG}" >/dev/null
DEFAULT_DIGEST="$(printf '%s' "${TMP}/default-workspace" | sha256sum)"
DEFAULT_KEY="${DEFAULT_DIGEST%% *}"
DEFAULT_KEY="${DEFAULT_KEY:0:16}"
grep -Fx -- "${TMP}/default-workspace:/workspace/${DEFAULT_KEY}" "${ARGV_LOG}" >/dev/null
grep -Fx -- "/workspace/${DEFAULT_KEY}" "${ARGV_LOG}" >/dev/null
grep -Fx -- "opencode2-data-${DEFAULT_KEY}:/var/lib/opencode-data:U" "${ARGV_LOG}" >/dev/null
if grep -Fq -- "/run/opencode/sandbox.json" "${ARGV_LOG}"; then
  printf 'config-free launcher mounted or advertised an absent sandbox config\n' >&2
  exit 1
fi
if grep -Fq -- "/run/opencode/model-router-global.json" "${ARGV_LOG}"; then
  printf 'config-free launcher mounted or advertised an absent global router config\n' >&2
  exit 1
fi
if grep -Fq -- "/run/opencode/gitconfig" "${ARGV_LOG}"; then
  printf 'config-free launcher mounted an absent user-global Git config\n' >&2
  exit 1
fi

# HOME and the file itself may both be symlinks. The launcher must still mount
# only the canonical exact file at the stable Git override target, not host HOME
# or the source file's parent directory.
mkdir -p "${TMP}/gitconfig-home-real" "${TMP}/gitconfig-real"
printf '%s\n' '[user]' '  name = Test User' > "${TMP}/gitconfig-real/config"
ln -s "${TMP}/gitconfig-real/config" "${TMP}/gitconfig-home-real/.gitconfig"
ln -s "${TMP}/gitconfig-home-real" "${TMP}/gitconfig-home-link"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/gitconfig-home-link" XDG_CONFIG_HOME="${TMP}/unused-xdg-config" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/gitconfig-warning.log"
grep -Fx -- "${TMP}/gitconfig-real/config:/run/opencode/gitconfig:ro" \
  "${ARGV_LOG}" >/dev/null
grep -Fx -- "GIT_CONFIG_GLOBAL=/run/opencode/gitconfig" "${ARGV_LOG}" >/dev/null
if grep -Fq -- "${TMP}/gitconfig-real:" "${ARGV_LOG}"; then
  printf 'launcher mounted the user-global Git config parent directory\n' >&2
  exit 1
fi
if grep -Fq -- "${TMP}/gitconfig-home-real:" "${ARGV_LOG}"; then
  printf 'launcher mounted host HOME for the user-global Git config\n' >&2
  exit 1
fi

# An additional writable mount cannot expose the canonical config through a
# second path.
mkdir -p "${TMP}/gitconfig-overlap-workspace"
printf '%s\n' '{"mounts":[{"source":"'"${TMP}/gitconfig-real"'","target":"/mnt/gitconfig"}]}' \
  > "${TMP}/gitconfig-overlap-workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/gitconfig-overlap-workspace"
  HOME="${TMP}/gitconfig-home-link" XDG_CONFIG_HOME="${TMP}/unused-xdg-config" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/gitconfig-overlap.log"; then
  printf 'launcher exposed the user-global Git config through an additional mount\n' >&2
  exit 1
fi
grep -F -- "overlaps the user-global Git config" "${TMP}/gitconfig-overlap.log" >/dev/null

# Workspace environment settings cannot redirect Git away from the dedicated
# launcher-owned global config mount.
printf '%s\n' '{"env":{"set":{"GIT_CONFIG_GLOBAL":"/mnt/other"}}}' \
  > "${TMP}/gitconfig-overlap-workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/gitconfig-overlap-workspace"
  HOME="${TMP}/gitconfig-home-link" XDG_CONFIG_HOME="${TMP}/unused-xdg-config" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/gitconfig-env-override.log"; then
  printf 'launcher allowed workspace override of GIT_CONFIG_GLOBAL\n' >&2
  exit 1
fi
grep -F -- "reserved environment variable: GIT_CONFIG_GLOBAL" \
  "${TMP}/gitconfig-env-override.log" >/dev/null

# A standalone user-global router config is discovered on every launch,
# canonicalized, and mounted as one exact read-only file without requiring a
# workspace sandbox config.
mkdir -p "${TMP}/global-config-real/opencode2"
ln -s "${TMP}/global-config-real" "${TMP}/global-config-link"
printf '%s\n' '{"schema_version":1,"profiles":{"reasoning":{"model":"anthropic/claude-opus-4-1"}}}' \
  > "${TMP}/global-config-real/opencode2/model-router.json"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" XDG_CONFIG_HOME="${TMP}/global-config-link" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/global-warning.log"
grep -F -- "user-global routing config" "${TMP}/global-warning.log" >/dev/null
if grep -Fq -- "baked routing defaults" "${TMP}/global-warning.log"; then
  printf 'launcher described routing as baked-only despite a global config\n' >&2
  exit 1
fi
grep -Fx -- "${TMP}/global-config-real/opencode2/model-router.json:/run/opencode/model-router-global.json:ro" \
  "${ARGV_LOG}" >/dev/null
grep -Fx -- "OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router-global.json" \
  "${ARGV_LOG}" >/dev/null
if grep -Fxq -- "${TMP}/global-config-real/opencode2:/run/opencode" "${ARGV_LOG}"; then
  printf 'launcher mounted the global router parent directory\n' >&2
  exit 1
fi

# The exact read-only control mount must not be bypassable through a workspace
# or additional mount that exposes the same host file through a writable path.
mkdir -p "${TMP}/global-overlap-workspace"
printf '%s\n' '{"mounts":[{"source":"'"${TMP}/global-config-real/opencode2"'","target":"/mnt/global-config"}]}' \
  > "${TMP}/global-overlap-workspace/.opencode-sandbox.json"
if (
  cd "${TMP}/global-overlap-workspace"
  HOME="${TMP}/default-home" XDG_CONFIG_HOME="${TMP}/global-config-link" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/global-overlap.log"; then
  printf 'launcher exposed the global router through an additional mount\n' >&2
  exit 1
fi
grep -F -- "overlaps the user-global model-router config" "${TMP}/global-overlap.log" >/dev/null

# Global and workspace router inputs are independent and must both be mounted
# when both files exist.
printf '{}\n' > "${TMP}/default-workspace/.opencode-sandbox.json"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" XDG_CONFIG_HOME="${TMP}/global-config-link" \
    PATH="${TMP}/bin:${PATH}" opencode-container
)
grep -Fx -- "${TMP}/global-config-real/opencode2/model-router.json:/run/opencode/model-router-global.json:ro" \
  "${ARGV_LOG}" >/dev/null
grep -Fx -- "OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router-global.json" \
  "${ARGV_LOG}" >/dev/null
grep -Fx -- "${TMP}/default-workspace/.opencode-sandbox.json:/run/opencode/sandbox.json:ro" \
  "${ARGV_LOG}" >/dev/null
grep -Fx -- "OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json" "${ARGV_LOG}" >/dev/null

# Malformed, multiple-document, non-object, and non-file global inputs fail
# before Podman instead of silently falling back to another routing layer.
printf '%s\n' '{ not json' > "${TMP}/global-config-real/opencode2/model-router.json"
if (
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" XDG_CONFIG_HOME="${TMP}/global-config-link" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/global-malformed.log"; then
  printf 'launcher accepted malformed global router JSON\n' >&2
  exit 1
fi
grep -F -- "invalid global model-router config" "${TMP}/global-malformed.log" >/dev/null

printf '%s\n' '{}' '{}' > "${TMP}/global-config-real/opencode2/model-router.json"
if (
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" XDG_CONFIG_HOME="${TMP}/global-config-link" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/global-multiple.log"; then
  printf 'launcher accepted multiple global router JSON documents\n' >&2
  exit 1
fi
grep -F -- "invalid global model-router config" "${TMP}/global-multiple.log" >/dev/null

printf '%s\n' '[]' > "${TMP}/global-config-real/opencode2/model-router.json"
if (
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" XDG_CONFIG_HOME="${TMP}/global-config-link" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/global-non-object.log"; then
  printf 'launcher accepted a non-object global router config\n' >&2
  exit 1
fi
grep -F -- "invalid global model-router config" "${TMP}/global-non-object.log" >/dev/null

rm "${TMP}/global-config-real/opencode2/model-router.json"
mkdir "${TMP}/global-config-real/opencode2/model-router.json"
if (
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" XDG_CONFIG_HOME="${TMP}/global-config-link" \
    PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/global-not-file.log"; then
  printf 'launcher accepted a non-file global router path\n' >&2
  exit 1
fi
grep -F -- "global model-router config is not a regular file" "${TMP}/global-not-file.log" >/dev/null
rm -r "${TMP}/global-config-real/opencode2/model-router.json"

# A present partial config inherits the same defaults and is mounted so its
# optional router block can be consumed.
printf '{}\n' > "${TMP}/default-workspace/.opencode-sandbox.json"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" PATH="${TMP}/bin:${PATH}" "${ROOT}/examples/opencode-container.sh"
) 2>"${TMP}/partial-config.log"
if grep -Fq -- "no .opencode-sandbox.json found" "${TMP}/partial-config.log"; then
  printf 'launcher warned despite a present sandbox config\n' >&2
  exit 1
fi
grep -Fx -- "opencode2:latest" "${ARGV_LOG}" >/dev/null
grep -Fx -- "${TMP}/default-workspace/.opencode-sandbox.json:/run/opencode/sandbox.json:ro" "${ARGV_LOG}" >/dev/null
grep -Fx -- "OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json" "${ARGV_LOG}" >/dev/null

# Existing user-global Podman secrets are not injected without project opt-in;
# the normal name-only host environment fallback remains active.
rm -f "${TMP}/default-workspace/.opencode-sandbox.json"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" PATH="${TMP}/bin:${PATH}" \
    AVAILABLE_SECRETS="openai-api-key" OPENAI_API_KEY="environment-fallback" \
    opencode-container
) 2>"${TMP}/unconfigured-secret-warning.log"
if grep -Fq -- "type=env,target=OPENAI_API_KEY" "${ARGV_LOG}"; then
  printf 'launcher injected an available Podman secret without project opt-in\n' >&2
  exit 1
fi
grep -Fx -- "OPENAI_API_KEY" "${ARGV_LOG}" >/dev/null

# Explicitly configured known Podman secrets take precedence over a same-named
# host environment variable and map to the provider variable OpenCode2 expects.
printf '%s\n' '{"provider_secrets":["openai-api-key","deepseek-api-key"]}' \
  > "${TMP}/default-workspace/.opencode-sandbox.json"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" PATH="${TMP}/bin:${PATH}" \
    AVAILABLE_SECRETS="openai-api-key,deepseek-api-key" \
    OPENAI_API_KEY="must-not-be-forwarded" \
    opencode-container
) 2>"${TMP}/secret-warning.log"
grep -Fx -- "openai-api-key,type=env,target=OPENAI_API_KEY" "${ARGV_LOG}" >/dev/null
grep -Fx -- "deepseek-api-key,type=env,target=DEEPSEEK_API_KEY" "${ARGV_LOG}" >/dev/null
if grep -Fxq -- "OPENAI_API_KEY" "${ARGV_LOG}"; then
  printf 'launcher forwarded an environment variable shadowed by a Podman secret\n' >&2
  exit 1
fi
if grep -Fq -- "must-not-be-forwarded" "${ARGV_LOG}"; then
  printf 'launcher exposed a shadowed provider value in Podman arguments\n' >&2
  exit 1
fi

# The central data volume can be overridden without depending on the launcher's
# own installation directory.
printf '%s\n' '{"persistence":{"data_volume":"project-opencode-data"}}' \
  > "${TMP}/default-workspace/.opencode-sandbox.json"
(
  cd "${TMP}/default-workspace"
  HOME="${TMP}/default-home" PATH="${TMP}/bin:${PATH}" opencode-container
)
grep -Fx -- "project-opencode-data:/var/lib/opencode-data:U" "${ARGV_LOG}" >/dev/null

# An optional user-wide local-provider catalog is validated and exposed only to
# the image build. Its digest is both a build-cache key and an integrity check.
mkdir -p "${TMP}/build-workspace" "${TMP}/shared-config/opencode2"
jq -n --arg containerfile "${ROOT}/Containerfile" --arg context "${ROOT}" '
  {
    image: "opencode2:provider-build-test",
    build: {containerfile: $containerfile, context: $context},
    command: ["/bin/true"]
  }
' > "${TMP}/build-workspace/.opencode-sandbox.json"
cp "${ROOT}/examples/local-providers.json.example" \
  "${TMP}/shared-config/opencode2/local-providers.json"
jq -S . "${TMP}/shared-config/opencode2/local-providers.json" \
  > "${TMP}/expected-canonical-providers.json"
PROVIDER_DIGEST="$(sha256sum "${TMP}/expected-canonical-providers.json")"
PROVIDER_DIGEST="${PROVIDER_DIGEST%% *}"
(
  cd "${TMP}/build-workspace"
  HOME="${TMP}/build-home" XDG_CONFIG_HOME="${TMP}/shared-config" \
    PODMAN_IMAGE_EXISTS=0 PATH="${TMP}/bin:${PATH}" opencode-container
)
grep -Fx -- "LOCAL_PROVIDERS_SHA256=${PROVIDER_DIGEST}" "${BUILD_ARGV_LOG}" >/dev/null
grep -Fx -- "label=disable" "${BUILD_ARGV_LOG}" >/dev/null
grep -E -- '^/.+:/run/opencode2-build-config/local-providers\.json:ro$' \
  "${BUILD_ARGV_LOG}" >/dev/null

# Removing the catalog must produce an explicit absent cache key and no build
# mount, ensuring a rebuild can also remove a previously baked catalog.
rm "${TMP}/shared-config/opencode2/local-providers.json"
(
  cd "${TMP}/build-workspace"
  HOME="${TMP}/build-home" XDG_CONFIG_HOME="${TMP}/shared-config" \
    PODMAN_IMAGE_EXISTS=0 PATH="${TMP}/bin:${PATH}" opencode-container
)
grep -Fx -- "LOCAL_PROVIDERS_SHA256=absent" "${BUILD_ARGV_LOG}" >/dev/null
if grep -Fq -- "/run/opencode2-build-config/local-providers.json" "${BUILD_ARGV_LOG}"; then
  printf 'launcher mounted a missing local-provider catalog into the build\n' >&2
  exit 1
fi

# Malformed, empty, or credential-bearing catalogs fail before Podman build.
printf '%s\n' '{"provider":{"local":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:18080/v1","apiKey":"must-not-be-baked"},"models":{"model":{}}}}}' \
  > "${TMP}/shared-config/opencode2/local-providers.json"
if (
  cd "${TMP}/build-workspace"
  HOME="${TMP}/build-home" XDG_CONFIG_HOME="${TMP}/shared-config" \
    PODMAN_IMAGE_EXISTS=0 PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/provider-invalid.log"; then
  printf 'launcher accepted a credential-bearing local-provider catalog\n' >&2
  exit 1
fi
grep -F -- "invalid local provider catalog" "${TMP}/provider-invalid.log" >/dev/null

# Validation must consume exactly one document and reject non-object models;
# otherwise a valid trailing document could hide bytes that get baked verbatim.
printf '%s\n' '{"provider":{"first-valid":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:18080/v1"},"models":{"model":{}}}}}' \
  > "${TMP}/shared-config/opencode2/local-providers.json"
jq -c . "${ROOT}/examples/local-providers.json.example" \
  >> "${TMP}/shared-config/opencode2/local-providers.json"
if (
  cd "${TMP}/build-workspace"
  HOME="${TMP}/build-home" XDG_CONFIG_HOME="${TMP}/shared-config" \
    PODMAN_IMAGE_EXISTS=0 PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/provider-multiple.log"; then
  printf 'launcher accepted multiple local-provider JSON documents\n' >&2
  exit 1
fi
grep -F -- "invalid local provider catalog" "${TMP}/provider-multiple.log" >/dev/null

printf '%s\n' '{"provider":{"local":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:18080/v1"},"models":{"broken":null}}}}' \
  > "${TMP}/shared-config/opencode2/local-providers.json"
if (
  cd "${TMP}/build-workspace"
  HOME="${TMP}/build-home" XDG_CONFIG_HOME="${TMP}/shared-config" \
    PODMAN_IMAGE_EXISTS=0 PATH="${TMP}/bin:${PATH}" opencode-container
) 2>"${TMP}/provider-model.log"; then
  printf 'launcher accepted a non-object local model definition\n' >&2
  exit 1
fi
grep -F -- "invalid local provider catalog" "${TMP}/provider-model.log" >/dev/null
