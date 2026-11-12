#!/usr/bin/env python3
"""Launcher contract tests against a mocked podman.

Python port of the retired launcher-env.test.sh. The launcher runs under a
fake `podman` and `id` on PATH; the tests assert on the exact argv and
environment the mock recorded.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "opencode-container"

# Contract value: the container directory the runtime scans for agent
# definitions in addition to the one the image bakes.
AGENTS_TARGET = "/opt/opencode/config/opencode/agents"

# A minimal definition the launcher accepts: fenced frontmatter and a body.
AGENT_BODY = "---\ndescription: Lane.\nmode: subagent\n---\nBody.\n"

MOCK_PODMAN = """\
#!/usr/bin/env python3
import os
import sys

argv = sys.argv[1:]

if argv[:2] == ["image", "exists"]:
    sys.exit(0 if os.environ.get("PODMAN_IMAGE_EXISTS", "1") == "1" else 1)

if argv[:2] == ["image", "inspect"]:
    # One label per call: each label has its own variable. The fixture image
    # is built with the nested engine and the agent environment loader (the
    # launcher defaults), so a config that says nothing still launches.
    if any("io.opencode.nested-podman" in arg for arg in argv):
        value = os.environ.get("PODMAN_IMAGE_NESTED_PODMAN", "1")
    elif any("io.opencode.agent-env" in arg for arg in argv):
        value = os.environ.get("PODMAN_IMAGE_AGENT_ENV", "1")
    else:
        value = os.environ.get("PODMAN_IMAGE_LOCAL_PROVIDERS_SHA256", "")
    if not value:
        sys.exit(1)
    print(value)
    sys.exit(0)

if argv[:2] == ["secret", "exists"]:
    available = os.environ.get("AVAILABLE_SECRETS", "")
    name = argv[2] if len(argv) > 2 else ""
    sys.exit(0 if f",{name}," in f",{available}," else 1)

if argv[:2] == ["secret", "create"]:
    if len(argv) < 4 or argv[3] != "-":
        print(f"unexpected podman invocation: {' '.join(argv)}", file=sys.stderr)
        sys.exit(1)
    with open(os.environ["SECRET_LOG"], "a", encoding="utf-8") as stream:
        stream.write(f"create {argv[2]}\\n")
    with open(os.environ["SECRET_VALUE_LOG"], "wb") as stream:
        stream.write(sys.stdin.buffer.read())
    sys.exit(0)

if argv[:2] == ["secret", "rm"]:
    with open(os.environ["SECRET_LOG"], "a", encoding="utf-8") as stream:
        stream.write(f"rm {argv[2]}\\n")
    sys.exit(0)

if argv and argv[0] == "run":
    with open(os.environ["ARGV_LOG"], "w", encoding="utf-8") as stream:
        for arg in argv:
            stream.write(arg + "\\n")
    with open(os.environ["ENV_LOG"], "w", encoding="utf-8") as stream:
        stream.write(f"OPENAI_API_KEY={os.environ.get('OPENAI_API_KEY', '')}\\n")
        stream.write(f"EMPTY_FORWARD_SET={'x' if 'EMPTY_FORWARD' in os.environ else ''}\\n")
        stream.write(f"EMPTY_FORWARD={os.environ.get('EMPTY_FORWARD', '')}\\n")
    # The launcher mounts project-supplied variables as a read-only file. Copy
    # it while it exists: the launcher removes it once the run finishes, and the
    # tests assert both the content and the removal.
    for index, arg in enumerate(argv[:-1]):
        if arg != "-v":
            continue
        mount = argv[index + 1]
        if not mount.endswith(":/run/opencode/agent-env:ro"):
            continue
        source = mount.rsplit(":", 2)[0]
        with open(source, "rb") as agent_env:
            payload = agent_env.read()
        with open(os.environ["AGENT_ENV_LOG"], "wb") as stream:
            stream.write(payload)
        with open(os.environ["AGENT_ENV_META_LOG"], "w", encoding="utf-8") as stream:
            stream.write(f"{source}\\n")
            stream.write(f"{oct(os.stat(source).st_mode & 0o777)}\\n")
    sys.exit(0)

if argv and argv[0] == "build":
    with open(os.environ["BUILD_ARGV_LOG"], "w", encoding="utf-8") as stream:
        for arg in argv:
            stream.write(arg + "\\n")
    sys.exit(0)

print(f"unexpected podman invocation: {' '.join(argv)}", file=sys.stderr)
sys.exit(1)
"""

# getsubids reports one range per line: "<index>: <owner> <start> <count>".
MOCK_GETSUBIDS = """\
#!/usr/bin/env python3
import os
import sys

groups = sys.argv[1:2] == ["-g"]
count = os.environ.get("FAKE_SUBGID_COUNT" if groups else "FAKE_SUBUID_COUNT", "65536")
if count == "missing":
    sys.exit(1)
print(f"0: {sys.argv[-1]} 524288 {count}")
"""

MOCK_ID = """\
#!/usr/bin/env python3
import os
import subprocess
import sys

if os.environ.get("FAKE_UID") and sys.argv[1:2] == ["-u"]:
    print(os.environ["FAKE_UID"])
    sys.exit(0)
sys.exit(subprocess.call(["/usr/bin/id", *sys.argv[1:]]))
"""

# git delegates to the real binary, except that FAKE_GIT_COMMON_DIR makes
# `rev-parse --git-common-dir` answer with an arbitrary path. That is the only
# way a test running as an ordinary user can present a common directory under a
# root-owned system path the launcher reserves.
MOCK_GIT = """\
#!/usr/bin/env python3
import os
import subprocess
import sys

argv = sys.argv[1:]
common = os.environ.get("FAKE_GIT_COMMON_DIR", "")
if common and "rev-parse" in argv and "--git-common-dir" in argv:
    print(common)
    sys.exit(0)
sys.exit(subprocess.call(["/usr/bin/git", *argv]))
"""


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def make_unix_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(path))
    finally:
        sock.close()


def sha256_16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def has_pair(lines: list[str], flag: str, value: str) -> bool:
    return any(
        lines[index] == flag and lines[index + 1] == value
        for index in range(len(lines) - 1)
    )


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        fail(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class Suite:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.workspace = tmp / "workspace"
        self.test_home = tmp / "test-home"
        self.argv_log = tmp / "argv.log"
        self.env_log = tmp / "env.log"
        self.agent_env_log = tmp / "agent-env.bin"
        self.agent_env_meta_log = tmp / "agent-env.meta"
        self.build_argv_log = tmp / "build-argv.log"
        self.secret_log = tmp / "secret.log"
        self.secret_value_log = tmp / "secret-value"
        for path in (self.bin, self.workspace, self.test_home):
            path.mkdir()
        self._write_mocks()
        self._write_workspace_config()

    # --- harness ------------------------------------------------------------

    def _write_mocks(self) -> None:
        (self.bin / "podman").write_text(MOCK_PODMAN, encoding="utf-8")
        (self.bin / "id").write_text(MOCK_ID, encoding="utf-8")
        (self.bin / "getsubids").write_text(MOCK_GETSUBIDS, encoding="utf-8")
        (self.bin / "git").write_text(MOCK_GIT, encoding="utf-8")
        for tool in ("podman", "id", "getsubids", "git"):
            os.chmod(self.bin / tool, 0o755)

    def _write_workspace_config(self) -> None:
        write_json(self.workspace / ".opencode-sandbox.json", {
            "schema_version": 1,
            "image": "opencode2:test",
            "workspace": ".",
            "containers": False,
            "mounts": [],
            "env": {
                "pass": ["OPENAI_API_KEY", "EMPTY_FORWARD"],
                "set": {"TZ": "UTC"},
            },
            "network": "none",
            "capabilities": [],
            "runtime_args": [],
            "command": ["/bin/true"],
        })

    def base_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        # The host's XDG base directories would point the launcher at the real
        # home instead of the test's; derive them from HOME unless a test sets them.
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            env.pop(name, None)
        env["HOME"] = str(self.test_home)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        env["ARGV_LOG"] = str(self.argv_log)
        env["ENV_LOG"] = str(self.env_log)
        env["AGENT_ENV_LOG"] = str(self.agent_env_log)
        env["AGENT_ENV_META_LOG"] = str(self.agent_env_meta_log)
        env["BUILD_ARGV_LOG"] = str(self.build_argv_log)
        env["SECRET_LOG"] = str(self.secret_log)
        env["SECRET_VALUE_LOG"] = str(self.secret_value_log)
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        cwd: Path,
        args: tuple[str, ...] = (),
        via_path: bool = False,
        extra_env: dict[str, str] | None = None,
        stdin: int | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = ["opencode-container", *args] if via_path else [str(LAUNCHER), *args]
        if input_text is not None:
            return subprocess.run(cmd, cwd=cwd, env=self.base_env(extra_env),
                                  input=input_text,
                                  capture_output=True, text=True, check=False)
        return subprocess.run(cmd, cwd=cwd, env=self.base_env(extra_env),
                              stdin=stdin,
                              capture_output=True, text=True, check=False)

    def run_ok(
        self,
        cwd: Path,
        args: tuple[str, ...] = (),
        via_path: bool = False,
        extra_env: dict[str, str] | None = None,
        stdin: int | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        proc = self.run(cwd, args, via_path, extra_env, stdin, input_text)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            fail(f"launcher unexpectedly failed: {cwd} {' '.join(args)} (exit {proc.returncode})")
        return proc

    def run_fail(
        self,
        cwd: Path,
        reject: str,
        args: tuple[str, ...] = (),
        via_path: bool = False,
        extra_env: dict[str, str] | None = None,
        stdin: int | None = None,
        input_text: str | None = None,
        stderr_contains: str | tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess:
        proc = self.run(cwd, args, via_path, extra_env, stdin, input_text)
        if proc.returncode == 0:
            fail(reject)
        # A bare string is one message, not a set of characters to find.
        needles = (stderr_contains,) if isinstance(stderr_contains, str) else stderr_contains
        for needle in needles:
            if needle not in proc.stderr:
                sys.stderr.write(proc.stderr)
                fail(f"expected {needle!r} in launcher stderr")
        return proc

    def _lines(self, path: Path) -> list[str]:
        if not path.exists():
            fail(f"missing log file: {path.name}")
        return path.read_text(encoding="utf-8").splitlines()

    def argv(self) -> list[str]:
        return self._lines(self.argv_log)

    def build_argv(self) -> list[str]:
        return self._lines(self.build_argv_log)

    def env_lines(self) -> list[str]:
        return self._lines(self.env_log)

    def agent_env(self) -> bytes:
        if not self.agent_env_log.exists():
            fail("no agent environment file was mounted")
        return self.agent_env_log.read_bytes()

    def agent_env_source(self) -> Path:
        lines = self._lines(self.agent_env_meta_log)
        return Path(lines[0])

    def agent_env_mode(self) -> str:
        return self._lines(self.agent_env_meta_log)[1]

    def secret_lines(self) -> list[str]:
        if not self.secret_log.exists():
            return []
        return self.secret_log.read_text(encoding="utf-8").splitlines()

    def secret_value(self) -> bytes:
        return self.secret_value_log.read_bytes()

    def has_line(self, lines: list[str], needle: str) -> None:
        if needle not in lines:
            fail(f"expected exact log line {needle!r}; got:\n" + "\n".join(lines))

    def has_substring(self, lines: list[str], needle: str) -> None:
        if not any(needle in line for line in lines):
            fail(f"expected a log line containing {needle!r}; got:\n" + "\n".join(lines))

    def no_line(self, lines: list[str], needle: str) -> None:
        if needle in lines:
            fail(f"log must not contain exact line {needle!r}")

    def no_substring(self, lines: list[str], needle: str) -> None:
        if any(needle in line for line in lines):
            fail(f"log must not contain {needle!r}")

    def count(self, lines: list[str], needle: str) -> int:
        return sum(1 for line in lines if line == needle)

    def forget_agent_env(self) -> None:
        self.agent_env_log.unlink(missing_ok=True)
        self.agent_env_meta_log.unlink(missing_ok=True)

    def set_config(self, path: Path, **changes: object) -> None:
        write_json(path, load_json(path) | changes)

    def derive_config(self, src: Path, dest: Path, **changes: object) -> None:
        write_json(dest, load_json(src) | changes)

    # --- sections (in the shell script's order) -----------------------------

    def _basic_env_forwarding(self) -> None:
        self.run_ok(self.workspace, extra_env={
            "OPENAI_API_KEY": "launch-secret-sentinel",
            "EMPTY_FORWARD": "",
        })
        argv = self.argv()
        self.has_line(argv, "OPENAI_API_KEY")
        self.has_line(argv, "EMPTY_FORWARD")
        self.has_line(argv, "TZ=UTC")
        self.has_line(argv, f"{self.workspace}/.opencode-sandbox.json:/run/opencode/sandbox.json:ro")
        self.no_substring(argv, "OPENCODE_MODEL_ROUTER")
        # Engine-free mode starts as the image user, so there is no pre-drop
        # window: values stay `-e` and no agent environment file is written.
        self.no_substring(argv, ":/run/opencode/agent-env:ro")
        self.no_substring(argv, "OPENCODE_AGENT_CAPABILITIES")
        key = sha256_16(str(self.workspace))
        self.has_line(argv, f"opencode2-data-{key}:/var/lib/opencode-data:U")
        self.has_line(argv, "XDG_DATA_HOME=/var/lib/opencode-data")
        self.has_line(argv, "XDG_STATE_HOME=/var/lib/opencode-data/state")
        self.has_line(argv, f"{self.workspace}:/workspace/{key}")
        self.has_line(argv, f"/workspace/{key}")
        self.no_substring(argv, "/run/opencode-container-engine.sock")
        if self.count(argv, "OPENAI_API_KEY") != 1:
            fail("OPENAI_API_KEY was forwarded more than once")
        self.no_substring(argv, "launch-secret-sentinel")
        env = self.env_lines()
        self.has_line(env, "OPENAI_API_KEY=launch-secret-sentinel")
        self.has_line(env, "EMPTY_FORWARD_SET=x")
        self.has_line(env, "EMPTY_FORWARD=")

    def _seccomp_config(self) -> None:
        def workspace(name: str, seccomp: object = ...) -> Path:
            root = self.tmp / f"seccomp-{name}"
            root.mkdir()
            config: dict[str, object] = {
                "image": "opencode2:test",
                "workspace": ".",
                "command": ["/bin/true"],
            }
            if seccomp is not ...:
                config["seccomp"] = seccomp
            write_json(root / ".opencode-sandbox.json", config)
            return root

        # Missing and null retain the engine's default seccomp policy: the
        # launcher's existing label security option is the only one emitted.
        # (A distinct loop variable: mypy would otherwise infer `value` from
        # this loop and reject the later loops' string/int/list/dict values.)
        for name, placeholder in (("missing", ...), ("null", None)):
            root = workspace(name, placeholder)
            self.run_ok(root)
            self.no_substring(self.argv(), "seccomp=")

        root = workspace("unconfined", "unconfined")
        self.run_ok(root)
        self.has_line(self.argv(), "seccomp=unconfined")

        # A relative profile is rooted at the invocation/config directory, not
        # at any configured workspace redirect, and Podman gets its real path.
        root = workspace("relative", "profiles/filter.json")
        profile = root / "profiles" / "filter.json"
        profile.parent.mkdir()
        profile.write_text("{}\n", encoding="utf-8")
        redirected = self.tmp / "seccomp-redirected-workspace"
        redirected.mkdir()
        self.set_config(root / ".opencode-sandbox.json", workspace=str(redirected))
        self.run_ok(root)
        self.has_line(self.argv(), f"seccomp={profile.resolve()}")

        absolute_profile = self.tmp / "absolute-seccomp.json"
        absolute_profile.write_text("{}\n", encoding="utf-8")
        absolute_link = self.tmp / "absolute-seccomp-link.json"
        absolute_link.symlink_to(absolute_profile)
        root = workspace("absolute", str(absolute_link))
        self.run_ok(root)
        self.has_line(self.argv(), f"seccomp={absolute_profile.resolve()}")

        for name, value in (
            ("boolean", False),
            ("number", 1),
            ("array", ["profile.json"]),
            ("object", {"profile": "profile.json"}),
        ):
            root = workspace(f"invalid-{name}", value)
            self.run_fail(
                root,
                f"launcher accepted {name} seccomp value",
                stderr_contains="seccomp must be a string or null",
            )

        for name, text in (("empty", ""), ("whitespace", " \t\n")):
            root = workspace(name, text)
            self.run_fail(
                root,
                f"launcher accepted {name} seccomp value",
                stderr_contains="seccomp must not be empty or whitespace",
            )

        root = workspace("missing-profile", "profiles/missing.json")
        self.run_fail(
            root,
            "launcher accepted a missing seccomp profile",
            stderr_contains="seccomp profile does not exist",
        )

        root = workspace("directory-profile", "profiles")
        (root / "profiles").mkdir()
        self.run_fail(
            root,
            "launcher accepted a directory as a seccomp profile",
            stderr_contains="seccomp profile is not a regular file",
        )

        # The dedicated field does not widen runtime_args: generic security
        # options remain outside its intentionally narrow pass-through list.
        root = workspace("runtime-arg")
        self.set_config(
            root / ".opencode-sandbox.json",
            runtime_args=["--security-opt=seccomp=unconfined"],
        )
        self.run_fail(
            root,
            "launcher accepted a generic security option in runtime_args",
            stderr_contains="runtime_args entry is not allowed",
        )

    def _containers_socket_selection(self) -> None:
        # Opting into host container-engine access prefers the user Podman
        # socket over Docker, manages both client variables, and mirrors the
        # workspace at its host path so nested bind mounts do not reference
        # the generated /workspace path.
        workspace = self.tmp / "containers-workspace"
        (workspace / "subdir").mkdir(parents=True)
        runtime = self.tmp / "containers-runtime"
        runtime.mkdir()
        make_unix_socket(runtime / "podman" / "podman.sock")
        make_unix_socket(runtime / "docker.sock")
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:test",
            "containers": "host",
            "workdir": "subdir",
            "env": {
                "set": {
                    "CONTAINER_HOST": "tcp://must-not-win.invalid",
                    "DOCKER_HOST": "tcp://must-not-win.invalid",
                },
            },
            "command": ["/bin/true"],
        })
        self.run_ok(workspace, extra_env={"XDG_RUNTIME_DIR": str(runtime)})
        argv = self.argv()
        self.has_line(argv, f"{runtime}/podman/podman.sock:/run/opencode-container-engine.sock")
        self.no_substring(argv, f"{runtime}/docker.sock:/run/opencode-container-engine.sock")
        self.has_line(argv, f"{workspace}:/src")
        self.has_line(argv, f"{workspace}:{workspace}")
        self.has_line(argv, f"{workspace}/subdir")
        self.has_line(argv, "CONTAINER_HOST=unix:///run/opencode-container-engine.sock")
        self.has_line(argv, "DOCKER_HOST=unix:///run/opencode-container-engine.sock")
        self.no_substring(argv, "must-not-win.invalid")
        self.no_substring(argv, "/workspace/")

        # Host-path workdirs and additional mounts may not escape or shadow
        # the mirror whose identity nested container bind mounts rely on.
        cfg = workspace / ".opencode-sandbox.json"
        self.set_config(cfg, workdir="../outside")
        self.run_fail(workspace,
                      'containers "host" accepted a workdir outside the mirrored workspace',
                      extra_env={"XDG_RUNTIME_DIR": str(runtime)},
                      stderr_contains="must stay within workspace")
        self.set_config(cfg, workdir=".",
                        mounts=[{"source": ".", "target": f"{workspace}/subdir"}])
        self.run_fail(workspace,
                      'containers "host" accepted a mount overlapping the workspace mirror',
                      extra_env={"XDG_RUNTIME_DIR": str(runtime)},
                      stderr_contains="overlaps the containers workspace mirror")

    def _containers_rootless_docker(self) -> None:
        # With no user Podman socket, a rootless Docker socket is selected next.
        workspace = self.tmp / "docker-workspace"
        runtime = self.tmp / "docker-runtime"
        for path in (workspace, runtime):
            path.mkdir()
        make_unix_socket(runtime / "docker.sock")
        write_json(workspace / ".opencode-sandbox.json",
                   {"image": "opencode2:test", "containers": "host",
                    "command": ["/bin/true"]})
        self.run_ok(workspace, extra_env={"XDG_RUNTIME_DIR": str(runtime), "FAKE_UID": "424242"})
        argv = self.argv()
        self.has_line(argv, f"{runtime}/docker.sock:/run/opencode-container-engine.sock")
        self.no_line(argv, "keep-groups")

    def _containers_invalid_mode(self) -> None:
        # Values outside false/true/"host"/"nested" fail before Podman runs,
        # including the string spellings of the booleans.
        workspace = self.tmp / "containers-invalid"
        workspace.mkdir()
        for value in ("true", "false", "Nested", "", 1, ["nested"], {"mode": "nested"}):
            write_json(workspace / ".opencode-sandbox.json", {"containers": value})
            self.argv_log.unlink(missing_ok=True)
            self.run_fail(workspace, f"launcher accepted containers value {value!r}",
                          stderr_contains='containers must be false, true, "host", or "nested"')
            if self.argv_log.exists():
                fail(f"containers value {value!r} reached podman run")

    def _containers_host_socket(self) -> None:
        # "host" is the only spelling that mounts the host engine socket: the
        # argv that `true` used to produce, before the default moved to nested.
        workspace = self.tmp / "containers-host"
        runtime = self.tmp / "containers-host-runtime"
        for path in (workspace, runtime):
            path.mkdir()
        make_unix_socket(runtime / "podman" / "podman.sock")
        write_json(workspace / ".opencode-sandbox.json",
                   {"image": "opencode2:test", "containers": "host",
                    "env": {"set": {"HOST_MODE_VALUE": "kept as -e"}},
                    "command": ["/bin/true"]})
        self.run_ok(workspace, extra_env={"XDG_RUNTIME_DIR": str(runtime)})
        argv = self.argv()
        self.has_line(argv, f"{runtime}/podman/podman.sock:/run/opencode-container-engine.sock")
        self.has_line(argv, "CONTAINER_HOST=unix:///run/opencode-container-engine.sock")
        self.has_line(argv, "DOCKER_HOST=unix:///run/opencode-container-engine.sock")
        # Host mode also starts as the image user: project env stays `-e`.
        self.has_line(argv, "HOST_MODE_VALUE=kept as -e")
        self.no_substring(argv, ":/run/opencode/agent-env:ro")
        self.has_line(argv, f"{workspace}:{workspace}")
        self.no_line(argv, "SYS_ADMIN")
        self.no_line(argv, "--entrypoint")
        self.no_substring(argv, "/var/lib/opencode-containers")

    def _containers_default_is_nested(self) -> None:
        # Unset, true, and an explicit "nested" are one mode: the image's own
        # engine. true is no longer an alias for the host socket.
        workspace = self.tmp / "containers-default"
        (workspace / "subdir").mkdir(parents=True)
        runtime = self.tmp / "containers-default-runtime"
        runtime.mkdir()
        # A reachable host socket must still not be mounted by default.
        make_unix_socket(runtime / "podman" / "podman.sock")
        config = {"image": "opencode2:test", "workspace": ".", "workdir": "subdir",
                  "command": ["/bin/true"]}
        argvs = []

        def comparable(lines: list[str]) -> list[str]:
            # The agent environment file lives at a fresh private path each run;
            # its fixed mount target is the contract, not the host source.
            return [
                "<agent-env>:/run/opencode/agent-env:ro"
                if arg.endswith(":/run/opencode/agent-env:ro")
                else arg
                for arg in lines
            ]
        for value in (..., True, "nested"):
            write_json(workspace / ".opencode-sandbox.json",
                       config if value is ... else config | {"containers": value})
            self.run_ok(workspace, extra_env={"XDG_RUNTIME_DIR": str(runtime)})
            argvs.append(self.argv())
        for label, argv in zip((True, "nested"), argvs[1:]):
            if comparable(argv) != comparable(argvs[0]):
                fail(f"containers {label!r} differs from the unset default:\n"
                     + "\n".join(argvs[0]) + "\n---\n" + "\n".join(argv))

        argv = argvs[0]
        key = sha256_16(str(workspace))
        for flag, wanted in (
            ("--user", "0"),
            ("--entrypoint", "/usr/local/libexec/opencode-entrypoint"),
            ("--cap-add", "SYS_ADMIN"),
            ("--security-opt", "systempaths=unconfined"),
            ("--device", "/dev/net/tun"),
            ("-v", f"opencode2-containers-{key}:/var/lib/opencode-containers"),
        ):
            if not has_pair(argv, flag, wanted):
                fail(f"default nested argv lacks {flag} {wanted}:\n" + "\n".join(argv))
        # Starting as root, :U would chown the user's data volume to root.
        self.has_line(argv, f"opencode2-data-{key}:/var/lib/opencode-data")
        self.no_substring(argv, "opencode-data:U")
        self.has_line(argv, f"{workspace}:/workspace/{key}")
        self.has_line(argv, f"/workspace/{key}/subdir")
        # No host engine: no socket, no client variables, no host-path mirror.
        self.no_substring(argv, "/run/opencode-container-engine.sock")
        self.no_substring(argv, "CONTAINER_HOST")
        self.no_substring(argv, "DOCKER_HOST")
        self.no_line(argv, f"{workspace}:{workspace}")

    def _containers_false_has_no_engine(self) -> None:
        # false keeps the engine-free profile byte for byte: no socket, no
        # engine variables, no nested privileges, the image user, and :U.
        workspace = self.tmp / "containers-off"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json",
                   {"image": "opencode2:test", "containers": False,
                    "command": ["/bin/true"]})
        self.run_ok(workspace)
        argv = self.argv()
        key = sha256_16(str(workspace))
        for absent in ("--user", "--entrypoint", "--cap-add", "--device",
                       "systempaths=unconfined", "SYS_ADMIN", "/dev/net/tun"):
            self.no_line(argv, absent)
        self.no_substring(argv, "/run/opencode-container-engine.sock")
        self.no_substring(argv, "CONTAINER_HOST")
        self.no_substring(argv, "/var/lib/opencode-containers")
        self.has_line(argv, f"opencode2-data-{key}:/var/lib/opencode-data:U")
        self.has_line(argv, f"{workspace}:/workspace/{key}")

    def _containers_nested(self) -> None:
        # The nested engine: the image's own rootless Podman, with the outer
        # privileges it needs, a per-project store volume, and no host socket.
        workspace = self.tmp / "containers-nested"
        (workspace / "subdir").mkdir(parents=True)
        runtime = self.tmp / "containers-nested-runtime"
        runtime.mkdir()
        # A reachable host socket must still not be mounted in nested mode.
        make_unix_socket(runtime / "podman" / "podman.sock")
        cfg = workspace / ".opencode-sandbox.json"
        write_json(cfg, {
            "image": "opencode2:test",
            "containers": "nested",
            "workdir": "subdir",
            "command": ["/bin/true"],
        })
        nested_env = {"XDG_RUNTIME_DIR": str(runtime), "PODMAN_IMAGE_NESTED_PODMAN": "1"}
        self.run_ok(workspace, extra_env=nested_env)
        argv = self.argv()
        key = sha256_16(str(workspace))

        def pair(flag: str, value: str) -> None:
            if not has_pair(argv, flag, value):
                fail(f"nested argv lacks {flag} {value}:\n" + "\n".join(argv))

        pair("--user", "0")
        pair("--entrypoint", "/usr/local/libexec/opencode-entrypoint")
        pair("--cap-add", "SYS_ADMIN")
        pair("--security-opt", "systempaths=unconfined")
        pair("--security-opt", "label=disable")
        pair("--device", "/dev/net/tun")
        pair("-v", f"opencode2-containers-{key}:/var/lib/opencode-containers")
        # The capability variable is always passed, empty when nothing is
        # configured, so an image ENV cannot supply the list.
        self.has_line(argv, "OPENCODE_AGENT_CAPABILITIES=")
        if self.count(argv, "OPENCODE_AGENT_CAPABILITIES=") != 1:
            fail("nested mode passed the capability variable more than once")
        self.has_line(argv, "--userns=keep-id")
        self.no_line(argv, "/dev/fuse")
        self.no_line(argv, "--privileged")
        # Starting as root, :U would chown the user's data volume to root.
        self.has_line(argv, f"opencode2-data-{key}:/var/lib/opencode-data")
        self.no_substring(argv, "opencode-data:U")
        # The entrypoint checks that volume as the image user after the drop.
        self.has_line(argv, "OPENCODE_DATA_MOUNT=/var/lib/opencode-data")
        self.has_line(argv, f"OPENCODE_DATA_VOLUME=opencode2-data-{key}")
        # No host engine: no socket, no client variables, no host-path mirror.
        self.no_substring(argv, "/run/opencode-container-engine.sock")
        self.no_substring(argv, "CONTAINER_HOST")
        self.no_substring(argv, "DOCKER_HOST")
        self.no_line(argv, "keep-groups")
        self.no_line(argv, f"{workspace}:{workspace}")
        self.has_line(argv, f"{workspace}:/workspace/{key}")
        self.has_line(argv, f"/workspace/{key}/subdir")
        if argv[-2:] != ["opencode2:test", "/bin/true"]:
            fail(f"nested argv must end with the image and command: {argv[-2:]}")

        # Mounts may not shadow the inner engine's store.
        self.set_config(cfg, mounts=[{"source": ".", "target": "/var/lib/opencode-containers/x"}])
        self.run_fail(workspace, "nested mode accepted a mount shadowing the engine store",
                      extra_env=nested_env,
                      stderr_contains="shadows the nested engine's storage volume")
        self.set_config(cfg, mounts=[{"source": ".", "target": "/var/lib"}])
        self.run_fail(workspace, "nested mode accepted a mount containing the engine store",
                      extra_env=nested_env,
                      stderr_contains="shadows the nested engine's storage volume")
        # The baked-path guards still hold.
        self.set_config(cfg, mounts=[{"source": ".", "target": "/workspace/elsewhere"}])
        self.run_fail(workspace, "nested mode accepted a mount shadowing /workspace",
                      extra_env=nested_env,
                      stderr_contains="shadows a baked config/plugin path")
        self.set_config(cfg, mounts=[])

        # Refusals: an image without the engine, and a host whose subordinate
        # ranges cannot hold the inner engine's id maps. Nested is the default,
        # so an unset or true setting is refused exactly like the explicit
        # spelling, and every message says how to proceed.
        nested = load_json(cfg)
        unset = {field: value for field, value in nested.items() if field != "containers"}
        forms = (
            ("unset", unset),
            ("true", nested | {"containers": True}),
            ('"nested"', nested),
        )
        refusal_cases = (
            ("an image with no nested label",
             ('does not carry io.opencode.nested-podman="1"', '"containers": false'),
             {"PODMAN_IMAGE_NESTED_PODMAN": ""}),
            ("an image whose nested label is 0",
             ('does not carry io.opencode.nested-podman="1"', '"containers": false'),
             {"PODMAN_IMAGE_NESTED_PODMAN": "0"}),
            ("an image that predates project-env delivery",
             ("predates project-env delivery", "rebuild it", '"containers": false'),
             {"PODMAN_IMAGE_AGENT_ENV": ""}),
            ("a 65535-id subuid range",
             ("needs at least 65536 subordinate uids", '"containers": false'),
             {"FAKE_SUBUID_COUNT": "65535"}),
            ("a 1000-id subgid range",
             ("needs at least 65536 subordinate gids", '"containers": false'),
             {"FAKE_SUBGID_COUNT": "1000"}),
            ("a user with no subuid range",
             ("found 0", '"containers": false'),
             {"FAKE_SUBUID_COUNT": "missing"}),
        )
        for what, needles, env in refusal_cases:
            for label, document in forms:
                write_json(cfg, document)
                self.argv_log.unlink(missing_ok=True)
                self.run_fail(
                    workspace,
                    f"nested mode accepted {what} with containers {label}",
                    extra_env=nested_env | env,
                    stderr_contains=needles,
                )
                if self.argv_log.exists():
                    fail(f"containers {label}: podman run ran with {what}")

        # build never needs the engine checks: it runs no container, so an
        # unsupported host or image must not refuse a build either.
        for label, document in forms:
            write_json(cfg, document)
            self.run_ok(workspace, args=("build",),
                        extra_env=nested_env | {"PODMAN_IMAGE_NESTED_PODMAN": "",
                                                "FAKE_SUBUID_COUNT": "1"})

    def _nested_agent_environment(self) -> None:
        # Nested mode starts the prelude as root, so project-supplied variables
        # cannot travel as `-e`: they would be in the startup script's own
        # environment before the drop. They are written to a launcher-owned,
        # read-only file the image's loader applies after the drop instead.
        workspace = self.tmp / "agent-environment"
        workspace.mkdir()
        runtime = self.tmp / "agent-environment-runtime"
        runtime.mkdir()
        tricky = "spaces = and\nnewlines\tand $(no expansion) `either`"
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:test",
            "containers": "nested",
            "command": ["/bin/true"],
            "env": {
                "pass": ["AGENT_ENV_PASSED"],
                "set": {"TRICKY": tricky},
            },
        })
        self.forget_agent_env()
        self.run_ok(workspace, extra_env={
            "XDG_RUNTIME_DIR": str(runtime),
            "AGENT_ENV_PASSED": "passed = value with spaces",
        })
        argv = self.argv()
        # Not as process environment: neither names nor values reach argv.
        self.no_substring(argv, "TRICKY")
        self.no_line(argv, "AGENT_ENV_PASSED")
        self.no_substring(argv, "passed = value with spaces")
        self.no_line(argv, "AGENT_ENV_PASSED=passed = value with spaces")

        # The mount is read-only, at the fixed path, and its source lives
        # outside the workspace in a launcher-owned private directory.
        if not any(
            arg == "-v" and argv[index + 1].endswith(":/run/opencode/agent-env:ro")
            for index, arg in enumerate(argv[:-1])
        ):
            fail("nested argv lacks the read-only agent environment mount:\n"
                 + "\n".join(argv))
        source = self.agent_env_source()
        if source.is_relative_to(workspace):
            fail(f"agent environment file lives in the workspace: {source}")
        if not source.is_relative_to(runtime):
            fail(f"agent environment file is not under XDG_RUNTIME_DIR: {source}")
        if self.agent_env_mode() != "0o600":
            fail(f"agent environment file mode is not 0600: {self.agent_env_mode()}")

        # NUL separation round-trips spaces, `=` and newlines unchanged.
        payload = self.agent_env()
        if not payload.endswith(b"\0"):
            fail(f"agent environment file does not end with NUL: {payload!r}")
        entries = payload.split(b"\0")[:-1]
        expected = [
            b"AGENT_ENV_PASSED=passed = value with spaces",
            b"TRICKY=" + tricky.encode("utf-8"),
        ]
        if sorted(entries) != sorted(expected):
            fail(f"agent environment file round-trip differs: {entries!r}")

        # Removed once the run finishes, with its private directory.
        if source.exists():
            fail(f"agent environment file survived the run: {source}")
        if source.parent.exists():
            fail(f"agent environment directory survived the run: {source.parent}")

        # The prelude's own fixed variables and the launcher settings still
        # travel as `-e`; only project-supplied values moved.
        self.has_line(argv, "XDG_DATA_HOME=/var/lib/opencode-data")
        self.has_line(argv, "OPENCODE_DATA_MOUNT=/var/lib/opencode-data")

        # Off mode keeps the old argv: no file, and the same `-e` entries.
        self.forget_agent_env()
        self.set_config(workspace / ".opencode-sandbox.json", containers=False)
        self.run_ok(workspace, extra_env={"AGENT_ENV_PASSED": "passed = value with spaces"})
        argv = self.argv()
        self.has_substring(argv, "TRICKY=spaces = and")
        self.has_line(argv, "AGENT_ENV_PASSED")
        self.no_substring(argv, ":/run/opencode/agent-env:ro")
        if self.agent_env_log.exists():
            fail("off mode wrote an agent environment file")

    def _bash_readonly_env_names(self) -> None:
        # The image's loader is bash, where exporting one of bash's readonly
        # variables fails with "readonly variable" after the container has
        # started. Refused at launch, in every mode, whether the name is passed
        # or given a literal.
        workspace = self.tmp / "bash-readonly-env"
        workspace.mkdir()
        runtime = self.tmp / "bash-readonly-env-runtime"
        runtime.mkdir()
        cfg = workspace / ".opencode-sandbox.json"
        names = ("SHELLOPTS", "BASHOPTS", "UID", "EUID", "PPID", "BASH_VERSINFO")
        for mode in (False, "nested"):
            for name in names:
                for document in (
                    {"env": {"set": {name: "value"}}},
                    {"env": {"pass": [name]}},
                ):
                    write_json(cfg, {
                        "image": "opencode2:test",
                        "containers": mode,
                        "command": ["/bin/true"],
                    } | document)
                    self.argv_log.unlink(missing_ok=True)
                    self.run_fail(
                        workspace,
                        f"containers {mode!r} accepted the bash readonly name {name}",
                        extra_env={"XDG_RUNTIME_DIR": str(runtime)},
                        stderr_contains=(name, "readonly in the image's bash"),
                    )
                    if self.argv_log.exists():
                        fail(f"containers {mode!r} reached podman with {name}")

    def _non_utf8_env_pass(self) -> None:
        # A host environment value that is not valid UTF-8 (Python represents
        # those bytes as surrogates) must reach the agent's file byte for byte,
        # and the private file and directory must not be left behind.
        workspace = self.tmp / "non-utf8-env"
        workspace.mkdir()
        runtime = self.tmp / "non-utf8-env-runtime"
        runtime.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:test",
            "containers": "nested",
            "command": ["/bin/true"],
            "env": {"pass": ["NON_UTF8"]},
        })
        self.forget_agent_env()
        self.run_ok(workspace, extra_env={
            "XDG_RUNTIME_DIR": str(runtime),
            "NON_UTF8": "caf\udcff",
        })
        entries = self.agent_env().split(b"\0")[:-1]
        if entries != [b"NON_UTF8=caf\xff"]:
            fail(f"the agent environment file mangled a non-UTF-8 value: {entries!r}")
        source = self.agent_env_source()
        if source.exists() or source.parent.exists():
            fail(f"the agent environment file or its directory survived: {source}")
        leftovers = list(runtime.iterdir())
        if leftovers:
            fail(f"the launcher left files behind in XDG_RUNTIME_DIR: {leftovers}")

    def _git_common_dir_system_path(self) -> None:
        # A workspace's Git common directory is mounted at its own path. It may
        # not resolve into a container system path: that mount would replace
        # /usr or /etc inside the sandbox, where the entrypoint, interpreter
        # and policy scripts live.
        workspace = self.tmp / "git-common-system"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:test",
            "containers": False,
            "command": ["/bin/true"],
        })
        for common in ("/usr/libexec", "/usr/share"):
            self.argv_log.unlink(missing_ok=True)
            self.run_fail(
                workspace,
                f"launcher accepted a git common directory at {common}",
                extra_env={"FAKE_GIT_COMMON_DIR": common},
                stderr_contains=("git metadata directory", "shadows a container system path"),
            )
            if self.argv_log.exists():
                fail(f"podman run ran with a git common directory at {common}")

    def _capability_policy(self) -> None:
        # The allow-list applies in every mode and normalises case and the CAP_
        # prefix; anything that could escape the agent's own files, ids,
        # processes or kernel state is refused by name.
        workspace = self.tmp / "capability-policy"
        workspace.mkdir()
        cfg = workspace / ".opencode-sandbox.json"
        base = {"image": "opencode2:test", "containers": False, "command": ["/bin/true"]}
        write_json(cfg, base | {"capabilities": []})
        self.run_ok(workspace)
        self.no_line(self.argv(), "--cap-add")

        allowed = ["NET_RAW", "CAP_NET_BIND_SERVICE", "net_admin", "NET_BROADCAST",
                   "sys_nice", "IPC_LOCK", "SYS_RESOURCE"]
        normalized = ["NET_RAW", "NET_BIND_SERVICE", "NET_ADMIN", "NET_BROADCAST",
                      "SYS_NICE", "IPC_LOCK", "SYS_RESOURCE"]
        write_json(cfg, base | {"capabilities": allowed})
        self.run_ok(workspace)
        argv = self.argv()
        for capability in normalized:
            self.has_line(argv, capability)
        if self.count(argv, "--cap-add") != len(normalized):
            fail("off mode did not pass exactly the configured capabilities")
        self.no_substring(argv, "OPENCODE_AGENT_CAPABILITIES")

        # Nested mode carries exactly that set through the drop: Podman's own
        # SYS_ADMIN plus the configured ones, named for the prelude.
        runtime = self.tmp / "capability-policy-runtime"
        runtime.mkdir()
        nested = {"image": "opencode2:test", "containers": "nested",
                  "command": ["/bin/true"],
                  "capabilities": ["NET_RAW", "cap_net_bind_service"]}
        write_json(cfg, nested)
        self.run_ok(workspace, extra_env={"XDG_RUNTIME_DIR": str(runtime)})
        argv = self.argv()
        self.has_pair_required(argv, "--cap-add", "NET_RAW")
        self.has_pair_required(argv, "--cap-add", "NET_BIND_SERVICE")
        self.has_line(argv, "OPENCODE_AGENT_CAPABILITIES=NET_RAW,NET_BIND_SERVICE")
        if self.count(argv, "--cap-add") != 3:
            fail("nested mode added a capability outside SYS_ADMIN and the configured set")

        # Refusals name the capability and the rule it breaks.
        refusals = (
            ("SYS_PTRACE", ("SYS_PTRACE", "is not allowed", "processes")),
            ("cap_sys_admin", ("SYS_ADMIN", "is not allowed", "namespaces")),
            ("CHOWN", ("CHOWN", "is not allowed", "file")),
            ("AUDIT_WRITE", ("AUDIT_WRITE", "is not allowed", "audit log")),
            ("SETUID", ("SETUID", "is not allowed", "uid")),
            ("NOT_A_CAPABILITY", ("unknown capability", "NOT_A_CAPABILITY")),
        )
        for capability, needles in refusals:
            write_json(cfg, base | {"capabilities": [capability]})
            self.argv_log.unlink(missing_ok=True)
            self.run_fail(
                workspace,
                f"accepted capability {capability}",
                stderr_contains=needles,
            )
            if self.argv_log.exists():
                fail(f"podman run ran with capability {capability}")

    def has_pair_required(self, lines: list[str], flag: str, value: str) -> None:
        if not has_pair(lines, flag, value):
            fail(f"argv lacks {flag} {value}:\n" + "\n".join(lines))

    def _containers_no_socket(self) -> None:
        # On hosts without a system Docker socket, opting in with no available
        # socket fails closed. (A real accessible system socket is legitimately
        # the fallback, so skip the case when one exists.)
        if Path("/var/run/docker.sock").is_socket() or Path("/run/docker.sock").is_socket():
            return
        workspace = self.tmp / "containers-missing"
        runtime = self.tmp / "empty-runtime"
        for path in (workspace, runtime):
            path.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {"containers": "host"})
        self.run_fail(workspace,
                      'launcher accepted containers "host" without an accessible socket',
                      extra_env={"XDG_RUNTIME_DIR": str(runtime), "FAKE_UID": "424242"},
                      stderr_contains="no accessible Podman user or Docker socket was found")

    def _mount_target_traversal(self) -> None:
        # Lexical traversal in a mount target must not bypass the baked-path guard.
        cfg = self.workspace / ".opencode-sandbox.json"
        self.set_config(cfg, mounts=[{"source": ".", "target": "/src/../opt/opencode"}])
        self.run_fail(self.workspace,
                      "launcher accepted a mount target that traverses into a baked path",
                      stderr_contains="shadows a baked config/plugin path")

    def _mount_target_system_paths(self) -> None:
        # A user mount may not shadow the container's system paths in any mode:
        # replacing /usr, /etc, or the entrypoint below them would break the
        # sandbox before its command runs. Paths inside the baked roots are
        # handled by the earlier guard, so these targets stay outside them.
        targets = ("/usr", "/usr/local/libexec", "/etc/hosts", "/bin/sh",
                   "/sbin", "/lib", "/lib64",
                   # Reserved beyond SYSTEM_ROOTS: the kernel's virtual
                   # filesystems and the paths the image symlinks into /run or
                   # /usr. The host need not have the same symlinks, so the
                   # target as written is checked too.
                   "/sys", "/proc", "/dev", "/var/lock", "/var/lib/rpm")

        def refuse(workspace: Path, env: dict[str, str], mode: str) -> None:
            cfg = workspace / ".opencode-sandbox.json"
            for target in targets:
                self.set_config(cfg, mounts=[{"source": ".", "target": target}])
                self.run_fail(
                    workspace,
                    f"{mode} accepted a mount target shadowing {target}",
                    extra_env=env,
                    stderr_contains="shadows a container system path",
                )

        off = self.tmp / "system-target-off"
        off.mkdir()
        write_json(off / ".opencode-sandbox.json", {
            "image": "opencode2:test", "containers": False,
            "command": ["/bin/true"]})
        refuse(off, {}, "engine-free mode")

        host = self.tmp / "system-target-host"
        host_runtime = self.tmp / "system-target-host-runtime"
        for path in (host, host_runtime):
            path.mkdir()
        make_unix_socket(host_runtime / "podman" / "podman.sock")
        write_json(host / ".opencode-sandbox.json", {
            "image": "opencode2:test", "containers": "host",
            "command": ["/bin/true"]})
        refuse(host, {"XDG_RUNTIME_DIR": str(host_runtime)}, 'containers "host"')

        nested = self.tmp / "system-target-nested"
        nested.mkdir()
        write_json(nested / ".opencode-sandbox.json", {
            "image": "opencode2:test", "containers": "nested",
            "command": ["/bin/true"]})
        refuse(nested, {"PODMAN_IMAGE_NESTED_PODMAN": "1"}, "nested mode")

        # /var/run is the image's symlink into /run. On a host where that
        # symlink exists the resolved target already contains the baked
        # /run/opencode, so either guard may be the one that refuses it; on a
        # host without the symlink only the reserved literal catches it.
        self.set_config(off / ".opencode-sandbox.json",
                        mounts=[{"source": ".", "target": "/var/run"}])
        self.run_fail(off, "engine-free mode accepted a mount target at /var/run",
                      stderr_contains="shadows a")
        self.set_config(off / ".opencode-sandbox.json",
                        mounts=[{"source": ".", "target": "/var/run/x"}])
        self.run_fail(off, "engine-free mode accepted a mount target below /var/run",
                      stderr_contains="shadows a")

        # A non-system target in the same slot still launches.
        self.set_config(off / ".opencode-sandbox.json",
                        mounts=[{"source": ".", "target": "/mnt/data"}])
        self.run_ok(off)

    def _workspace_contains_protected_state(self) -> None:
        # A workspace that contains protected host state must be rejected, not
        # only a workspace located inside one of the protected directories.
        host_home = self.tmp / "host-home"
        (host_home / ".config" / "opencode").mkdir(parents=True)
        self.derive_config(self.workspace / ".opencode-sandbox.json",
                           host_home / ".opencode-sandbox.json",
                           workspace=str(host_home), mounts=[])
        self.run_fail(host_home, "launcher accepted a workspace containing protected host state",
                      extra_env={"HOME": str(host_home)},
                      stderr_contains="overlaps host OpenCode/.agents/.claude/.mcp state")

    def _mount_source_contains_protected_state(self) -> None:
        # Additional mount sources must reject the same ancestor relationship.
        mount_home = self.tmp / "mount-home"
        (mount_home / ".agents").mkdir(parents=True)
        cfg = self.workspace / ".opencode-sandbox.json"
        self.set_config(cfg, workspace=".",
                        mounts=[{"source": str(mount_home), "target": "/mnt/host-home"}])
        self.run_fail(self.workspace,
                      "launcher accepted a mount source containing protected host state",
                      extra_env={"HOME": str(mount_home)},
                      stderr_contains="overlaps host OpenCode/.agents/.claude/.mcp state")

    def _root_as_mount_source(self) -> None:
        # Root is an ancestor of every protected path; guard against the
        # special root case that a plain prefix check would miss.
        root_home = self.tmp / "root-home"
        (root_home / ".config" / "opencode").mkdir(parents=True)
        cfg = self.workspace / ".opencode-sandbox.json"
        self.set_config(cfg, workspace=".",
                        mounts=[{"source": "/", "target": "/mnt/host-root"}])
        self.run_fail(self.workspace, "launcher accepted the host root as a mount source",
                      extra_env={"HOME": str(root_home)},
                      stderr_contains="overlaps host OpenCode/.agents/.claude/.mcp state")

    def _symlinked_host_state(self) -> None:
        # Canonicalize protected roots too, so a symlinked host-state directory
        # cannot be exposed by mounting its resolved target.
        symlink_home = self.tmp / "symlink-home"
        sensitive = self.tmp / "sensitive-target"
        for path in (symlink_home, sensitive):
            path.mkdir()
        (symlink_home / ".agents").symlink_to(sensitive)
        cfg = self.workspace / ".opencode-sandbox.json"
        self.set_config(cfg, workspace=".",
                        mounts=[{"source": str(sensitive), "target": "/mnt/sensitive"}])
        self.run_fail(self.workspace,
                      "launcher accepted the resolved target of symlinked host state",
                      extra_env={"HOME": str(symlink_home)},
                      stderr_contains="overlaps host OpenCode/.agents/.claude/.mcp state")

    def _missing_workspace(self) -> None:
        # A workspace that does not exist is rejected, not silently
        # canonicalized to a phantom path (realpath on a missing path must
        # fail closed).
        workspace = self.tmp / "missing-workspace"
        workspace.mkdir()
        self.derive_config(self.workspace / ".opencode-sandbox.json",
                           workspace / ".opencode-sandbox.json",
                           workspace=str(self.tmp / "missing-workspace-target"), mounts=[])
        self.run_fail(workspace, "launcher accepted a non-existent workspace",
                      stderr_contains="workspace does not exist")

    def _missing_mount_source(self) -> None:
        # A mount whose source does not exist is rejected before any target
        # validation.
        workspace = self.tmp / "missing-mount-source"
        workspace.mkdir()
        self.derive_config(self.workspace / ".opencode-sandbox.json",
                           workspace / ".opencode-sandbox.json",
                           workspace=".",
                           mounts=[{"source": "no-such-dir", "target": "/mnt/missing"}])
        self.run_fail(workspace, "launcher accepted a non-existent mount source",
                      stderr_contains="mount source does not exist")

    def _git_linked_worktree(self) -> None:
        # A .git file must expose its external metadata root at the same
        # absolute path. Cover Git's usual absolute pointer and an equivalent
        # relative pointer.
        source = self.tmp / "git-source"
        worktree = self.tmp / "git-linked-worktree"
        git("init", "-q", "--initial-branch=main", str(source))
        git("-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "initial", "--allow-empty")
        git("-C", str(source), "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
        write_json(worktree / ".opencode-sandbox.json",
                   {"image": "opencode2:test", "command": ["/bin/true"]})
        git_dir = git("-C", str(worktree), "rev-parse", "--git-dir")
        common_dir = git("-C", str(worktree), "rev-parse", "--git-common-dir")
        self.run_ok(worktree)
        self.has_line(self.argv(), f"{common_dir}:{common_dir}")

        relative = os.path.relpath(git_dir, worktree)
        (worktree / ".git").write_text(f"gitdir: {relative}\n", encoding="utf-8")
        self.run_ok(worktree)
        self.has_line(self.argv(), f"{common_dir}:{common_dir}")

        # A stale .git pointer is an actionable error rather than a silent
        # omission.
        (worktree / ".git").write_text("gitdir: ../missing-worktree-metadata\n", encoding="utf-8")
        self.run_fail(worktree, "launcher accepted a stale linked-worktree gitdir pointer",
                      stderr_contains="linked-worktree gitdir does not exist")

        # Linked worktree support must apply the same protection to the
        # external Git metadata root before mounting it into the container.
        git_home = self.tmp / "git-home"
        protected_repo = git_home / ".agents" / "main"
        protected_worktree = self.tmp / "git-worktree"
        git("init", "-q", "--initial-branch=main", str(protected_repo))
        git("-C", str(protected_repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid",
            "commit", "-qm", "initial", "--allow-empty")
        git("-C", str(protected_repo), "worktree", "add", "-q", "-b", "smoke",
            str(protected_worktree))
        self.derive_config(self.workspace / ".opencode-sandbox.json",
                           protected_worktree / ".opencode-sandbox.json",
                           workspace=".", mounts=[])
        self.run_fail(protected_worktree,
                      "launcher accepted a protected linked-worktree common directory",
                      extra_env={"HOME": str(git_home)},
                      stderr_contains=("git metadata directory",
                                       "overlaps host OpenCode/.agents/.claude/.mcp state"))

    def _custom_xdg_state(self) -> None:
        # Effective custom XDG locations are host OpenCode state too.
        xdg_home = self.tmp / "xdg-home"
        custom_xdg = self.tmp / "custom-xdg-data"
        (custom_xdg / "opencode").mkdir(parents=True)
        xdg_home.mkdir()
        cfg = self.workspace / ".opencode-sandbox.json"
        self.set_config(cfg, workspace=".",
                        mounts=[{"source": str(custom_xdg), "target": "/mnt/xdg-data"}])
        self.run_fail(self.workspace, "launcher accepted an ancestor of custom XDG OpenCode state",
                      extra_env={"HOME": str(xdg_home), "XDG_DATA_HOME": str(custom_xdg)},
                      stderr_contains="overlaps host OpenCode/.agents/.claude/.mcp state")

    def _relative_mount_source(self) -> None:
        # Relative additional-mount sources resolve from the configured
        # workspace, not from the directory containing the launcher config.
        relative_home = self.tmp / "relative-home"
        relative_workspace = self.tmp / "relative-workspace"
        (relative_workspace / "data").mkdir(parents=True)
        relative_home.mkdir()
        cfg = self.workspace / ".opencode-sandbox.json"
        self.set_config(cfg, workspace=str(relative_workspace),
                        mounts=[{"source": "data", "target": "/mnt/data"}])
        self.run_ok(self.workspace, extra_env={"HOME": str(relative_home)})
        self.has_line(self.argv(), f"{relative_workspace}/data:/mnt/data")

    # --- workspace agent definitions -------------------------------------------

    def _new_agents_workspace(self, name: str, **changes: object) -> Path:
        """An isolated workspace with a minimal sandbox config, for agents cases.

        Self-contained on purpose: the shared workspace config accumulates
        mutations across sections, and its `workspace` redirect would move the
        agent source out from under the assertions.
        """
        workspace = self.tmp / name
        workspace.mkdir()
        base: dict[str, object] = {
            "schema_version": 1,
            "image": "opencode2:test",
            "workspace": ".",
            "containers": False,
            "mounts": [],
            "command": ["/bin/true"],
        }
        write_json(workspace / ".opencode-sandbox.json", base | changes)
        return workspace

    def _agents_default_pickup(self) -> None:
        # `.agents/*.md` in the workspace is mounted into the second agent
        # directory the runtime scans, so it registers as additional agents.
        workspace = self._new_agents_workspace("agents-default")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text(
            "---\ndescription: Lane.\nmode: subagent\n---\nBody.\n", encoding="utf-8"
        )
        (agents / "review-schema.md").write_text(
            "---\ndescription: Lane.\nmode: subagent\n---\nBody.\n", encoding="utf-8"
        )
        proc = self.run_ok(workspace)
        self.has_line(self.argv(), f"{agents}:{AGENTS_TARGET}:ro")
        self.has_substring([proc.stdout], "workspace agents: 2 definition(s)")

    def _agents_absent_is_silent(self) -> None:
        # No `.agents` directory: no mount, no notice. This is every project that
        # has not adopted workspace agent definitions.
        workspace = self._new_agents_workspace("agents-absent")
        proc = self.run_ok(workspace)
        self.no_substring(self.argv(), AGENTS_TARGET)
        if "workspace agents:" in proc.stdout:
            fail(f"unexpected agent notice: {proc.stdout}")

    def _agents_disabled(self) -> None:
        workspace = self._new_agents_workspace("agents-off", agents=False)
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text("Body.\n", encoding="utf-8")
        self.run_ok(workspace)
        self.no_substring(self.argv(), AGENTS_TARGET)

    def _agents_explicit_true_uses_the_default(self) -> None:
        # `"agents": true` is an explicit opt-in to the default directory.
        workspace = self._new_agents_workspace("agents-true", agents=True)
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text(AGENT_BODY, encoding="utf-8")
        self.run_ok(workspace)
        self.has_line(self.argv(), f"{agents}:{AGENTS_TARGET}:ro")

    def _agents_custom_directory(self) -> None:
        # `agents` replaces the default directory; it never reaches the baked set.
        workspace = self._new_agents_workspace("agents-dir", agents=".claude/agents")
        agents = workspace / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "review-adr.md").write_text(AGENT_BODY, encoding="utf-8")
        self.run_ok(workspace)
        self.has_line(self.argv(), f"{agents}:{AGENTS_TARGET}:ro")

    def _agents_missing_override_directory(self) -> None:
        # An explicit setting that is absent is a mistake, not a silent opt-out.
        workspace = self._new_agents_workspace("agents-missing", agents="team-agents")
        self.run_fail(
            workspace,
            "a missing agents override must fail",
            stderr_contains=("agents directory does not exist",),
        )

    def _agents_bad_setting(self) -> None:
        workspace = self._new_agents_workspace("agents-bad", agents=["review-adr"])
        self.run_fail(
            workspace,
            "a non-string agents setting must fail",
            stderr_contains=("agents must be a directory path or false",),
        )

    def _agents_rejects_container_id(self) -> None:
        # A same-id definition replaces the container agent's prompt and unions
        # its permission rules, so it is refused rather than treated as an override.
        for agent_id in ("aid-oracle", "build"):
            workspace = self._new_agents_workspace(f"agents-id-{agent_id}")
            agents = workspace / ".agents"
            agents.mkdir()
            (agents / f"{agent_id}.md").write_text("Body.\n", encoding="utf-8")
            self.run_fail(
                workspace,
                f"a redefined container agent {agent_id} must fail",
                stderr_contains=("redefines a container agent",),
            )

    def _agents_rejects_unusable_entries(self) -> None:
        # A definition must be a regular `<agent-id>.md` file directly under the
        # agents directory: a nested path is not a definition, an invalid name
        # is rejected, and a symlink can leave the workspace.
        cases = (
            ("nested directory", "dir", "team", "must be regular files"),
            ("non-Markdown file", "file", "notes.txt", "must be a Markdown file"),
            ("invalid name", "file", "review adr.md", "invalid agent name"),
            ("symlink", "link", "elsewhere.md", "must be regular files"),
        )
        for label, kind, entry, reject in cases:
            workspace = self._new_agents_workspace(f"agents-entry-{kind}-{entry}")
            agents = workspace / ".agents"
            agents.mkdir()
            (agents / "review-adr.md").write_text(AGENT_BODY, encoding="utf-8")
            if kind == "dir":
                (agents / entry).mkdir()
            elif kind == "file":
                (agents / entry).write_text("Body.\n", encoding="utf-8")
            else:
                outside = self.tmp / "elsewhere.md"
                outside.write_text("Body.\n", encoding="utf-8")
                (agents / entry).symlink_to(outside)
            self.run_fail(
                workspace,
                f"{label} entry must fail",
                stderr_contains=(reject,),
            )

    def _agents_skips_hidden_files(self) -> None:
        # Bookkeeping files are not definitions; a hidden `.md` is skipped loudly,
        # because it looks like a definition and would otherwise vanish silently.
        workspace = self._new_agents_workspace("agents-hidden")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text(AGENT_BODY, encoding="utf-8")
        (agents / ".gitignore").write_text("*.bak\n", encoding="utf-8")
        (agents / ".review-typo.md").write_text("Body.\n", encoding="utf-8")
        proc = self.run_ok(workspace)
        self.has_line(self.argv(), f"{agents}:{AGENTS_TARGET}:ro")
        self.has_substring([proc.stdout], "workspace agents: 1 definition(s)")
        self.has_substring([proc.stderr], "skipping hidden agent file")

    def _agents_gate_contributions_are_reported(self) -> None:
        # A reviewer names the gates it feeds, inline or as a block list, and the
        # launcher reports the map so the contract is legible at launch.
        workspace = self._new_agents_workspace("agents-gates")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text(
            "---\ndescription: ADR conformance.\nmode: subagent\n"
            "review-gates: [review_quality, review_blind_spots, review_quality]\n---\nBody.\n",
            encoding="utf-8",
        )
        (agents / "review-schema.md").write_text(
            "---\ndescription: Schema drift.\nmode: subagent\nreview-gates:\n"
            "  - review_spec_compliance\n---\nBody.\n",
            encoding="utf-8",
        )
        proc = self.run_ok(workspace)
        self.has_substring(
            [proc.stdout],
            "review contributions: review-adr -> review_quality, review_blind_spots; "
            "review-schema -> review_spec_compliance",
        )
        if "names no review-gates" in proc.stderr:
            fail(f"declared gates must not warn: {proc.stderr}")

    def _agents_rejects_unknown_gate(self) -> None:
        # The gate vocabulary is closed by the review tooling; a name outside it is
        # a typo that would silently contribute to nothing.
        workspace = self._new_agents_workspace("agents-gate-unknown")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text(
            "---\nmode: subagent\nreview-gates: [review_made_up]\n---\nBody.\n",
            encoding="utf-8",
        )
        self.run_fail(
            workspace,
            "an unknown review gate must fail",
            stderr_contains=("unknown review gate(s) review_made_up",),
        )

    def _agents_rejects_scalar_gate(self) -> None:
        workspace = self._new_agents_workspace("agents-gate-scalar")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text(
            "---\nmode: subagent\nreview-gates: review_quality\n---\nBody.\n",
            encoding="utf-8",
        )
        self.run_fail(
            workspace,
            "a scalar review-gates value must fail",
            stderr_contains=("must be a list of review gates",),
        )

    def _agents_empty_gates_is_explicitly_advisory(self) -> None:
        # `[]` is a deliberate "contributes to no gate", so it warns about neither.
        workspace = self._new_agents_workspace("agents-gate-empty")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-notes.md").write_text(
            "---\nmode: subagent\nreview-gates: []\n---\nBody.\n", encoding="utf-8"
        )
        proc = self.run_ok(workspace)
        for noise in ("names no review-gates", "review contributions:"):
            if noise in proc.stderr + proc.stdout:
                fail(f"an explicit empty gate list must be quiet about {noise!r}")

    def _agents_undeclared_gates_warns(self) -> None:
        # A reviewer that could feed a gate but names none would have its findings
        # silently dropped at synthesis.
        workspace = self._new_agents_workspace("agents-gate-missing")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "review-adr.md").write_text(AGENT_BODY, encoding="utf-8")
        proc = self.run_ok(workspace)
        if "names no review-gates" not in proc.stderr:
            fail(f"an undeclared gate list must warn: {proc.stderr}")

    def _agents_outside_convention_is_silent(self) -> None:
        # aid's agents do not restrict subagent dispatch, so an id outside the
        # `review-*` convention is no longer a worker-dispatch problem: it still
        # mounts and the launcher says nothing about dispatch.
        workspace = self._new_agents_workspace("agents-convention")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "migration-planner.md").write_text(AGENT_BODY, encoding="utf-8")
        proc = self.run_ok(workspace)
        self.has_line(self.argv(), f"{agents}:{AGENTS_TARGET}:ro")
        if "outside the review-* convention" in proc.stderr:
            fail(f"an id outside review-* must not warn about dispatch: {proc.stderr}")

    def _agents_rejects_unfenced_frontmatter(self) -> None:
        # Nothing else checks these files: the runtime registers a document with no
        # parseable frontmatter as an unnamed primary agent whose prompt is its own
        # frontmatter text.
        for label, body, reject in (
            ("no fence", "Just a prompt.\n", "must open with"),
            ("unclosed fence", "---\nmode: subagent\nNo closing fence\n", "never closed"),
        ):
            workspace = self._new_agents_workspace(f"agents-fence-{label.split()[0]}")
            agents = workspace / ".agents"
            agents.mkdir()
            (agents / "review-adr.md").write_text(body, encoding="utf-8")
            self.run_fail(
                workspace,
                f"{label} must fail",
                stderr_contains=(reject,),
            )

    def _agents_source_is_protected_state(self) -> None:
        # The override must not point at host agent state the container may not reach.
        host_agents = self.test_home / ".agents"
        host_agents.mkdir()
        (host_agents / "review-adr.md").write_text("Body.\n", encoding="utf-8")
        workspace = self._new_agents_workspace("agents-host-state", agents=str(host_agents))
        self.run_fail(
            workspace,
            "host .agents state must be refused",
            stderr_contains=("overlaps host OpenCode/.agents/.claude/.mcp state",),
        )
        shutil.rmtree(host_agents, ignore_errors=True)

    def _agents_reserved_ids_track_the_image(self) -> None:
        # The refusal above only protects the agents the image actually ships.
        # They come from the pinned ai-devtools plugin (dist/opencode/agents at
        # AI_DEVTOOLS_REF), so this restates that pin; a launcher edit cannot
        # silently stop reserving one of them.
        expected = (
            "aid-orchestrator",
            "aid-explorer",
            "aid-mechanic",
            "aid-oracle",
            "aid-designer",
            "aid-creator",
            "aid-craftsman",
            "aid-sage",
        )
        loader = importlib.machinery.SourceFileLoader("launcher", str(LAUNCHER))
        spec = importlib.util.spec_from_loader("launcher", loader)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if sorted(module.BAKED_AGENT_IDS) != sorted(expected):
            fail(
                "launcher BAKED_AGENT_IDS drifted from the ai-devtools plugin's agents:\n"
                f"  launcher: {sorted(module.BAKED_AGENT_IDS)}\n  plugin: {sorted(expected)}"
            )
        for agent_id in module.RESERVED_AGENT_IDS:
            if not module.AGENT_ID_RE.fullmatch(agent_id):
                fail(f"reserved id is not a valid agent name: {agent_id}")

    def _setup_default_workspace(self) -> tuple[Path, Path]:
        home = self.tmp / "default-home"
        workspace = self.tmp / "default-workspace"
        home.mkdir()
        workspace.mkdir()
        if not (self.bin / "opencode-container").exists():
            (self.bin / "opencode-container").symlink_to(LAUNCHER)
        return home, workspace

    def _no_config_defaults(self) -> tuple[Path, Path]:
        # With no sandbox config, warn and use only the launcher's baked
        # defaults. The absent control file must not be mounted.
        home, workspace = self._setup_default_workspace()
        proc = self.run_ok(workspace, via_path=True, extra_env={"HOME": str(home)})
        if "warning: no .opencode-sandbox.json found" not in proc.stderr:
            fail("launcher did not warn about the missing sandbox config")
        argv = self.argv()
        self.has_line(argv, f"{workspace}:/src")
        self.has_line(argv, "opencode2:latest")
        self.has_line(argv, "opencode2")
        self.has_line(argv, "--standalone")
        key = sha256_16(str(workspace))
        self.has_line(argv, f"{workspace}:/workspace/{key}")
        self.has_line(argv, f"/workspace/{key}")
        # No config means the launcher's defaults, which now include the nested
        # engine: a no-config project is not an engine-free exception.
        self.has_line(argv, f"opencode2-containers-{key}:/var/lib/opencode-containers")
        self.has_line(argv, f"opencode2-data-{key}:/var/lib/opencode-data")
        self.no_substring(argv, "opencode-data:U")
        self.no_substring(argv, "/run/opencode-container-engine.sock")
        self.no_substring(argv, "/run/opencode/sandbox.json")
        self.no_substring(argv, "/opt/opencode/config/opencode/aid.json")
        self.no_substring(argv, "/run/opencode/gitconfig")
        return home, workspace

    def _gitconfig_mounts(self, default_home: Path, default_workspace: Path) -> None:
        # HOME and the file itself may both be symlinks. The launcher must
        # still mount only the canonical exact file at the stable Git override
        # target, not host HOME or the source file's parent directory.
        real = self.tmp / "gitconfig-real"
        home_real = self.tmp / "gitconfig-home-real"
        real.mkdir()
        home_real.mkdir()
        (real / "config").write_text("[user]\n  name = Test User\n", encoding="utf-8")
        (home_real / ".gitconfig").symlink_to(real / "config")
        home_link = self.tmp / "gitconfig-home-link"
        home_link.symlink_to(home_real)
        unused_xdg = str(self.tmp / "unused-xdg-config")
        self.run_ok(default_workspace, via_path=True,
                    extra_env={"HOME": str(home_link), "XDG_CONFIG_HOME": unused_xdg})
        argv = self.argv()
        self.has_line(argv, f"{real}/config:/run/opencode/gitconfig:ro")
        self.has_line(argv, "GIT_CONFIG_GLOBAL=/run/opencode/gitconfig")
        self.no_substring(argv, f"{real}/:")
        self.no_substring(argv, f"{home_real}/:")

        # An additional writable mount cannot expose the canonical config
        # through a second path.
        overlap_workspace = self.tmp / "gitconfig-overlap-workspace"
        overlap_workspace.mkdir()
        write_json(overlap_workspace / ".opencode-sandbox.json",
                   {"mounts": [{"source": str(real), "target": "/mnt/gitconfig"}]})
        self.run_fail(overlap_workspace,
                      "launcher exposed the user-global Git config through an additional mount",
                      via_path=True,
                      extra_env={"HOME": str(home_link), "XDG_CONFIG_HOME": unused_xdg},
                      stderr_contains="overlaps the user-global Git config")

        # Workspace environment settings cannot redirect Git away from the
        # dedicated launcher-owned global config mount.
        write_json(overlap_workspace / ".opencode-sandbox.json",
                   {"env": {"set": {"GIT_CONFIG_GLOBAL": "/mnt/other"}}})
        self.run_fail(overlap_workspace,
                      "launcher allowed workspace override of GIT_CONFIG_GLOBAL",
                      via_path=True,
                      extra_env={"HOME": str(home_link), "XDG_CONFIG_HOME": unused_xdg},
                      stderr_contains="reserved environment variable: GIT_CONFIG_GLOBAL")

    def _global_aid_config(self, default_home: Path,
                           default_workspace: Path) -> tuple[Path, Path]:
        # A standalone user-global ai-devtools routing config is discovered on
        # every launch, canonicalized, and mounted as one exact read-only file
        # without requiring a workspace sandbox config.
        real = self.tmp / "global-aid-real"
        (real / "opencode2").mkdir(parents=True)
        link = self.tmp / "global-aid-link"
        link.symlink_to(real)
        write_json(real / "opencode2" / "aid.json",
                   {"agents": {"aid-orchestrator": "orchestration"}})
        self.run_ok(default_workspace, via_path=True,
                    extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)})
        argv = self.argv()
        self.has_line(argv,
                      f"{real}/opencode2/aid.json:/opt/opencode/config/opencode/aid.json:ro")
        self.no_substring(argv, "OPENCODE_MODEL_ROUTER")
        self.no_line(argv, f"{real}/opencode2:/opt/opencode/config/opencode")
        return real, link

    def _global_aid_overlap(self, real: Path, link: Path,
                            default_home: Path, default_workspace: Path) -> None:
        # The exact read-only control mount must not be bypassable through a
        # workspace or additional mount that exposes the same host file.
        workspace = self.tmp / "global-aid-overlap-workspace"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json",
                   {"mounts": [{"source": str(real / "opencode2"),
                                "target": "/mnt/global-aid"}]})
        self.run_fail(workspace, "launcher exposed the global aid config through an additional mount",
                      via_path=True,
                      extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                      stderr_contains="overlaps the user-global aid config")

        # A workspace that contains the config is refused too.
        self.run_fail(real / "opencode2",
                      "launcher accepted a workspace containing the global aid config",
                      via_path=True,
                      extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                      stderr_contains="overlaps the user-global aid config")

    def _global_aid_and_sandbox_mounted(self, real: Path, link: Path,
                                        default_home: Path, default_workspace: Path) -> None:
        # The global aid file and the workspace sandbox control file are
        # independent and both mount, with no router env var pairing them.
        write_json(default_workspace / ".opencode-sandbox.json", {})
        self.run_ok(default_workspace, via_path=True,
                    extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)})
        argv = self.argv()
        self.has_line(argv,
                      f"{real}/opencode2/aid.json:/opt/opencode/config/opencode/aid.json:ro")
        self.has_line(argv, f"{default_workspace}/.opencode-sandbox.json:/run/opencode/sandbox.json:ro")
        self.no_substring(argv, "OPENCODE_MODEL_ROUTER")

    def _global_aid_git_metadata_overlap(self) -> None:
        # Linked-worktree metadata is mounted writable at its host path, so a
        # metadata root that contains the global aid config must be refused.
        workspace = self.tmp / "aid-git-workspace"
        config_home = self.tmp / "aid-git-config"
        git_home = config_home / "opencode2"
        workspace.mkdir()
        config_home.mkdir()
        git("init", "-q", "--initial-branch=main", "--separate-git-dir",
            str(git_home), str(workspace))
        write_json(git_home / "aid.json", {"agents": {}})
        git("-C", str(workspace), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid",
            "commit", "-qm", "initial", "--allow-empty")
        write_json(workspace / ".opencode-sandbox.json",
                   {"image": "opencode2:test", "command": ["/bin/true"]})
        self.run_fail(workspace, "launcher accepted git metadata containing the global aid config",
                      extra_env={"HOME": str(self.tmp / "aid-git-home"),
                                 "XDG_CONFIG_HOME": str(config_home)},
                      stderr_contains=("git metadata directory",
                                       "overlaps the user-global aid config"))

    def _malformed_global_aid(self, default_home: Path, default_workspace: Path,
                              link: Path, aid: Path) -> None:
        # Malformed, multiple-document, non-object, and non-file global inputs
        # fail before Podman instead of silently running without global routing.
        cases = (
            ("{ not json\n", "launcher accepted malformed global aid JSON"),
            ("{}\n{}\n", "launcher accepted multiple global aid JSON documents"),
            ("[]\n", "launcher accepted a non-object global aid config"),
        )
        for text, reject in cases:
            aid.write_text(text, encoding="utf-8")
            self.run_fail(default_workspace, reject, via_path=True,
                          extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                          stderr_contains="invalid global aid config")
        aid.unlink()
        aid.mkdir()
        self.run_fail(default_workspace, "launcher accepted a non-file global aid path",
                      via_path=True,
                      extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                      stderr_contains="global aid config is not a regular file")
        shutil.rmtree(aid)

    def _model_router_block_refused(self, default_home: Path,
                                    default_workspace: Path) -> None:
        # The embedded model router is gone, so any leftover block is refused:
        # silently ignoring it would leave a project routed by nothing.
        workspace = self.tmp / "router-block-workspace"
        workspace.mkdir()
        env = {"HOME": str(default_home)}
        for document in (
            {"model_router": {"schema_version": 2, "agents": {"reasoner": "reasoning"}}},
            {"model_router": "nope"},
        ):
            write_json(workspace / ".opencode-sandbox.json", document)
            self.run_fail(workspace, "launcher accepted a retired model_router block",
                          via_path=True, extra_env=env,
                          stderr_contains=(
                              '"model_router" block',
                              ".opencode/aid.json",
                              f"{default_home}/.config/opencode2/aid.json",
                          ))

    def _leftover_global_router_warns(self, default_home: Path,
                                      default_workspace: Path) -> None:
        # A leftover global router file no longer does anything; say so once,
        # with the replacement path, but launch anyway.
        config = default_home / ".config" / "opencode2"
        config.mkdir(parents=True, exist_ok=True)
        legacy = config / "model-router.json"
        legacy.write_text("{}\n", encoding="utf-8")
        proc = self.run_ok(default_workspace, via_path=True,
                           extra_env={"HOME": str(default_home)})
        if proc.stderr.count("is no longer read") != 1:
            fail(f"leftover model-router.json must warn exactly once: {proc.stderr}")
        for needle in ("model-router.json", ".opencode/aid.json",
                       f"{config}/aid.json"):
            if needle not in proc.stderr:
                fail(f"leftover model-router.json warning omits {needle!r}: {proc.stderr}")
        self.no_substring(self.argv(), "model-router.json")
        legacy.unlink()

    def _partial_config(self, default_home: Path, default_workspace: Path) -> None:
        # A present partial config inherits the same defaults and is mounted so
        # the sandbox sees its own control file.
        write_json(default_workspace / ".opencode-sandbox.json", {})
        proc = self.run_ok(default_workspace, via_path=True,
                           extra_env={"HOME": str(default_home)})
        if "no .opencode-sandbox.json found" in proc.stderr:
            fail("launcher warned despite a present sandbox config")
        argv = self.argv()
        self.has_line(argv, "opencode2:latest")
        self.has_line(argv, f"{default_workspace}/.opencode-sandbox.json:/run/opencode/sandbox.json:ro")
        self.no_substring(argv, "OPENCODE_MODEL_ROUTER")

    def _podman_secrets(self, default_home: Path, default_workspace: Path) -> None:
        # Existing user-global Podman secrets are not injected without project
        # opt-in; the normal name-only host environment fallback remains active.
        (default_workspace / ".opencode-sandbox.json").unlink()
        self.run_ok(default_workspace, via_path=True, extra_env={
            "HOME": str(default_home),
            "AVAILABLE_SECRETS": "openai-api-key",
            "OPENAI_API_KEY": "environment-fallback",
        })
        argv = self.argv()
        self.no_substring(argv, "type=env,target=OPENAI_API_KEY")
        # The default mode is nested, so the forwarded host value travels in
        # the agent environment file rather than in `-e`.
        self.no_line(argv, "OPENAI_API_KEY")
        self.has_substring(argv, ":/run/opencode/agent-env:ro")

        # Explicitly configured known Podman secrets take precedence over a
        # same-named host environment variable and map to the provider variable
        # OpenCode2 expects.
        write_json(default_workspace / ".opencode-sandbox.json",
                   {"provider_secrets": ["openai-api-key", "deepseek-api-key"]})
        self.run_ok(default_workspace, via_path=True, extra_env={
            "HOME": str(default_home),
            "AVAILABLE_SECRETS": "openai-api-key,deepseek-api-key",
            "OPENAI_API_KEY": "must-not-be-forwarded",
        })
        argv = self.argv()
        self.has_line(argv, "openai-api-key,type=env,target=OPENAI_API_KEY")
        self.has_line(argv, "deepseek-api-key,type=env,target=DEEPSEEK_API_KEY")
        self.no_line(argv, "OPENAI_API_KEY")
        self.no_substring(argv, "must-not-be-forwarded")

    def _secrets_subcommand(self, default_home: Path, default_workspace: Path) -> None:
        # The secrets subcommand manages Podman provider secrets and the
        # workspace provider_secrets opt-in from the host, without launching a
        # container. Only known provider names are accepted.
        self.argv_log.unlink(missing_ok=True)
        self.secret_log.unlink(missing_ok=True)
        self.secret_value_log.unlink(missing_ok=True)
        workspace = self.tmp / "secrets-workspace"
        workspace.mkdir()
        key_file = self.tmp / "api-key.txt"
        key_file.write_text("sk-test-key\n", encoding="utf-8")
        env = {"HOME": str(default_home)}

        # add from a file: creates the secret over stdin (one trailing newline
        # dropped) and creates the workspace config with the opt-in.
        self.run_ok(workspace, args=("secrets", "add", "openai-api-key", "--in", str(key_file)),
                    extra_env=env, stdin=subprocess.DEVNULL)
        self.has_line(self.secret_lines(), "create openai-api-key")
        if self.secret_value() != b"sk-test-key":
            fail("file secret value was not passed to podman verbatim (newline dropped)")
        if self.argv_log.exists():
            fail("secrets add launched a container")
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg != {"schema_version": 1, "provider_secrets": ["openai-api-key"]}:
            fail(f"secrets add did not hook up the workspace config: {cfg}")

        # An existing secret is rejected before prompting, without touching
        # the config.
        before = (workspace / ".opencode-sandbox.json").read_text(encoding="utf-8")
        self.run_fail(workspace, "launcher accepted secrets add for an existing secret",
                      args=("secrets", "add", "openai-api-key", "--in", str(key_file)),
                      extra_env={**env, "AVAILABLE_SECRETS": "openai-api-key"},
                      stdin=subprocess.DEVNULL,
                      stderr_contains=("already exists",))
        if (workspace / ".opencode-sandbox.json").read_text(encoding="utf-8") != before:
            fail("secrets add rewrote the config for an existing secret")

        # Without --in and without a terminal the prompt is refused, not
        # silently read from stdin.
        self.run_fail(workspace, "launcher read a secret from non-tty stdin without --in",
                      args=("secrets", "add", "deepseek-api-key"),
                      extra_env=env, stdin=subprocess.DEVNULL,
                      stderr_contains=("--in",))
        if "create deepseek-api-key" in self.secret_lines():
            fail("secrets add created a secret with no usable value source")

        # --in - reads the value from stdin; a newline-less file is verbatim.
        self.run_ok(workspace, args=("secrets", "add", "deepseek-api-key", "--in", "-"),
                    extra_env=env, input_text="sk-piped-key\n")
        self.has_line(self.secret_lines(), "create deepseek-api-key")
        if self.secret_value() != b"sk-piped-key":
            fail("stdin secret value was not passed through")
        raw_file = self.tmp / "raw-key.txt"
        raw_file.write_bytes(b"sk-no-newline")
        self.run_ok(workspace, args=("secrets", "add", "groq-api-key", "--in", str(raw_file)),
                    extra_env=env, stdin=subprocess.DEVNULL)
        if self.secret_value() != b"sk-no-newline":
            fail("newline-less file value was mangled")
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg["provider_secrets"] != ["openai-api-key", "deepseek-api-key", "groq-api-key"]:
            fail(f"provider_secrets order/content wrong after adds: {cfg}")

        # Empty values and unknown names are rejected before any podman call.
        empty_file = self.tmp / "empty-key.txt"
        empty_file.write_bytes(b"")
        self.run_fail(workspace, "launcher accepted an empty secret value",
                      args=("secrets", "add", "anthropic-api-key", "--in", str(empty_file)),
                      extra_env=env, stdin=subprocess.DEVNULL,
                      stderr_contains=("must not be empty",))
        self.run_fail(workspace, "launcher accepted an unknown secret name",
                      args=("secrets", "add", "not-a-provider-key", "--in", str(key_file)),
                      extra_env=env, stdin=subprocess.DEVNULL,
                      stderr_contains=("unknown provider secret name",))
        for name in ("anthropic-api-key", "not-a-provider-key"):
            if f"create {name}" in self.secret_lines():
                fail(f"secrets add created a secret for a rejected input: {name}")

        # The openapi-api-key alias normalizes to openai-api-key without
        # duplicating the config entry.
        self.run_ok(workspace, args=("secrets", "add", "openapi-api-key", "--in", str(key_file)),
                    extra_env=env, stdin=subprocess.DEVNULL)
        if self.count(self.secret_lines(), "create openai-api-key") != 2:
            fail("alias add did not create the canonical podman secret")
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg["provider_secrets"] != ["openai-api-key", "deepseek-api-key", "groq-api-key"]:
            fail(f"alias add duplicated or misnamed the config entry: {cfg}")

        # remove: podman rm plus config cleanup; the last entry drops the key.
        self.run_ok(workspace, args=("secrets", "remove", "openai-api-key"),
                    extra_env={**env, "AVAILABLE_SECRETS": "openai-api-key"},
                    stdin=subprocess.DEVNULL)
        self.has_line(self.secret_lines(), "rm openai-api-key")
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg != {"schema_version": 1,
                   "provider_secrets": ["deepseek-api-key", "groq-api-key"]}:
            fail(f"unexpected config after secrets remove: {cfg}")
        self.run_ok(workspace, args=("secrets", "remove", "deepseek-api-key"),
                    extra_env={**env, "AVAILABLE_SECRETS": "deepseek-api-key"},
                    stdin=subprocess.DEVNULL)
        self.run_ok(workspace, args=("secrets", "remove", "groq-api-key"),
                    extra_env={**env, "AVAILABLE_SECRETS": "groq-api-key"},
                    stdin=subprocess.DEVNULL)
        self.has_line(self.secret_lines(), "rm groq-api-key")
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg != {"schema_version": 1}:
            fail(f"removing the last secret should drop the provider_secrets key: {cfg}")

        # Removing a secret present in neither store is a clean no-op.
        self.run_ok(workspace, args=("secrets", "remove", "cohere-api-key"),
                    extra_env=env, stdin=subprocess.DEVNULL)
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg != {"schema_version": 1}:
            fail(f"no-op remove modified the workspace config: {cfg}")
        if "rm cohere-api-key" in self.secret_lines():
            fail("secrets remove called podman rm for an absent secret")

        # An existing config keeps every other key; the opt-in is the only edit.
        rich = {
            "schema_version": 1,
            "image": "opencode2:test",
            "workspace": ".",
            "provider_secrets": ["perplexity-api-key"],
            "env": {"set": {"TZ": "UTC"}},
            "network": "none",
        }
        write_json(workspace / ".opencode-sandbox.json", rich)
        self.run_ok(workspace, args=("secrets", "add", "together-api-key", "--in", str(key_file)),
                    extra_env=env, stdin=subprocess.DEVNULL)
        cfg = load_json(workspace / ".opencode-sandbox.json")
        expected = dict(rich)
        expected["provider_secrets"] = ["perplexity-api-key", "together-api-key"]
        if cfg != expected:
            fail(f"secrets add clobbered existing config keys: {cfg}")
        self.run_ok(workspace, args=("secrets", "remove", "together-api-key"),
                    extra_env={**env, "AVAILABLE_SECRETS": "together-api-key"},
                    stdin=subprocess.DEVNULL)
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg != rich:
            fail(f"secrets remove did not restore the config: {cfg}")

        # A stale config entry (secret already gone) still gets cleaned up.
        before_log = self.secret_lines()
        self.run_ok(workspace, args=("secrets", "remove", "perplexity-api-key"),
                    extra_env=env, stdin=subprocess.DEVNULL)
        cfg = load_json(workspace / ".opencode-sandbox.json")
        if cfg != {k: v for k, v in rich.items() if k != "provider_secrets"}:
            fail(f"stale entry removal did not drop the emptied key: {cfg}")
        if self.secret_lines() != before_log:
            fail("stale entry removal called podman rm")

        # "run --" keeps "secrets" a literal container command.
        self.run_ok(workspace, args=("run", "--", "secrets", "add", "openai-api-key"),
                    extra_env=env, stdin=subprocess.DEVNULL)
        self.has_line(self.argv(), "secrets")
        self.has_line(self.argv(), "add")
        if "create openai-api-key" not in self.secret_lines() or \
                self.count(self.secret_lines(), "create openai-api-key") != 2:
            fail("run -- secrets must not invoke the secrets subcommand")

        # The removed top-level launcher options are rejected by the parser.
        self.run_fail(workspace, "launcher accepted a removed top-level launcher option",
                      args=("--rebuild", "secrets", "add", "openai-api-key", "--in", str(key_file)),
                      extra_env=env, stdin=subprocess.DEVNULL,
                      stderr_contains=("unrecognized arguments: --rebuild",))

        # A bare secrets argument is a usage error.
        self.run_fail(workspace, "launcher accepted a bare secrets subcommand",
                      args=("secrets",), extra_env=env, stdin=subprocess.DEVNULL,
                      stderr_contains=("required",))

    def _data_volume_override(self, default_home: Path, default_workspace: Path) -> None:
        # The central data volume can be overridden without depending on the
        # launcher's own installation directory.
        write_json(default_workspace / ".opencode-sandbox.json",
                   {"containers": False,
                    "persistence": {"data_volume": "project-opencode-data"}})
        self.run_ok(default_workspace, via_path=True, extra_env={"HOME": str(default_home)})
        argv = self.argv()
        self.has_line(argv, "project-opencode-data:/var/lib/opencode-data:U")
        self.has_line(argv, "XDG_STATE_HOME=/var/lib/opencode-data/state")

    def _provider_catalog_runtime(self) -> None:
        # Validate and mount the user-wide catalog from its canonical host path
        # on every launch, including when the selected image already exists.
        workspace = self.tmp / "provider-workspace"
        shared_root = self.tmp / "shared-config-real"
        shared = shared_root / "opencode2"
        shared_link = self.tmp / "shared-config-link"
        shared.mkdir(parents=True)
        shared_link.symlink_to(shared_root)
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:provider-runtime-test",
            "build": {"containerfile": str(ROOT / "Containerfile"),
                      "context": str(ROOT)},
            "command": ["/bin/true"],
        })
        catalog = shared / "local-providers.json"
        shutil.copyfile(ROOT / "examples" / "local-providers.json.example", catalog)
        env = {"HOME": str(self.tmp / "provider-home"),
               "XDG_CONFIG_HOME": str(shared_link),
               "PODMAN_IMAGE_EXISTS": "1",
               "PODMAN_IMAGE_LOCAL_PROVIDERS_SHA256": hashlib.sha256(
                   catalog.read_bytes()
               ).hexdigest()}

        self.build_argv_log.unlink(missing_ok=True)
        self.run_ok(workspace, via_path=True, extra_env=env)
        self.has_line(
            self.argv(),
            f"{catalog}:/opt/opencode/config/opencode/opencode.json:ro",
        )
        if self.build_argv_log.exists():
            fail("launcher rebuilt an existing image to install the local-provider catalog")

        # A changed present catalog must rebuild an existing configured image
        # when its recorded digest does not match, without mounting it into the
        # build process.
        catalog.write_bytes(catalog.read_bytes() + b"\n")
        changed_digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
        self.run_ok(workspace, via_path=True, extra_env=env)
        build_argv = self.build_argv()
        self.has_line(build_argv, f"LOCAL_PROVIDERS_SHA256={changed_digest}")
        self.has_line(build_argv, f"io.opencode.local-providers-sha256={changed_digest}")
        self.no_substring(build_argv, "/run/opencode2-build-config/")
        self.no_substring(build_argv, f"{catalog}:")

        # Removing the catalog removes the mount on the next launch without a
        # rebuild because the catalog is runtime-only.
        catalog.unlink()
        self.build_argv_log.unlink(missing_ok=True)
        self.run_ok(workspace, via_path=True, extra_env=env)
        self.no_substring(self.argv(), "/opt/opencode/config/opencode/opencode.json")
        if self.build_argv_log.exists():
            fail("launcher rebuilt after removing a runtime-only provider catalog")

        # A configured build receives the catalog digest as a cache key but
        # never receives the catalog itself; only the subsequent run mounts it.
        shutil.copyfile(ROOT / "examples" / "local-providers.json.example", catalog)
        digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
        self.run_ok(workspace, via_path=True,
                    extra_env={**env, "PODMAN_IMAGE_EXISTS": "0"})
        build_argv = self.build_argv()
        self.has_line(build_argv, f"LOCAL_PROVIDERS_SHA256={digest}")
        self.has_line(build_argv, f"io.opencode.local-providers-sha256={digest}")
        self.no_substring(build_argv, "/run/opencode2-build-config/")
        self.no_substring(build_argv, f"{catalog}:")
        self.has_line(
            self.argv(),
            f"{catalog}:/opt/opencode/config/opencode/opencode.json:ro",
        )

        # Credential-bearing catalogs fail before a container is launched.
        write_json(catalog, {"provider": {"local": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://host.containers.internal:18080/v1",
                        "apiKey": "must-not-be-mounted"},
            "models": {"model": {}},
        }}})
        self.argv_log.unlink(missing_ok=True)
        self.run_fail(workspace,
                      "launcher accepted a credential-bearing local-provider catalog",
                      via_path=True, extra_env=env,
                      stderr_contains="invalid local provider catalog")
        if self.argv_log.exists():
            fail("launcher ran a container with a credential-bearing provider catalog")

        # Reject duplicate keys before mounting the host file. The overwritten
        # first provider object deliberately contains a credential field.
        catalog.write_text(
            '{"provider":{"hidden":{"npm":"@ai-sdk/openai-compatible",'
            '"options":{"baseURL":"http://host.invalid/v1",'
            '"apiKey":"must-not-be-mounted"},"models":{"hidden":{}}}},'
            '"provider":{"visible":{"npm":"@ai-sdk/openai-compatible",'
            '"options":{"baseURL":"http://host.containers.internal:18080/v1"},'
            '"models":{"model":{}}}}}\n',
            encoding="utf-8",
        )
        self.run_fail(workspace,
                      "launcher accepted duplicate keys in a local-provider catalog",
                      via_path=True, extra_env=env,
                      stderr_contains="invalid local provider catalog")
        if self.argv_log.exists():
            fail("launcher ran a container with duplicate provider catalog keys")

        # Validation consumes exactly one document and rejects non-object
        # models because the exact host bytes are mounted without rewriting.
        first = json.dumps({"provider": {"first-valid": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://host.containers.internal:18080/v1"},
            "models": {"model": {}},
        }}}, separators=(",", ":"))
        example = (ROOT / "examples" / "local-providers.json.example").read_text(encoding="utf-8")
        example_compact = json.dumps(json.loads(example), separators=(",", ":"))
        catalog.write_text(f"{first}\n{example_compact}\n", encoding="utf-8")
        self.run_fail(workspace, "launcher accepted multiple local-provider JSON documents",
                      via_path=True, extra_env=env,
                      stderr_contains="invalid local provider catalog")

        write_json(catalog, {"provider": {"local": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://host.containers.internal:18080/v1"},
            "models": {"broken": None},
        }}})
        self.run_fail(workspace, "launcher accepted a non-object local model definition",
                      via_path=True, extra_env=env,
                      stderr_contains="invalid local provider catalog")

    def _provider_catalog_unstamped_image(self) -> None:
        # An image built outside the launcher records no catalog digest: `make`
        # never passes the build argument, so the Containerfile default stamps
        # "absent", and an image carrying no label at all is the same case. That
        # is unknown provenance, not staleness: the catalog reaches the container
        # by mount regardless, so the launch must not discard a build the user
        # just paid for, and must say what it could not verify.
        workspace = self.tmp / "provider-unstamped"
        config_home = self.tmp / "provider-unstamped-config"
        shared = config_home / "opencode2"
        shared.mkdir(parents=True)
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:provider-unstamped-test",
            "build": {"containerfile": str(ROOT / "Containerfile"), "context": str(ROOT)},
            "command": ["/bin/true"],
        })
        catalog = shared / "local-providers.json"
        shutil.copyfile(ROOT / "examples" / "local-providers.json.example", catalog)
        env = {
            "HOME": str(self.tmp / "provider-unstamped-home"),
            "XDG_CONFIG_HOME": str(config_home),
            "PODMAN_IMAGE_EXISTS": "1",
        }

        for label, reason in (
            ("absent", "an image whose recorded digest is the Containerfile default"),
            (None, "an image carrying no provider-catalog label"),
        ):
            self.build_argv_log.unlink(missing_ok=True)
            run_env = dict(env)
            if label is not None:
                run_env["PODMAN_IMAGE_LOCAL_PROVIDERS_SHA256"] = label
            proc = self.run_ok(workspace, via_path=True, extra_env=run_env)
            if self.build_argv_log.exists():
                fail(f"launcher rebuilt {reason}")
            self.has_substring([proc.stderr], "records no provider-catalog digest")
            self.has_line(
                self.argv(),
                f"{catalog}:/opt/opencode/config/opencode/opencode.json:ro",
            )

        # A launcher-stamped digest still carries the comparison this check
        # exists for: a moved catalog is exactly one rebuild.
        self.build_argv_log.unlink(missing_ok=True)
        proc = self.run_ok(
            workspace, via_path=True,
            extra_env={**env, "PODMAN_IMAGE_LOCAL_PROVIDERS_SHA256": "0" * 64},
        )
        self.has_line(
            self.build_argv(),
            "io.opencode.local-providers-sha256="
            + hashlib.sha256(catalog.read_bytes()).hexdigest(),
        )
        if "records no provider-catalog digest" in proc.stderr:
            fail(f"a stamped image must not warn about provenance: {proc.stderr}")

    def _run_command_passthrough(self) -> Path:
        # "run" is the default action: a bare invocation, "run", and a bare
        # trailing "--" all pass the configured command. Extra arguments after
        # "run" override the configured command.
        workspace = self.tmp / "bare-separator"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json",
                   {"image": "opencode2:test", "workspace": ".", "command": ["/bin/true"]})
        for args in ((), ("run",), ("run", "--")):
            self.run_ok(workspace, args=args)
            argv = self.argv()
            self.has_line(argv, "opencode2:test")
            self.has_line(argv, "/bin/true")
        # A bare top-level -- is not an invocation; it is a usage error.
        self.run_fail(workspace, "launcher accepted a bare top-level --",
                      args=("--",),
                      stderr_contains=("unrecognized arguments",))
        # Extra arguments after run override the configured command.
        self.run_ok(workspace, args=("run", "ls", "-la"))
        argv = self.argv()
        index = argv.index("opencode2:test")
        tail = [line for line in argv[index + 1:] if line != "opencode2:test"]
        if tail != ["ls", "-la"]:
            fail(f"run command override was mangled: {tail}")
        # The argparse separator passes dash-leading commands through.
        self.run_ok(workspace, args=("run", "--", "-dash-cmd"))
        argv = self.argv()
        index = argv.index("opencode2:test")
        tail = [line for line in argv[index + 1:] if line != "opencode2:test"]
        if tail != ["-dash-cmd"]:
            fail(f"run -- -dash-cmd was mangled: {tail}")
        key = sha256_16(str(workspace))
        self.has_line(self.argv(), f"{workspace}:/workspace/{key}")
        return workspace

    def _dangling_image(self, workspace: Path) -> None:
        # A dangling --image (no value) must fail cleanly, before Podman is
        # ever called.
        self.argv_log.unlink(missing_ok=True)
        self.build_argv_log.unlink(missing_ok=True)
        self.run_fail(workspace, "launcher accepted a dangling --image",
                      args=("run", "--image"),
                      stderr_contains="expected one argument")
        if self.argv_log.exists() or self.build_argv_log.exists():
            fail("launcher reached podman with a dangling --image")

    def _non_utf8_config(self) -> None:
        # A non-UTF-8 sandbox config must fail with a clean error, not a
        # traceback, and must not reach Podman.
        workspace = self.tmp / "bad-utf8"
        workspace.mkdir()
        (workspace / ".opencode-sandbox.json").write_bytes(
            b"\xff\xfe" + b'{"command": ["/bin/true"]}')
        self.argv_log.unlink(missing_ok=True)
        proc = self.run_fail(workspace, "launcher accepted a non-UTF-8 sandbox config",
                             stderr_contains="invalid sandbox config")
        if "Traceback" in proc.stderr:
            fail("launcher crashed with a traceback on a non-UTF-8 sandbox config")
        if self.argv_log.exists():
            fail("launcher reached podman with a non-UTF-8 sandbox config")

    def _command_must_be_array(self) -> None:
        # command must be an array of strings; a bare string is a config
        # error, not silently treated as absent.
        workspace = self.tmp / "command-string"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {"command": "not-an-array"})
        self.argv_log.unlink(missing_ok=True)
        self.run_fail(workspace, "launcher accepted a non-array command value",
                      stderr_contains="command must be an array of strings")
        if self.argv_log.exists():
            fail("launcher reached podman with a non-array command")

    def _shell_subcommand(self, workspace: Path) -> None:
        # The "shell" subcommand drops into a login bash; "run -- shell" runs
        # the literal "shell" program instead.
        alias_workspace = self.tmp / "shell-alias"
        alias_workspace.mkdir()
        shutil.copyfile(workspace / ".opencode-sandbox.json",
                        alias_workspace / ".opencode-sandbox.json")
        self.run_ok(alias_workspace, args=("run", "--", "shell"))
        argv = self.argv()
        self.has_line(argv, "shell")
        self.no_line(argv, "bash")
        self.run_ok(alias_workspace, args=("shell",))
        argv = self.argv()
        self.has_line(argv, "bash")
        self.has_line(argv, "-l")

    def _image_fqn_build(self) -> None:
        # --image selects a fully qualified name to build from the context
        # repository. The build is tagged with the first 8 characters of the
        # context HEAD plus latest, and the run uses the hash-pinned reference.
        context = self.tmp / "fqn-context"
        context.mkdir()
        (context / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
        git("init", "-q", "--initial-branch=main", str(context))
        git("-C", str(context), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "initial", "--allow-empty")
        sha = git("-C", str(context), "rev-parse", "--verify", "HEAD")[:8]
        workspace = self.tmp / "fqn-workspace"
        workspace.mkdir()
        image = "example.com/fqn-test/opencode2"
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:test",
            "build": {"containerfile": str(context / "Containerfile"),
                      "context": str(context)},
            "command": ["/bin/true"],
        })
        self.run_ok(workspace, args=("run", "--image", image),
                    extra_env={"PODMAN_IMAGE_EXISTS": "0"})
        build_argv = self.build_argv()
        self.has_line(build_argv, "-t")
        if self.count(build_argv, "-t") != 2:
            fail("expected two -t tags on the --image build")
        self.has_line(build_argv, f"{image}:{sha}")
        self.has_line(build_argv, f"{image}:latest")
        argv = self.argv()
        self.has_line(argv, f"{image}:{sha}")
        self.no_substring(argv, "opencode2:test")
        self.no_substring(argv, f"{image}:latest")

        # A missing image is not rebuilt when the hash-pinned reference already
        # exists.
        self.build_argv_log.unlink(missing_ok=True)
        self.run_ok(workspace, args=("run", "--image", image),
                    extra_env={"PODMAN_IMAGE_EXISTS": "1"})
        if self.build_argv_log.exists():
            fail("launcher rebuilt despite an existing hash-pinned image")
        self.has_line(self.argv(), f"{image}:{sha}")

        # The build subcommand never runs a container, and without --force it
        # skips an existing image.
        self.argv_log.unlink(missing_ok=True)
        self.run_ok(workspace, args=("build", "--image", image),
                    extra_env={"PODMAN_IMAGE_EXISTS": "1"})
        if self.argv_log.exists():
            fail("build subcommand ran a container")
        if self.build_argv_log.exists():
            fail("build without --force rebuilt an existing image")

        # build --force rebuilds even when the image exists, without running.
        self.run_ok(workspace, args=("build", "--force", "--image", image),
                    extra_env={"PODMAN_IMAGE_EXISTS": "1"})
        build_argv = self.build_argv()
        self.has_line(build_argv, "-t")
        if self.count(build_argv, "-t") != 2:
            fail("expected two -t tags on the forced build")
        self.has_line(build_argv, f"{image}:{sha}")
        self.has_line(build_argv, f"{image}:latest")
        if self.argv_log.exists():
            fail("build --force ran a container")

        # A tag on the --image name is rejected rather than silently mangled.
        self.run_fail(workspace, "launcher accepted a tagged --image name",
                      args=("run", "--image", f"{image}:tagged"),
                      extra_env={"PODMAN_IMAGE_EXISTS": "0"},
                      stderr_contains="without a tag")

        # A build context without git history cannot produce a hash-pinned tag.
        nongit = self.tmp / "nongit-context"
        nongit.mkdir()
        (nongit / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.set_config(workspace / ".opencode-sandbox.json",
                        build={"containerfile": str(nongit / "Containerfile"),
                               "context": str(nongit)})
        self.run_fail(workspace, "launcher accepted --image with a non-git build context",
                      args=("build", "--image", image),
                      extra_env={"PODMAN_IMAGE_EXISTS": "0"},
                      stderr_contains="git repository")

    def _build_requires_build_config(self) -> None:
        # build --force with no build block fails cleanly and never runs a
        # container.
        workspace = self.tmp / "build-no-block"
        workspace.mkdir()
        self.argv_log.unlink(missing_ok=True)
        self.build_argv_log.unlink(missing_ok=True)
        self.run_fail(workspace, "build succeeded without a build block",
                      args=("build", "--force"),
                      extra_env={"PODMAN_IMAGE_EXISTS": "1"},
                      stderr_contains="no build block is configured to rebuild")
        if self.argv_log.exists():
            fail("build ran a container despite a missing build block")
        if self.build_argv_log.exists():
            fail("build reached podman build without a build block")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    try:
        suite = Suite(tmp)
        suite._basic_env_forwarding()
        suite._seccomp_config()
        suite._containers_socket_selection()
        suite._containers_rootless_docker()
        suite._containers_invalid_mode()
        suite._containers_host_socket()
        suite._containers_default_is_nested()
        suite._containers_false_has_no_engine()
        suite._containers_nested()
        suite._nested_agent_environment()
        suite._capability_policy()
        suite._bash_readonly_env_names()
        suite._non_utf8_env_pass()
        suite._git_common_dir_system_path()
        suite._containers_no_socket()
        suite._mount_target_traversal()
        suite._mount_target_system_paths()
        suite._workspace_contains_protected_state()
        suite._mount_source_contains_protected_state()
        suite._root_as_mount_source()
        suite._symlinked_host_state()
        suite._missing_workspace()
        suite._missing_mount_source()
        suite._git_linked_worktree()
        suite._custom_xdg_state()
        suite._relative_mount_source()
        suite._agents_default_pickup()
        suite._agents_absent_is_silent()
        suite._agents_disabled()
        suite._agents_explicit_true_uses_the_default()
        suite._agents_custom_directory()
        suite._agents_missing_override_directory()
        suite._agents_bad_setting()
        suite._agents_rejects_container_id()
        suite._agents_rejects_unusable_entries()
        suite._agents_skips_hidden_files()
        suite._agents_gate_contributions_are_reported()
        suite._agents_rejects_unknown_gate()
        suite._agents_rejects_scalar_gate()
        suite._agents_empty_gates_is_explicitly_advisory()
        suite._agents_undeclared_gates_warns()
        suite._agents_outside_convention_is_silent()
        suite._agents_rejects_unfenced_frontmatter()
        suite._agents_source_is_protected_state()
        suite._agents_reserved_ids_track_the_image()
        default_home, default_workspace = suite._no_config_defaults()
        suite._gitconfig_mounts(default_home, default_workspace)
        global_aid_real, global_aid_link = suite._global_aid_config(
            default_home, default_workspace)
        suite._global_aid_overlap(global_aid_real, global_aid_link,
                                  default_home, default_workspace)
        suite._global_aid_git_metadata_overlap()
        suite._global_aid_and_sandbox_mounted(global_aid_real, global_aid_link,
                                              default_home, default_workspace)
        aid = global_aid_real / "opencode2" / "aid.json"
        suite._malformed_global_aid(default_home, default_workspace, global_aid_link, aid)
        suite._model_router_block_refused(default_home, default_workspace)
        suite._leftover_global_router_warns(default_home, default_workspace)
        suite._partial_config(default_home, default_workspace)
        suite._podman_secrets(default_home, default_workspace)
        suite._secrets_subcommand(default_home, default_workspace)
        suite._data_volume_override(default_home, default_workspace)
        suite._provider_catalog_runtime()
        suite._provider_catalog_unstamped_image()
        bare_workspace = suite._run_command_passthrough()
        suite._dangling_image(bare_workspace)
        suite._non_utf8_config()
        suite._command_must_be_array()
        suite._shell_subcommand(bare_workspace)
        suite._image_fqn_build()
        suite._build_requires_build_config()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
