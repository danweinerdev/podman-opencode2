# Containerfile — OpenCode2 container: a Fedora 44 runtime for the OpenCode2 CLI
# with the code-graph / debug MCP servers, the SDD planning CLI, and a native
# v2 model-router plugin baked in.
#
# Multi-stage build. Only compiled binaries and plugin/skill assets are carried
# into the final image; every Go/Rust/Cargo toolchain lives in throwaway
# builder stages, so the runtime image ships no dev toolchain.
#
# Final-image layout:
#   /opt/mcp/bin/<code-graph-mcp|debug-mcp|sdd>   MCP + SDD binaries (on PATH)
#   /opt/opencode/plugins/<code-graph|debug|sdd>  OpenCode skills/plugin assets
#   /opt/opencode/plugins/model-router            native v2 model-router plugin
#   /opt/opencode/sandbox/                         launcher + config templates
#   /opt/opencode/config/opencode/opencode.json    optional local providers
#   /opt/opencode/config/opencode/agent/*.md      baked agent definitions
#   /opt/opencode/config/opencode/command/*.md    baked slash commands
#   /etc/opencode/container-config.json           baked OPENCODE_CONFIG
#   /etc/opencode/container-config.schema.json    pinned preview schema
#
# Writable OpenCode data/state/cache land under the image user's home
# (~/.local/share|state, ~/.cache); no host state is assumed.

# ---------------------------------------------------------------------------
# Stage 1: exact Go toolchain (1.26.5) for the sdd CLI
# ---------------------------------------------------------------------------
FROM docker.io/library/golang:1.26.5-bookworm AS sdd-builder

ENV GOFLAGS=-buildvcs=false

# Clone the pinned sdd-planner, build the single `sdd` binary, and stage the
# portable .opencode-plugin tree (plugin.json + skills/ + shared/) alongside it.
# Its collaboration prompts are rendered by the skills into the restricted
# native workers, so the separate Claude-oriented agent catalog is not copied.
ARG SDD_PLANNER_REF=103abe038d896d40adeeaa9d900cdda2fb6c2ae0
RUN set -eux; \
    git clone --filter=blob:none --no-tags \
        https://github.com/danweinerdev/claude-sdd-planner.git /tmp/claude-sdd-planner; \
    cd /tmp/claude-sdd-planner; \
    git fetch --depth 1 origin "${SDD_PLANNER_REF}"; \
    git checkout FETCH_HEAD; \
    mkdir -p /opt/build/bin; \
    go build -o /opt/build/bin/sdd ./cmd/sdd; \
    mkdir -p /opt/build/sdd-plugin; \
    cp -a .opencode-plugin /opt/build/sdd-plugin/

# ---------------------------------------------------------------------------
# Stage 2: builder-only Rust toolchains for the two MCP servers
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
ARG CODE_GRAPH_MCP_REF=45d53cdd8ec17776ae7a6156e5be5cdb82c4ad4f
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

# ---------------------------------------------------------------------------
# Stage 3: final runtime image
# ---------------------------------------------------------------------------
FROM docker.io/library/fedora:44

ARG USER_UID=1000
ARG USER_GID=1000
ARG USERNAME=dev
ARG OPENCODE2_VERSION=0.0.0-beta-19234

# Runtime + debug utilities. Node 24 powers the OpenCode2 CLI npm package and the
# model-router plugin; lldb ships the lldb-dap provider the debug MCP server
# spawns. No Go/Rust/Cargo toolchain is installed here.
RUN dnf install -y --setopt=install_weak_deps=False \
        ca-certificates curl wget git gnupg2 \
        nodejs24 nodejs24-npm \
        jq ripgrep fd-find less vim nano ShellCheck \
        gdb lldb strace ltrace \
        procps-ng file unzip xz tar diffutils \
        python3 \
        shadow-utils \
        openssl-libs zlib \
    && command -v git >/dev/null \
    && command -v shellcheck >/dev/null \
    && git --version \
    && shellcheck --version \
    && dnf clean all

# OpenCode2 CLI, pinned via the @opencode-ai/cli npm package. Its postinstall
# script materializes the platform binary from the platform-specific optional
# dependency, so install with scripts disabled and run postinstall.mjs
# explicitly — then verify the binary reports the pinned version and that no
# legacy `opencode` executable exists.
RUN set -eux; \
    npm install -g @opencode-ai/cli@"${OPENCODE2_VERSION}" --ignore-scripts; \
    node "$(npm root -g)/@opencode-ai/cli/postinstall.mjs"; \
    [ "$(opencode2 --version)" = "opencode2 v${OPENCODE2_VERSION}" ]; \
    rm -rf /tmp/opencode; \
    ! command -v opencode >/dev/null 2>&1

# Create an unprivileged user whose UID/GID match the host caller so
# bind-mounted workspace files keep correct ownership under --userns=keep-id.
RUN set -eux; \
    if ! getent group "${USER_GID}" >/dev/null; then \
        groupadd --gid "${USER_GID}" "${USERNAME}"; \
    fi; \
    useradd --uid "${USER_UID}" --gid "${USER_GID}" \
        --create-home --shell /bin/bash "${USERNAME}"

# --- Baked binaries (on PATH via /opt/mcp/bin) -------------------------------
RUN mkdir -p /opt/mcp/bin
COPY --from=sdd-builder  /opt/build/bin/sdd            /opt/mcp/bin/sdd
COPY --from=mcp-builder  /opt/build/bin/code-graph-mcp /opt/mcp/bin/code-graph-mcp
COPY --from=mcp-builder  /opt/build/bin/debug-mcp      /opt/mcp/bin/debug-mcp
RUN chmod 0755 /opt/mcp/bin/*

# --- Baked plugin/skill assets ----------------------------------------------
RUN mkdir -p /opt/opencode/plugins
COPY --from=mcp-builder /opt/build/code-graph-plugin/opencode-plugin /opt/opencode/plugins/code-graph
COPY --from=mcp-builder /opt/build/debug-plugin/.opencode-plugin        /opt/opencode/plugins/debug
COPY --from=sdd-builder /opt/build/sdd-plugin/.opencode-plugin          /opt/opencode/plugins/sdd

# The native v2 model-router plugin, built from this repository. Its runtime
# dependency (@opencode-ai/plugin@1.18.25, pinned in package.json) is installed
# here so `import "@opencode-ai/plugin/v2/promise"` resolves in-container.
COPY plugins/model-router /opt/opencode/plugins/model-router
RUN cd /opt/opencode/plugins/model-router \
    && npm install --omit=dev --ignore-scripts --no-audit --no-fund

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
# Agent markdown definitions carry their own prompts and permissions; the
# model-router plugin assigns each a model. The SDD skills render their bundled
# collaboration prompts into these native workers instead of installing a
# second, less-restricted agent catalog.
RUN mkdir -p /opt/opencode/config/opencode/agent /opt/opencode/config/opencode/command
COPY plugins/model-router/agents/*.md /opt/opencode/config/opencode/agent/

# The host catalog is deliberately optional and is never part of the build
# context. The launcher exposes only that file at build time and supplies its
# digest above. Verify the mounted bytes before baking them into OpenCode's
# exact global-config filename. A digest without a mount (or vice versa) fails
# closed instead of silently producing an unexpected image.
ARG LOCAL_PROVIDERS_SHA256=absent
RUN set -eux; \
    case "${LOCAL_PROVIDERS_SHA256}" in \
        absent) \
            [ ! -e /run/opencode2-build-config/local-providers.json ]; \
            ;; \
        ''|*[!0-9a-f]*) \
            printf 'invalid LOCAL_PROVIDERS_SHA256: %s\n' "${LOCAL_PROVIDERS_SHA256}" >&2; \
            exit 1; \
            ;; \
        *) \
            [ "${#LOCAL_PROVIDERS_SHA256}" -eq 64 ]; \
            [ -f /run/opencode2-build-config/local-providers.json ]; \
            printf '%s  %s\n' "${LOCAL_PROVIDERS_SHA256}" \
                /run/opencode2-build-config/local-providers.json | sha256sum -c -; \
            jq -e -s 'length == 1 and (.[0] | \
                type == "object" and ((keys - ["$schema", "provider"]) | length == 0) and \
                (.provider | type == "object" and length > 0 and all(.[]; \
                    type == "object" and .npm == "@ai-sdk/openai-compatible" and \
                    (.options.baseURL | type == "string" and length > 0) and \
                    (.models | type == "object" and length > 0 and \
                        all(.[]; type == "object")))) and \
                ([.. | objects | keys[]] | all(.[]; \
                    test("^(api[-_]?key|authorization|headers|token|access[-_]?token|secret|client[-_]?secret|password|credential|credentials)$"; "i") | not)))' \
                /run/opencode2-build-config/local-providers.json >/dev/null; \
            jq -S . /run/opencode2-build-config/local-providers.json \
                > /opt/opencode/config/opencode/opencode.json; \
            chmod 0644 /opt/opencode/config/opencode/opencode.json; \
            ;; \
    esac

# code-graph slash commands become OpenCode commands.
COPY --from=mcp-builder /opt/build/code-graph-plugin/opencode-plugin/commands/*.md /opt/opencode/config/opencode/command/

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

# Encapsulation: the baked config is authoritative; project config and external
# (host) skill scans are disabled so the container never reaches for host
# state. Data/state/cache default to the image user's home.
ENV OPENCODE_CONFIG=/etc/opencode/container-config.json \
    XDG_CONFIG_HOME=/opt/opencode/config \
    OPENCODE_DISABLE_PROJECT_CONFIG=1 \
    OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \
    OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    PATH=/opt/mcp/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

USER ${USERNAME}
WORKDIR /home/${USERNAME}
CMD ["opencode2", "--standalone"]
