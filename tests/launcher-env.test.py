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
    sys.exit(0)

if argv and argv[0] == "build":
    with open(os.environ["BUILD_ARGV_LOG"], "w", encoding="utf-8") as stream:
        for arg in argv:
            stream.write(arg + "\\n")
    sys.exit(0)

print(f"unexpected podman invocation: {' '.join(argv)}", file=sys.stderr)
sys.exit(1)
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
        os.chmod(self.bin / "podman", 0o755)
        os.chmod(self.bin / "id", 0o755)

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
        env["HOME"] = str(self.test_home)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        env["ARGV_LOG"] = str(self.argv_log)
        env["ENV_LOG"] = str(self.env_log)
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
        self.has_line(argv, "OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json")
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
        for name, value in (("missing", ...), ("null", None)):
            root = workspace(name, value)
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

        for name, value in (("empty", ""), ("whitespace", " \t\n")):
            root = workspace(name, value)
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
            "containers": True,
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
                      "containers mode accepted a workdir outside the mirrored workspace",
                      extra_env={"XDG_RUNTIME_DIR": str(runtime)},
                      stderr_contains="must stay within workspace")
        self.set_config(cfg, workdir=".",
                        mounts=[{"source": ".", "target": f"{workspace}/subdir"}])
        self.run_fail(workspace,
                      "containers mode accepted a mount overlapping the workspace mirror",
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
                   {"image": "opencode2:test", "containers": True, "command": ["/bin/true"]})
        self.run_ok(workspace, extra_env={"XDG_RUNTIME_DIR": str(runtime), "FAKE_UID": "424242"})
        argv = self.argv()
        self.has_line(argv, f"{runtime}/docker.sock:/run/opencode-container-engine.sock")
        self.no_line(argv, "keep-groups")

    def _containers_non_boolean(self) -> None:
        # Non-boolean opt-in values fail before Podman is invoked.
        workspace = self.tmp / "containers-invalid"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {"containers": "true"})
        self.run_fail(workspace, "launcher accepted a non-boolean containers value",
                      stderr_contains="containers must be a boolean")

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
        write_json(workspace / ".opencode-sandbox.json", {"containers": True})
        self.run_fail(workspace, "launcher accepted containers mode without an accessible socket",
                      extra_env={"XDG_RUNTIME_DIR": str(runtime), "FAKE_UID": "424242"},
                      stderr_contains="no accessible Podman user or Docker socket was found")

    def _mount_target_traversal(self) -> None:
        # Lexical traversal in a mount target must not bypass the baked-path guard.
        cfg = self.workspace / ".opencode-sandbox.json"
        self.set_config(cfg, mounts=[{"source": ".", "target": "/src/../opt/opencode"}])
        self.run_fail(self.workspace,
                      "launcher accepted a mount target that traverses into a baked path",
                      stderr_contains="shadows a baked config/plugin path")

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
        for agent_id in ("review-quality", "build"):
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
        # A nested path becomes an id the model router rejects, which disables
        # agent routing for the whole container; anything that is not `<id>.md`
        # is not a definition, and a symlink can leave the workspace.
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
        for noise in ("names no review-gates", "outside the review-* convention",
                      "review contributions:"):
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

    def _agents_outside_convention_warns(self) -> None:
        # Only `review-*` ids are granted worker dispatch by the container agents,
        # so the launcher says so instead of leaving a mystery denial.
        workspace = self._new_agents_workspace("agents-convention")
        agents = workspace / ".agents"
        agents.mkdir()
        (agents / "migration-planner.md").write_text(AGENT_BODY, encoding="utf-8")
        proc = self.run_ok(workspace)
        self.has_line(self.argv(), f"{agents}:{AGENTS_TARGET}:ro")
        self.has_substring([proc.stderr], "outside the review-* convention")

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
        # The refusal above only protects the container agents it knows about, so
        # the launcher's list must match what the image bakes.
        loader = importlib.machinery.SourceFileLoader("launcher", str(LAUNCHER))
        spec = importlib.util.spec_from_loader("launcher", loader)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        baked = sorted(path.stem for path in (ROOT / "plugins/model-router/agents").glob("*.md"))
        if sorted(module.BAKED_AGENT_IDS) != baked:
            fail(
                "launcher BAKED_AGENT_IDS drifted from plugins/model-router/agents/:\n"
                f"  launcher: {sorted(module.BAKED_AGENT_IDS)}\n  image: {baked}"
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
        # defaults. The absent control file must not be mounted or advertised
        # to the router.
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
        self.has_line(argv, f"opencode2-data-{key}:/var/lib/opencode-data:U")
        self.no_substring(argv, "/run/opencode/sandbox.json")
        self.no_substring(argv, "/run/opencode/model-router-global.json")
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

    def _global_router_config(self, default_home: Path,
                              default_workspace: Path) -> tuple[Path, Path]:
        # A standalone user-global router config is discovered on every launch,
        # canonicalized, and mounted as one exact read-only file without
        # requiring a workspace sandbox config.
        real = self.tmp / "global-config-real"
        (real / "opencode2").mkdir(parents=True)
        link = self.tmp / "global-config-link"
        link.symlink_to(real)
        write_json(real / "opencode2" / "model-router.json",
                   {"schema_version": 2,
                    "models": {"reasoning": {"model": "anthropic/claude-opus-4-1"}}})
        proc = self.run_ok(default_workspace, via_path=True,
                           extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)})
        if "user-global routing config" not in proc.stderr:
            fail("launcher did not report the user-global routing config")
        if "baked routing defaults" in proc.stderr:
            fail("launcher described routing as baked-only despite a global config")
        argv = self.argv()
        self.has_line(argv,
                      f"{real}/opencode2/model-router.json:/run/opencode/model-router-global.json:ro")
        self.has_line(argv, "OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router-global.json")
        self.no_line(argv, f"{real}/opencode2:/run/opencode")
        return real, link

    def _global_router_overlap(self, real: Path, link: Path,
                               default_home: Path, default_workspace: Path) -> None:
        # The exact read-only control mount must not be bypassable through a
        # workspace or additional mount that exposes the same host file through
        # a writable path.
        workspace = self.tmp / "global-overlap-workspace"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json",
                   {"mounts": [{"source": str(real / "opencode2"),
                                "target": "/mnt/global-config"}]})
        self.run_fail(workspace, "launcher exposed the global router through an additional mount",
                      via_path=True,
                      extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                      stderr_contains="overlaps the user-global model-router config")

    def _global_and_workspace_router(self, real: Path, link: Path,
                                     default_home: Path, default_workspace: Path) -> None:
        # Global and workspace router inputs are independent and must both be
        # mounted when both files exist.
        write_json(default_workspace / ".opencode-sandbox.json", {})
        self.run_ok(default_workspace, via_path=True,
                    extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)})
        argv = self.argv()
        self.has_line(argv,
                      f"{real}/opencode2/model-router.json:/run/opencode/model-router-global.json:ro")
        self.has_line(argv, "OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router-global.json")
        self.has_line(argv, f"{default_workspace}/.opencode-sandbox.json:/run/opencode/sandbox.json:ro")
        self.has_line(argv, "OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json")

    def _malformed_global_router(self, default_home: Path, default_workspace: Path,
                                 link: Path, router: Path) -> None:
        # Malformed, multiple-document, non-object, and non-file global inputs
        # fail before Podman instead of silently falling back to another
        # routing layer.
        cases = (
            ("{ not json\n", "launcher accepted malformed global router JSON"),
            ("{}\n{}\n", "launcher accepted multiple global router JSON documents"),
            ("[]\n", "launcher accepted a non-object global router config"),
        )
        for text, reject in cases:
            router.write_text(text, encoding="utf-8")
            self.run_fail(default_workspace, reject, via_path=True,
                          extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                          stderr_contains="invalid global model-router config")
        router.unlink()
        router.mkdir()
        self.run_fail(default_workspace, "launcher accepted a non-file global router path",
                      via_path=True,
                      extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                      stderr_contains="global model-router config is not a regular file")
        shutil.rmtree(router)

    def _retired_router_shape_rejected(self, default_home: Path, default_workspace: Path,
                                       link: Path, router: Path) -> None:
        # The router's retired v1 shape is refused at launch. The plugin rejects it
        # too, but there the failure is a disabled plugin: every agent follows the
        # session model and one log line is the only trace.
        stale = {"reasoning": {"model": "anthropic/claude-opus-4-1"}}
        cases = (
            ({"schema_version": 1, "profiles": stale}, 'uses "profiles"',
             "launcher accepted a v1 global router file"),
            ({"schema_version": 1, "models": stale}, "unsupported schema_version",
             "launcher accepted a retired router schema_version"),
        )
        for document, needle, reject in cases:
            router.write_text(json.dumps(document) + "\n", encoding="utf-8")
            self.run_fail(default_workspace, reject, via_path=True,
                          extra_env={"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)},
                          stderr_contains=(needle,))

        # A v2 global file launches again, so the guard is about the retired shape
        # and not about the file's presence.
        router.write_text(json.dumps({"schema_version": 2, "models": stale}) + "\n",
                          encoding="utf-8")

        workspace = self.tmp / "router-v1-workspace"
        workspace.mkdir()
        env = {"HOME": str(default_home), "XDG_CONFIG_HOME": str(link)}
        write_json(workspace / ".opencode-sandbox.json",
                   {"model_router": {"schema_version": 1, "profiles": {"x": {"model": "a/b"}}}})
        self.run_fail(workspace, "launcher accepted a v1 model_router block",
                      via_path=True, extra_env=env,
                      stderr_contains=('model_router uses "profiles"',))
        write_json(workspace / ".opencode-sandbox.json", {"model_router": "nope"})
        self.run_fail(workspace, "launcher accepted a non-object model_router block",
                      via_path=True, extra_env=env,
                      stderr_contains=("model_router must be an object",))

        # The point of the new shape: one agent naming its own model, with no
        # shared map declared, launches and is mounted for the plugin to read.
        write_json(workspace / ".opencode-sandbox.json",
                   {"model_router": {"agents": {"review-adr": {"model": "local/my-model"}}}})
        self.run_ok(workspace, via_path=True, extra_env=env)
        self.has_line(self.argv(),
                      f"{workspace}/.opencode-sandbox.json:/run/opencode/sandbox.json:ro")

    def _partial_config(self, default_home: Path, default_workspace: Path) -> None:
        # A present partial config inherits the same defaults and is mounted so
        # its optional router block can be consumed.
        write_json(default_workspace / ".opencode-sandbox.json", {})
        proc = self.run_ok(default_workspace, via_path=True,
                           extra_env={"HOME": str(default_home)})
        if "no .opencode-sandbox.json found" in proc.stderr:
            fail("launcher warned despite a present sandbox config")
        argv = self.argv()
        self.has_line(argv, "opencode2:latest")
        self.has_line(argv, f"{default_workspace}/.opencode-sandbox.json:/run/opencode/sandbox.json:ro")
        self.has_line(argv, "OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json")

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
        self.has_line(argv, "OPENAI_API_KEY")

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
                   {"persistence": {"data_volume": "project-opencode-data"}})
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
        suite._containers_non_boolean()
        suite._containers_no_socket()
        suite._mount_target_traversal()
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
        suite._agents_outside_convention_warns()
        suite._agents_rejects_unfenced_frontmatter()
        suite._agents_source_is_protected_state()
        suite._agents_reserved_ids_track_the_image()
        default_home, default_workspace = suite._no_config_defaults()
        suite._gitconfig_mounts(default_home, default_workspace)
        global_real, global_link = suite._global_router_config(default_home, default_workspace)
        suite._global_router_overlap(global_real, global_link, default_home, default_workspace)
        suite._global_and_workspace_router(global_real, global_link,
                                           default_home, default_workspace)
        router = global_real / "opencode2" / "model-router.json"
        suite._malformed_global_router(default_home, default_workspace, global_link, router)
        suite._retired_router_shape_rejected(default_home, default_workspace, global_link, router)
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
