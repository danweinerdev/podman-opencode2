# Containerfile — OpenCode2 container: a Fedora 44 runtime for the OpenCode2 CLI
# with the code-graph / debug / search MCP servers and the ai-devtools `aid`
# CLI + plugin baked in.
#
# Multi-stage build. Only compiled binaries and plugin/skill assets are carried
# into the final image; every Go/Rust/Cargo toolchain lives in throwaway
# builder stages, so the runtime image ships no dev toolchain.
#
# Final-image layout:
#   /opt/mcp/bin/<code-graph-mcp|debug-mcp|search-mcp|aid>  MCP + aid binaries (on PATH)
#   /opt/opencode/plugins/<code-graph|debug|search>         OpenCode skills/plugin assets
#   /opt/opencode/plugins/ai-devtools                       aid plugin (agents, skills, TUI)
#   /opt/opencode/plugins/sandbox/skills                    sandbox skills
#   /opt/opencode/sandbox/                         launcher + config templates
#   /opt/opencode/config/opencode/opencode.json    optional runtime-mounted local providers
#   /opt/opencode/config/opencode/agent/          root-owned agent dir (plugin registers agents)
#   /opt/opencode/config/opencode/command/*.md    baked slash commands
#   /etc/opencode/container-config.json           baked OPENCODE_CONFIG
#   /etc/opencode/container-config.schema.json    pinned config schema
#
# Writable OpenCode data/state/cache land under the image user's home
# (~/.local/share|state, ~/.cache); no host state is assumed.

# ---------------------------------------------------------------------------
# Stage 1: exact Go toolchain (1.26.10) for the aid CLI
# ---------------------------------------------------------------------------
FROM docker.io/library/golang:1.27.4-bookworm AS aid-builder

ENV GOFLAGS=-buildvcs=false

# Clone the pinned ai-devtools, build the single `aid` binary, and stage the
# plugin's runtime files: package.json plus everything its `files` list ships.
# The build passes -X main.version=<package.json version>, the same ldflag its
# Makefile release target uses, so `aid version` reports the plugin version.
# renovate: repo=danweinerdev/ai-devtools branch=main
ARG AI_DEVTOOLS_REF=abba5934875176cdc895d9d2ec520b7e276f18f3
RUN set -eux; \
    git clone --filter=blob:none --no-tags \
        https://github.com/danweinerdev/ai-devtools.git /tmp/ai-devtools; \
    cd /tmp/ai-devtools; \
    git fetch --depth 1 origin "${AI_DEVTOOLS_REF}"; \
    git checkout FETCH_HEAD; \
    version="$(sed -n 's/^ *"version": *"\([^"]*\)".*/\1/p' package.json | head -n 1)"; \
    mkdir -p /opt/build/bin /opt/build/ai-devtools-plugin/.opencode-plugin; \
    CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=${version}" \
        -o /opt/build/bin/aid ./cmd/aid; \
    cp -a package.json index.js tui.tsx skills agents dist \
        skills-manifest.json agents-manifest.json /opt/build/ai-devtools-plugin/; \
    cp -a .opencode-plugin/src .opencode-plugin/tui.tsx \
        /opt/build/ai-devtools-plugin/.opencode-plugin/

# ---------------------------------------------------------------------------
# Stage 2: builder-only Rust toolchains for the Rust MCP servers
# ---------------------------------------------------------------------------
FROM docker.io/library/fedora:44 AS mcp-builder

RUN dnf install -y --setopt=install_weak_deps=False \
        ca-certificates curl git \
        gcc gcc-c++ make cmake pkgconf-pkg-config \
        openssl-devel zlib-devel \
        rustup \
    && dnf clean all

# Each MCP repo carries a rust-toolchain.toml that auto-selects its pinned
# channel when cargo runs inside the clone, so install both up front to avoid a
# surprise toolchain download mid-build:
#   - code-graph-mcp pins channel "stable"
#   - lldb-debug-mcp pins channel "1.97.1"
#   - search-mcp pins channel "stable"
# Both list rustfmt + clippy as required components.
ENV RUSTUP_HOME=/opt/rust/rustup \
    CARGO_HOME=/opt/rust/cargo \
    PATH=/opt/rust/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN set -eux; \
    mkdir -p /opt/rust/rustup /opt/rust/cargo; \
    rustup-init -y --no-modify-path \
        --default-toolchain stable --profile minimal \
        --component rustfmt --component clippy; \
    rustup toolchain install 1.97.1 --profile minimal \
        --component rustfmt --component clippy

# code-graph-mcp: pinned server binary + its OpenCode plugin assets
# (code-graph.js + skills/ + commands/). Clone the branch tip then fetch the
# pinned ref so a plain commit SHA works without server-side
# uploadpack.allowReachableSHA1InWant.
# renovate: repo=danweinerdev/code-graph-mcp branch=main
ARG CODE_GRAPH_MCP_REF=9bb7b6bf47c12f48cdce57b2762881692b6c60b3
RUN set -eux; \
    git clone --filter=blob:none --no-tags \
        https://github.com/danweinerdev/code-graph-mcp.git /tmp/code-graph-mcp; \
    cd /tmp/code-graph-mcp; \
    git fetch --depth 1 origin "${CODE_GRAPH_MCP_REF}"; \
    git checkout FETCH_HEAD; \
    cargo build --release -p code-graph-mcp; \
    install -Dm755 target/release/code-graph-mcp /opt/build/bin/code-graph-mcp; \
    mkdir -p /opt/build/code-graph-plugin; \
    cp -a opencode-plugin /opt/build/code-graph-plugin/

# lldb-debug-mcp: pinned server binary + its .opencode-plugin skill tree.
# renovate: repo=danweinerdev/lldb-debug-mcp branch=main
ARG DEBUG_MCP_REF=a032c18f2f52c9f2b5a3c43f22917cef9e6264dc
RUN set -eux; \
    git clone --filter=blob:none --no-tags \
        https://github.com/danweinerdev/lldb-debug-mcp.git /tmp/lldb-debug-mcp; \
    cd /tmp/lldb-debug-mcp; \
    git fetch --depth 1 origin "${DEBUG_MCP_REF}"; \
    git checkout FETCH_HEAD; \
    cargo build --release -p debug-mcp; \
    install -Dm755 target/release/debug-mcp /opt/build/bin/debug-mcp; \
    mkdir -p /opt/build/debug-plugin; \
    cp -a .opencode-plugin /opt/build/debug-plugin/

# search-mcp: pinned server binary + its .opencode-plugin tree
# (search-mcp.js + skills/ + commands/).
# renovate: repo=danweinerdev/search-mcp branch=main
ARG SEARCH_MCP_REF=89891c8a8fdcaea6b8ab28e60441c01227f54306
RUN set -eux; \
    git clone --filter=blob:none --no-tags \
        https://github.com/danweinerdev/search-mcp.git /tmp/search-mcp; \
    cd /tmp/search-mcp; \
    git fetch --depth 1 origin "${SEARCH_MCP_REF}"; \
    git checkout FETCH_HEAD; \
    cargo build --release -p search-mcp; \
    install -Dm755 target/release/search-mcp /opt/build/bin/search-mcp; \
    mkdir -p /opt/build/search-plugin; \
    cp -a .opencode-plugin /opt/build/search-plugin/

# ---------------------------------------------------------------------------
# Stage 3: final runtime image
# ---------------------------------------------------------------------------
FROM docker.io/library/fedora:44

ARG USER_UID=1000
ARG USER_GID=1000
ARG USERNAME=dev
# renovate: datasource=npm depName=@opencode/cli
ARG OPENCODE2_VERSION=2.3.0
# This opaque cache key records only the launcher-validated host catalog's
# digest. The catalog bytes are never available to the image build.
ARG LOCAL_PROVIDERS_SHA256=absent

# Runtime + debug utilities. Node 24 powers the OpenCode2 CLI npm package; lldb
# ships the lldb-dap provider the debug MCP server spawns. No Go/Rust/Cargo
# toolchain is installed here.
RUN dnf install -y --setopt=install_weak_deps=False \
        ca-certificates curl wget git gnupg2 \
        nodejs24 nodejs24-npm \
        jq ripgrep fd-find less vim nano ShellCheck \
        gdb lldb strace ltrace \
        procps-ng file unzip xz tar diffutils \
        python3 python3-pip \
        shadow-utils \
        openssl-libs zlib \
        libcap \
    && command -v git >/dev/null \
    && command -v shellcheck >/dev/null \
    && command -v getcap >/dev/null \
    && command -v setcap >/dev/null \
    && git --version \
    && shellcheck --version \
    && dnf clean all; \
    python3 -m pip install --no-cache-dir --root-user-action ignore \
        PyYAML;

# OpenCode2 CLI, pinned via the @opencode/cli npm package (the formal v2
# release line; both the `opencode` and `opencode2` executables are the same
# binary). Its postinstall script materializes the platform binary from the
# platform-specific optional dependency, so install with scripts disabled and
# run postinstall.mjs explicitly — then verify the binary reports the pinned
# version.
RUN set -eux; \
    npm install -g @opencode/cli@"${OPENCODE2_VERSION}" --ignore-scripts; \
    node "$(npm root -g)/@opencode/cli/postinstall.mjs"; \
    [ "$(opencode2 --version)" = "opencode v${OPENCODE2_VERSION}" ]; \
    rm -rf /tmp/opencode

# Create an unprivileged user whose UID/GID match the host caller so
# bind-mounted workspace files keep correct ownership under --userns=keep-id.
RUN set -eux; \
    if ! getent group "${USER_GID}" >/dev/null; then \
        groupadd --gid "${USER_GID}" "${USERNAME}"; \
    fi; \
    useradd --uid "${USER_UID}" --gid "${USER_GID}" \
        --create-home --shell /bin/bash "${USERNAME}"

# Mount point of the launcher's per-project data volume, owned by the image
# user: a fresh named volume copies this ownership up. Nested mode relies on
# that, because it starts the container as root, where the volume's :U option
# would chown the volume to root rather than to the image user.
RUN install -d -m 0755 -o "${USER_UID}" -g "${USER_GID}" /var/lib/opencode-data

# --- Nested rootless Podman engine (default on) ------------------------------
# NESTED_PODMAN=1 ships a rootless Podman engine the launcher's default
# `"containers": "nested"` mode starts: the agent gets its own image store,
# network namespace and cgroup subtree, with no host engine socket. 0 builds
# the engine-free image. A host that cannot grant the nested mode its
# CAP_SYS_ADMIN in the bounding set, or its subordinate ids, is refused with a
# pointer to `"containers": false` or "host": without CAP_SYS_ADMIN, newuidmap
# cannot map an inner user namespace.
#
# The engine is rootless inside the agent container, so inner root is the
# agent's own uid and never the outer root that owns the baked policy. Its
# subordinate ids are every id of the outer 65,536-id user namespace except 0
# and the user's own (the podman/stable split: 1:(UID-1) and
# (UID+1):(65535-UID)); a USER_UID above 65535 takes the whole 1:65535. The
# useradd above allocates a 524288-based range that cannot exist inside a
# 65,536-id namespace, so these files are replaced, not appended to. The inner
# graph root lives on the launcher's per-project volume at
# /var/lib/opencode-containers (native overlay; no /dev/fuse needed).
ARG NESTED_PODMAN=1
RUN set -eux; \
    case "${NESTED_PODMAN}" in 0|1) ;; *) echo "NESTED_PODMAN must be 0 or 1" >&2; exit 1 ;; esac; \
    if [ "${NESTED_PODMAN}" = 1 ]; then \
        dnf install -y --setopt=install_weak_deps=False \
            podman crun conmon netavark aardvark-dns passt util-linux; \
        dnf clean all; \
        command -v setpriv >/dev/null; \
        subid_ranges() { \
            id="$1"; \
            if [ "${id}" -le 0 ]; then \
                echo "nested Podman needs a non-root image user (got id ${id})" >&2; exit 1; \
            elif [ "${id}" -gt 65535 ]; then \
                printf '%s:1:65535\n' "${USERNAME}"; \
            else \
                if [ "${id}" -gt 1 ]; then printf '%s:1:%s\n' "${USERNAME}" "$((id - 1))"; fi; \
                if [ "${id}" -lt 65535 ]; then printf '%s:%s:%s\n' "${USERNAME}" "$((id + 1))" "$((65535 - id))"; fi; \
            fi; \
        }; \
        subid_ranges "${USER_UID}" > /etc/subuid; \
        subid_ranges "${USER_GID}" > /etc/subgid; \
        chmod 0644 /etc/subuid /etc/subgid; \
        printf '%s\n' \
            '# Rootless engine store for the image user: the launcher mounts a' \
            '# per-project volume at /var/lib/opencode-containers in nested mode.' \
            '[storage]' \
            'driver = "overlay"' \
            'rootless_storage_path = "/var/lib/opencode-containers/storage"' \
            > /etc/containers/storage.conf; \
        install -d -m 0700 -o "${USER_UID}" -g "${USER_GID}" /var/lib/opencode-containers; \
    fi

# The ENTRYPOINT prelude: execs the command unchanged for a non-root start;
# for a root start (nested mode only) delegates the cgroup, re-checks the
# image's setuid/setgid policy, and drops to the image user. opencode-agent-env
# is the image-owned loader that applies the launcher's read-only agent
# environment file after that drop. The setuid policy strips every
# setuid/setgid bit except the nested engine's newuidmap/newgidmap (none at all
# with NESTED_PODMAN=0).
COPY image/opencode-entrypoint image/opencode-setuid-policy image/opencode-agent-env /usr/local/libexec/
RUN set -eux; \
    sed -i \
        -e "s/@USER_UID@/${USER_UID}/" \
        -e "s/@USER_GID@/${USER_GID}/" \
        -e "s/@NESTED_PODMAN@/${NESTED_PODMAN}/" \
        /usr/local/libexec/opencode-entrypoint; \
    if grep -q '@[A-Z_]*@' /usr/local/libexec/opencode-entrypoint; then exit 1; fi; \
    chmod 0755 /usr/local/libexec/opencode-entrypoint \
        /usr/local/libexec/opencode-setuid-policy \
        /usr/local/libexec/opencode-agent-env; \
    printf '%s\n' "${NESTED_PODMAN}" > /etc/opencode-nested-podman; \
    chmod 0444 /etc/opencode-nested-podman; \
    /usr/local/libexec/opencode-setuid-policy "${NESTED_PODMAN}"

# --- Baked binaries (on PATH via /opt/mcp/bin) -------------------------------
RUN mkdir -p /opt/mcp/bin
COPY --from=aid-builder  /opt/build/bin/aid             /opt/mcp/bin/aid
COPY --from=mcp-builder  /opt/build/bin/code-graph-mcp /opt/mcp/bin/code-graph-mcp
COPY --from=mcp-builder  /opt/build/bin/debug-mcp      /opt/mcp/bin/debug-mcp
COPY --from=mcp-builder  /opt/build/bin/search-mcp     /opt/mcp/bin/search-mcp
RUN chmod 0755 /opt/mcp/bin/*

# --- Baked plugin/skill assets ----------------------------------------------
RUN mkdir -p /opt/opencode/plugins
COPY --from=mcp-builder /opt/build/code-graph-plugin/opencode-plugin /opt/opencode/plugins/code-graph
COPY --from=mcp-builder /opt/build/debug-plugin/.opencode-plugin        /opt/opencode/plugins/debug
COPY --from=mcp-builder /opt/build/search-plugin/.opencode-plugin       /opt/opencode/plugins/search
# The ai-devtools plugin registers its own agents and skills; the host 2.2.6
# runtime supplies @opencode/plugin and solid-js to plugins, so it needs no
# installed dependencies.
COPY --from=aid-builder /opt/build/ai-devtools-plugin /opt/opencode/plugins/ai-devtools
# Sandbox how-to skills stay baked as plain skill directories and are listed in
# container-config.json's `skills`.
COPY plugins/sandbox/skills /opt/opencode/plugins/sandbox/skills

# Host-side sandbox templates are readable by the baked setup skill. They are
# copied into a mounted workspace for the user to run after leaving the image.
RUN mkdir -p /opt/opencode/sandbox
COPY bin/opencode-container /opt/opencode/sandbox/opencode-container
COPY examples/opencode-sandbox.json.example /opt/opencode/sandbox/.opencode-sandbox.json.example
COPY examples/local-providers.json.example /opt/opencode/sandbox/local-providers.json.example
RUN chmod 0755 /opt/opencode/sandbox/opencode-container \
    && chmod 0644 /opt/opencode/sandbox/.opencode-sandbox.json.example \
        /opt/opencode/sandbox/local-providers.json.example

# --- Baked agents + commands under XDG_CONFIG_HOME ---------------------------
# The ai-devtools plugin registers its own agents at load time, so no agent
# markdown is baked here; the directory stays because it is part of the
# root-owned XDG config tree.
RUN mkdir -p /opt/opencode/config/opencode/agent /opt/opencode/config/opencode/command
# The runtime bind-mount destination stays a file of valid JSON even when no
# local catalog is present, so the 2.2.6 runtime does not log it as malformed.
RUN printf '{}\n' > /opt/opencode/config/opencode/opencode.json \
    && chmod 0644 /opt/opencode/config/opencode/opencode.json

# code-graph and search slash commands become OpenCode commands.
COPY --from=mcp-builder /opt/build/code-graph-plugin/opencode-plugin/commands/*.md /opt/opencode/config/opencode/command/
COPY --from=mcp-builder /opt/build/search-plugin/.opencode-plugin/commands/*.md /opt/opencode/config/opencode/command/

# --- Baked OPENCODE_CONFIG ---------------------------------------------------
COPY container-config.json /etc/opencode/container-config.json
COPY container-config.schema.json /etc/opencode/container-config.schema.json
RUN chmod 0644 /etc/opencode/container-config.json /etc/opencode/container-config.schema.json

# Standalone mode keeps its private server on stdio and writes no service
# metadata beside the global policy assets, so the complete XDG config tree can
# remain root-owned and non-writable at runtime.
RUN chmod 0755 /opt/opencode/config/opencode \
        /opt/opencode/config/opencode/agent \
        /opt/opencode/config/opencode/command

# Encapsulation: the baked config is authoritative and project config is
# disabled so the container never reaches for host state. Data/state/cache
# default to the image user's home. OPENCODE_DISABLE_PROJECT_CONFIG keeps the
# runtime from reading project configs (and their .claude/.agents trees) from
# the mounted workspace. The v2 runtime has no external/Claude-skill scans to
# disable: its skill sources are the config directory, the config's `skills`
# entries, and the image user's (empty) home, so no further skill-scan
# switches exist.
ENV OPENCODE_CONFIG=/etc/opencode/container-config.json \
    XDG_CONFIG_HOME=/opt/opencode/config \
    OPENCODE_DISABLE_PROJECT_CONFIG=1 \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    PATH=/opt/mcp/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Changing the host catalog changes this metadata layer when a sandbox image is
# rebuilt, without copying the catalog's contents into the image.
LABEL io.opencode.local-providers-sha256="${LOCAL_PROVIDERS_SHA256}"
# The launcher refuses `"containers": "nested"` unless the image says "1".
LABEL io.opencode.nested-podman="${NESTED_PODMAN}"
# The launcher refuses `"containers": "nested"` unless the image says "1": an
# image built before project-env delivery would run the agent without its
# configured variables.
LABEL io.opencode.agent-env="1"

USER ${USERNAME}
WORKDIR /home/${USERNAME}
ENTRYPOINT ["/usr/local/libexec/opencode-entrypoint"]
CMD ["opencode2", "--standalone", "--auto"]
