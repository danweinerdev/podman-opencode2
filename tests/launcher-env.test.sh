#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

mkdir -p "${TMP}/bin" "${TMP}/workspace"

cat > "${TMP}/bin/podman" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "image" && "${2:-}" == "exists" ]]; then
  exit 0
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

printf 'unexpected podman invocation: %s\n' "$*" >&2
exit 1
EOF
chmod 0755 "${TMP}/bin/podman"

cat > "${TMP}/workspace/.opencode-sandbox.json" <<'EOF'
{
  "schema_version": 1,
  "image": "opencode2:test",
  "workspace": ".",
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
export ARGV_LOG ENV_LOG

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
[[ "$(grep -Fxc -- "OPENAI_API_KEY" "${ARGV_LOG}")" -eq 1 ]]
if grep -Fq -- "launch-secret-sentinel" "${ARGV_LOG}"; then
  printf 'provider secret leaked into podman argv\n' >&2
  exit 1
fi

grep -Fx -- "OPENAI_API_KEY=launch-secret-sentinel" "${ENV_LOG}" >/dev/null
grep -Fx -- "EMPTY_FORWARD_SET=x" "${ENV_LOG}" >/dev/null
grep -Fx -- "EMPTY_FORWARD=" "${ENV_LOG}" >/dev/null

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

# Linked worktree support must apply the same protection to the external Git
# common directory before mounting it into the container.
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
grep -F -- "git common directory" "${TMP}/git-common-overlap.log" >/dev/null
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
