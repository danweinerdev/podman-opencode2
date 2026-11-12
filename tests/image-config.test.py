#!/usr/bin/env python3
"""Image policy checks that used to live in the model-router plugin tests.

These assert the built image's shape from the two inputs that define it: the
Containerfile and the baked OpenCode config. They are text/JSON checks on
purpose; building the image is the smoke task, not this one.
"""

import importlib.machinery
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
CONTAINERFILE = ROOT / "Containerfile"
CONTAINERFILE_DEV = ROOT / "Containerfile.dev"
ENTRYPOINT_SCRIPT = ROOT / "image" / "opencode-entrypoint"
LOADER_SCRIPT = ROOT / "image" / "opencode-agent-env"
CONTAINER_CONFIG = ROOT / "container-config.json"
EXAMPLE = ROOT / "examples" / "opencode-sandbox.json.example"
LAUNCHER = ROOT / "bin" / "opencode-container"

AI_DEVTOOLS_PLUGIN = "file:///opt/opencode/plugins/ai-devtools"
EXPECTED_CMD = ["opencode2", "--standalone", "--auto"]
EXPECTED_ENTRYPOINT = ["/usr/local/libexec/opencode-entrypoint"]
NESTED_LABEL = "io.opencode.nested-podman"
AGENT_ENV_LABEL = "io.opencode.agent-env"


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"unable to read {path.name}: {exc}")


def check_command(source: str) -> None:
    # `--auto` auto-approves every permission that is not explicitly denied, so
    # the whole instruction is pinned: changing the flag set is a conscious edit.
    commands = re.findall(r"^CMD\s+(\[.*\])\s*$", source, re.MULTILINE)
    if len(commands) != 1:
        fail(f"expected exactly one CMD instruction, found {len(commands)}")
    try:
        command = json.loads(commands[0])
    except ValueError:
        fail(f"CMD is not a JSON array: {commands[0]}")
    if command != EXPECTED_CMD:
        fail(f"CMD drifted from {EXPECTED_CMD}: {command}")


def check_default_agent(config: object) -> None:
    if not isinstance(config, dict):
        fail("container-config.json must be a JSON object")
    if config.get("default_agent") != "aid-orchestrator":
        fail(f"default_agent must be aid-orchestrator, got {config.get('default_agent')!r}")


def plugin_packages(config: object) -> list[str]:
    if not isinstance(config, dict):
        fail("container-config.json must be a JSON object")
    plugins = config.get("plugins")
    if not isinstance(plugins, list):
        fail("container-config.json plugins must be an array")
    packages: list[str] = []
    for entry in plugins:
        if isinstance(entry, str):
            packages.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("package"), str):
            packages.append(entry["package"])
        else:
            fail(f"unrecognised plugins entry: {entry!r}")
    return packages


def check_plugins(config: object) -> None:
    packages = plugin_packages(config)
    if packages != [AI_DEVTOOLS_PLUGIN]:
        fail(f"plugins must be exactly [{AI_DEVTOOLS_PLUGIN}], got {packages}")


def copy_destinations(source: str) -> list[str]:
    destinations: list[str] = []
    for line in source.splitlines():
        match = re.match(r"^\s*COPY\s+(.+)$", line)
        if match is None:
            continue
        tokens = [
            token for token in shlex.split(match.group(1)) if not token.startswith("--")
        ]
        if not tokens:
            continue
        destinations.append(tokens[-1].rstrip("/"))
    return destinations


def check_skill_copy_destinations(config: object, source: str) -> None:
    if not isinstance(config, dict):
        fail("container-config.json must be a JSON object")
    skills = config.get("skills")
    if not isinstance(skills, list):
        fail("container-config.json skills must be an array")
    destinations = copy_destinations(source)
    for skill in skills:
        if not isinstance(skill, str):
            fail(f"skills entries must be strings, got {skill!r}")
        if not skill.startswith("/opt/opencode/plugins/"):
            continue
        if not any(
            skill == destination or skill.startswith(destination + "/")
            for destination in destinations
        ):
            fail(f"skills path {skill} has no matching Containerfile COPY destination")


def check_opencode2_version(source: str, example: object) -> None:
    match = re.search(r"^ARG\s+OPENCODE2_VERSION=(\S+)\s*$", source, re.MULTILINE)
    if match is None:
        fail("Containerfile does not declare ARG OPENCODE2_VERSION")
    version = match.group(1)

    assertion = re.search(
        r'"\$\(opencode2 --version\)"\s*=\s*"opencode v([^"]*)"', source
    )
    if assertion is None:
        fail("Containerfile has no opencode2 --version assertion")
    asserted = assertion.group(1)
    # The assertion either pins the literal or interpolates the ARG itself.
    if asserted != "${OPENCODE2_VERSION}" and asserted != version:
        fail(
            f"opencode2 --version assertion {asserted!r} does not match "
            f"OPENCODE2_VERSION {version!r}"
        )

    if not isinstance(example, dict):
        fail("examples/opencode-sandbox.json.example must be a JSON object")
    build = example.get("build")
    args = build.get("args") if isinstance(build, dict) else None
    expected = args.get("OPENCODE2_VERSION") if isinstance(args, dict) else None
    if expected != version:
        fail(
            f"example OPENCODE2_VERSION {expected!r} does not match "
            f"Containerfile ARG {version!r}"
        )


def check_ai_devtools_ref(source: str) -> None:
    match = re.search(r"^ARG\s+AI_DEVTOOLS_REF=(\S+)\s*$", source, re.MULTILINE)
    if match is None:
        fail("Containerfile does not declare ARG AI_DEVTOOLS_REF")
    ref = match.group(1)
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        fail(f"AI_DEVTOOLS_REF must be a 40-character git SHA, got {ref!r}")


def check_permissions(config: object) -> None:
    if not isinstance(config, dict):
        fail("container-config.json must be a JSON object")
    permissions = config.get("permissions", [])
    if not isinstance(permissions, list):
        fail("container-config.json permissions must be an array")
    if "oc2-verify" in json.dumps(permissions):
        fail("container-config.json grants oc2-verify, which was dropped")


def check_singular_plugin(config: object) -> None:
    if not isinstance(config, dict):
        fail("container-config.json must be a JSON object")
    if "plugin" in config:
        fail('container-config.json has a singular "plugin" key, which selects the legacy v1 loader')


def check_mcp_servers(config: object) -> None:
    if not isinstance(config, dict):
        fail("container-config.json must be a JSON object")
    mcp = config.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    if not isinstance(servers, dict):
        fail("container-config.json mcp.servers must be an object")
    for name in ("code-graph", "debug", "search"):
        entry = servers.get(name)
        if not isinstance(entry, dict):
            fail(f"container-config.json must configure the {name} MCP server")
        if entry.get("disabled") is not False:
            fail(f"the {name} MCP server must be enabled (disabled: false)")


def check_policy_asset_permissions(source: str) -> None:
    # The config tree is scanned by the runtime, so it stays root-owned and not
    # world-writable. The agents themselves come from the plugin at load time.
    if re.search(r"chown -R[^\n]*/opt/opencode/config", source):
        fail("Containerfile must not chown -R /opt/opencode/config")
    if "chmod 1777 /opt/opencode/config/opencode" in source:
        fail("Containerfile must not chmod 1777 /opt/opencode/config/opencode")
    if "chmod 0755 /opt/opencode/config/opencode" not in source:
        fail("Containerfile must chmod 0755 /opt/opencode/config/opencode")


def check_runtime_tools(source: str) -> None:
    start = source.rfind("FROM docker.io/library/fedora:44")
    if start == -1:
        fail("Containerfile has no final Fedora runtime stage")
    runtime = source[start:]
    if "ca-certificates curl wget git gnupg2" not in runtime:
        fail("runtime image must install git")
    if "jq ripgrep fd-find less vim nano ShellCheck" not in runtime:
        fail("runtime image must install ShellCheck")
    if "command -v git" not in runtime:
        fail("runtime image must assert git is installed")
    if "command -v shellcheck" not in runtime:
        fail("runtime image must assert shellcheck is installed")
    if not re.search(r"^\s+libcap \\$", runtime, re.MULTILINE):
        fail("runtime image must install libcap, which ships getcap")
    if "command -v getcap" not in runtime:
        fail("runtime image must assert getcap is installed")


def load_launcher() -> object:
    loader = importlib.machinery.SourceFileLoader("launcher", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("launcher", loader)
    if spec is None or spec.loader is None:
        fail(f"unable to load {LAUNCHER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def final_stage(source: str) -> str:
    start = source.rfind("FROM docker.io/library/fedora:44")
    if start == -1:
        fail("Containerfile has no final Fedora runtime stage")
    return source[start:]


def check_entrypoint_capabilities(script: str, launcher: object) -> None:
    # The prelude applies whatever list it is handed, so it validates each name
    # against its own copy of the allow-list. That copy must be the launcher's
    # list, or a capability the launcher refuses could still be applied by an
    # image ENV.
    match = re.search(r'^ALLOWED_CAPABILITIES="([^"]*)"$', script, re.MULTILINE)
    if match is None:
        fail('entrypoint must declare ALLOWED_CAPABILITIES="..."')
    names = match.group(1).split()
    allowed = getattr(launcher, "ALLOWED_CAPABILITIES", None)
    if not isinstance(allowed, frozenset) or not all(
        isinstance(name, str) for name in allowed
    ):
        fail("bin/opencode-container does not define ALLOWED_CAPABILITIES as a frozenset")
    if sorted(names) != sorted(allowed):
        fail(
            "entrypoint ALLOWED_CAPABILITIES drifted from the launcher's:\n"
            f"  entrypoint: {sorted(names)}\n"
            f"  launcher:   {sorted(allowed)}"
        )


def check_nested_podman(source: str, launcher: object) -> None:
    # The nested engine ships by default; the launcher detects it by label and
    # the entrypoint prelude must wrap every command, including the CMD.
    runtime = final_stage(source)
    args = re.findall(r"^ARG\s+NESTED_PODMAN=(\S*)\s*$", runtime, re.MULTILINE)
    if args != ["1"]:
        fail(f"final stage must declare ARG NESTED_PODMAN=1 exactly once, got {args}")
    labels = re.findall(r'^LABEL\s+' + re.escape(NESTED_LABEL) + r'="([^"]*)"\s*$',
                        runtime, re.MULTILINE)
    if labels != ["${NESTED_PODMAN}"]:
        fail(f'final stage must LABEL {NESTED_LABEL}="${{NESTED_PODMAN}}", got {labels}')
    if getattr(launcher, "NESTED_PODMAN_LABEL", None) != NESTED_LABEL:
        fail(f"launcher NESTED_PODMAN_LABEL must be {NESTED_LABEL!r}")
    agent_env_labels = re.findall(
        r'^LABEL\s+' + re.escape(AGENT_ENV_LABEL) + r'="([^"]*)"\s*$',
        runtime, re.MULTILINE)
    if agent_env_labels != ["1"]:
        fail(f'final stage must LABEL {AGENT_ENV_LABEL}="1", got {agent_env_labels}')
    if getattr(launcher, "AGENT_ENV_LABEL", None) != AGENT_ENV_LABEL:
        fail(f"launcher AGENT_ENV_LABEL must be {AGENT_ENV_LABEL!r}")

    entrypoints = re.findall(r"^ENTRYPOINT\s+(.*?)\s*$", source, re.MULTILINE)
    if len(entrypoints) != 1:
        fail(f"expected exactly one ENTRYPOINT instruction, found {len(entrypoints)}")
    try:
        entrypoint = json.loads(entrypoints[0])
    except ValueError:
        fail(f"ENTRYPOINT is not a JSON array: {entrypoints[0]}")
    if entrypoint != EXPECTED_ENTRYPOINT:
        fail(f"ENTRYPOINT drifted from {EXPECTED_ENTRYPOINT}: {entrypoint}")
    if not re.search(r"^COPY\s+image/opencode-entrypoint\s.*/usr/local/libexec/\s*$",
                     runtime, re.MULTILINE):
        fail("final stage must COPY image/opencode-entrypoint to /usr/local/libexec/")
    if not re.search(r"^COPY\s+image/opencode-entrypoint\s.*opencode-agent-env\s.*"
                     r"/usr/local/libexec/\s*$", runtime, re.MULTILINE):
        fail("final stage must COPY image/opencode-agent-env to /usr/local/libexec/")
    joined_runtime = re.sub(r"\\\n\s*", " ", runtime)
    if not re.search(r"chmod 0755 [^;\n]*/usr/local/libexec/opencode-agent-env",
                     joined_runtime):
        fail("the agent environment loader must be installed executable")

    # The setuid policy runs in the base and again after the dev image's
    # package installs, so neither image widens it.
    policy = '/usr/local/libexec/opencode-setuid-policy "${NESTED_PODMAN}"'
    if policy not in runtime:
        fail("final stage must run opencode-setuid-policy with NESTED_PODMAN")
    try:
        dev = CONTAINERFILE_DEV.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"unable to read Containerfile.dev: {exc}")
    if "/usr/local/libexec/opencode-setuid-policy" not in dev:
        fail("Containerfile.dev must re-run opencode-setuid-policy after installing packages")
    # A baked engine address would hijack the nested engine's podman.
    if re.search(r"^\s*(ENV\b.*)?\b(CONTAINER_HOST|DOCKER_HOST)=", dev, re.MULTILINE):
        fail("Containerfile.dev must not bake CONTAINER_HOST or DOCKER_HOST")

    # The prelude drops privileges only after the cgroup is delegated, and a
    # non-root start is passed straight through.
    try:
        script = ENTRYPOINT_SCRIPT.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"unable to read {ENTRYPOINT_SCRIPT}: {exc}")
    passthrough = re.search(
        r'if \[ "\$\(/usr/bin/id -u\)" != 0 \]; then\s+'
        r'PATH=\$saved_path\s+export PATH\s+exec "\$@"\s+fi',
        script,
    )
    if passthrough is None:
        fail("entrypoint must restore PATH and exec the command for a non-root start")
    drop = script.rfind("exec /usr/bin/setpriv --reuid")
    if drop == -1:
        fail("entrypoint must drop to the image user with setpriv")
    if '--inh-caps="$capability_ops" --ambient-caps="$capability_ops"' not in script[drop:]:
        fail("entrypoint must carry the configured capabilities on both sets at the drop")
    if "capability_ops=-all" not in script:
        fail("entrypoint must start the capability list from -all")
    if "OPENCODE_AGENT_CAPABILITIES" not in script:
        fail("entrypoint must read the launcher's capability list")
    if script.find("cgroup.subtree_control") > drop:
        fail("entrypoint must delegate the cgroup before dropping privileges")

    # The image's setuid/setgid policy is verified at startup, before the drop,
    # and names the same two helpers opencode-setuid-policy leaves in place.
    try:
        setuid_policy = (ROOT / "image" / "opencode-setuid-policy").read_text(
            encoding="utf-8")
    except OSError as exc:
        fail(f"unable to read image/opencode-setuid-policy: {exc}")
    check_start = script.find("setuid_files=")
    if check_start == -1 or check_start > drop:
        fail("entrypoint must verify the image's setuid policy before the drop")
    if "unexpected setuid/setgid files" not in script[check_start:]:
        fail("entrypoint must refuse an image with unexpected setuid/setgid files")
    if "-perm /6000" not in script[check_start:]:
        fail("entrypoint must list setuid/setgid files with find -perm /6000")
    for helper in ("/usr/bin/newuidmap", "/usr/bin/newgidmap"):
        if helper not in script[check_start:] or helper not in setuid_policy:
            fail(f"the setuid policy check must name {helper}")
    if "/usr/sbin/getcap" not in script[check_start:]:
        fail("entrypoint must check for file capabilities with getcap")
    if 'if [ ! -x /usr/sbin/getcap ]' not in script:
        fail("entrypoint must refuse a start when getcap is missing")
    if "/usr/bin/find / -xdev -type f -perm /111 -exec /usr/sbin/getcap {} +" not in script:
        fail("entrypoint must ask getcap about every executable on the root filesystem")
    check_entrypoint_capabilities(script, launcher)

    # The agent environment file: one fixed path, agreed by the launcher, the
    # prelude and the loader, and never applied by sourcing or eval.
    launcher_agent_env = getattr(launcher, "AGENT_ENV_TARGET", None)
    if launcher_agent_env != "/run/opencode/agent-env":
        fail(f"launcher AGENT_ENV_TARGET must be '/run/opencode/agent-env', "
             f"got {launcher_agent_env!r}")
    if not re.search(rf"^AGENT_ENV={re.escape(launcher_agent_env)}$", script,
                     re.MULTILINE):
        fail("the prelude's agent environment path drifted from the launcher's")
    try:
        loader = LOADER_SCRIPT.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"unable to read {LOADER_SCRIPT}: {exc}")
    if not re.search(rf"^AGENT_ENV={re.escape(launcher_agent_env)}$", loader,
                     re.MULTILINE):
        fail("the agent environment loader's path drifted from the launcher's")
    if re.search(r"^\s*(eval|source|\.)\s", loader, re.MULTILINE):
        fail("the agent environment loader must not eval or source the file")
    if "read -r -d ''" not in loader:
        fail("the agent environment loader must read NUL-separated entries")
    if 'export "$entry"' not in loader:
        fail("the agent environment loader must export each entry literally")
    if 'set -- "$AGENT_ENV_LOADER" "$@"' not in script:
        fail("the prelude must route the command through the agent environment loader")
    joined = re.sub(r"\\\n\s*", " ", script)
    if not re.search(r'exec /usr/bin/setpriv [^\n]*-- "\$@"$', joined, re.MULTILINE):
        fail("the prelude must exec the dropped command through setpriv")

    # Command resolution: the script sets its own PATH and calls every external
    # command by absolute path, so a workspace PATH cannot redirect them. The
    # incoming PATH is saved and restored only for the user's command.
    if not re.search(r"^PATH=/usr/sbin:/usr/bin$", script, re.MULTILINE):
        fail("entrypoint must set PATH=/usr/sbin:/usr/bin for its own commands")
    if not re.search(r"^saved_path=\$\{?PATH", script, re.MULTILINE):
        fail("entrypoint must save the incoming PATH before overriding it")
    if not re.search(r"^PATH=\$saved_path$", script, re.MULTILINE):
        fail("entrypoint must restore the incoming PATH for the user's command")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    for command in ("id", "setpriv", "mkdir", "chown", "cat", "grep",
                    "getent", "cut", "tty", "find", "sort", "getcap", "tr"):
        bare = re.search(rf"(?<![\w/.-]){command}(?![\w.-])", code)
        if bare is not None:
            fail(f"entrypoint invokes {command} without an absolute path")

    # Cgroup delegation chowns the delegated directory and its three control
    # files, never the recursive tree, per the kernel's cgroup v2 guidance.
    joined = re.sub(r"\\\n\s*", " ", script)
    if "chown -R" in joined:
        fail("entrypoint must not chown -R the cgroup tree")
    if not re.search(r'for delegated in "\$cg/agent" "\$cg/agent/self"', joined):
        fail("entrypoint must delegate both $cg/agent and $cg/agent/self")
    delegated = [
        line for line in joined.splitlines()
        if "/usr/bin/chown" in line and "$delegated" in line
    ]
    if not delegated:
        fail("entrypoint must chown the delegated cgroup and its control files")
    for control in ("cgroup.procs", "cgroup.threads", "cgroup.subtree_control"):
        if control not in delegated[0]:
            fail(f"entrypoint cgroup delegation must chown {control}")


def main() -> None:
    try:
        source = CONTAINERFILE.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"unable to read Containerfile: {exc}")
    config = load_json(CONTAINER_CONFIG)
    example = load_json(EXAMPLE)

    check_command(source)
    check_default_agent(config)
    check_plugins(config)
    check_skill_copy_destinations(config, source)
    check_opencode2_version(source, example)
    check_ai_devtools_ref(source)
    check_permissions(config)
    check_singular_plugin(config)
    check_mcp_servers(config)
    check_policy_asset_permissions(source)
    check_runtime_tools(source)
    check_nested_podman(source, load_launcher())


if __name__ == "__main__":
    main()
