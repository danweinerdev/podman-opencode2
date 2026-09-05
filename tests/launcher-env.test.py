#!/usr/bin/env python3
"""Launcher contract tests against a mocked podman.

Python port of the retired launcher-env.test.sh. The launcher runs under a
fake `podman` and `id` on PATH; the tests assert on the exact argv and
environment the mock recorded.
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "opencode-container"

MOCK_PODMAN = """\
#!/usr/bin/env python3
import os
import sys

argv = sys.argv[1:]

if argv[:2] == ["image", "exists"]:
    sys.exit(0 if os.environ.get("PODMAN_IMAGE_EXISTS", "1") == "1" else 1)

if argv[:2] == ["secret", "exists"]:
    available = os.environ.get("AVAILABLE_SECRETS", "")
    name = argv[2] if len(argv) > 2 else ""
    sys.exit(0 if f",{name}," in f",{available}," else 1)

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
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        cwd: Path,
        args: tuple[str, ...] = (),
        via_path: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = ["opencode-container", *args] if via_path else [str(LAUNCHER), *args]
        return subprocess.run(cmd, cwd=cwd, env=self.base_env(extra_env),
                              capture_output=True, text=True, check=False)

    def run_ok(
        self,
        cwd: Path,
        args: tuple[str, ...] = (),
        via_path: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        proc = self.run(cwd, args, via_path, extra_env)
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
        stderr_contains: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess:
        proc = self.run(cwd, args, via_path, extra_env)
        if proc.returncode == 0:
            fail(reject)
        for needle in stderr_contains:
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
                   {"schema_version": 1,
                    "profiles": {"reasoning": {"model": "anthropic/claude-opus-4-1"}}})
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

    def _data_volume_override(self, default_home: Path, default_workspace: Path) -> None:
        # The central data volume can be overridden without depending on the
        # launcher's own installation directory.
        write_json(default_workspace / ".opencode-sandbox.json",
                   {"persistence": {"data_volume": "project-opencode-data"}})
        self.run_ok(default_workspace, via_path=True, extra_env={"HOME": str(default_home)})
        argv = self.argv()
        self.has_line(argv, "project-opencode-data:/var/lib/opencode-data:U")
        self.has_line(argv, "XDG_STATE_HOME=/var/lib/opencode-data/state")

    def _provider_catalog_build(self) -> None:
        # An optional user-wide local-provider catalog is validated and exposed
        # only to the image build. Its digest is both a build-cache key and an
        # integrity check.
        workspace = self.tmp / "build-workspace"
        shared = self.tmp / "shared-config" / "opencode2"
        shared.mkdir(parents=True)
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json", {
            "image": "opencode2:provider-build-test",
            "build": {"containerfile": str(ROOT / "Containerfile"),
                      "context": str(ROOT)},
            "command": ["/bin/true"],
        })
        catalog = shared / "local-providers.json"
        shutil.copyfile(ROOT / "examples" / "local-providers.json.example", catalog)
        # The Containerfile canonicalizes with `jq -S`, so the test keeps jq as
        # an independent oracle for the launcher's canonicalization and digest.
        canonical = subprocess.run(["jq", "-S", ".", str(catalog)],
                                   check=True, capture_output=True).stdout
        digest = hashlib.sha256(canonical).hexdigest()
        env = {"HOME": str(self.tmp / "build-home"),
               "XDG_CONFIG_HOME": str(self.tmp / "shared-config"),
               "PODMAN_IMAGE_EXISTS": "0"}
        self.run_ok(workspace, via_path=True, extra_env=env)
        build_argv = self.build_argv()
        self.has_line(build_argv, f"LOCAL_PROVIDERS_SHA256={digest}")
        self.has_line(build_argv, "label=disable")
        if not any(re.fullmatch(r"/.+:/run/opencode2-build-config/local-providers\.json:ro", line)
                   for line in build_argv):
            fail("build did not mount the local-provider catalog")

        # Removing the catalog must produce an explicit absent cache key and no
        # build mount, ensuring a rebuild can also remove a previously baked
        # catalog.
        catalog.unlink()
        self.run_ok(workspace, via_path=True, extra_env=env)
        build_argv = self.build_argv()
        self.has_line(build_argv, "LOCAL_PROVIDERS_SHA256=absent")
        self.no_substring(build_argv, "/run/opencode2-build-config/local-providers.json")

        # Malformed, empty, or credential-bearing catalogs fail before Podman
        # build.
        write_json(catalog, {"provider": {"local": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://host.containers.internal:18080/v1",
                        "apiKey": "must-not-be-baked"},
            "models": {"model": {}},
        }}})
        self.run_fail(workspace,
                      "launcher accepted a credential-bearing local-provider catalog",
                      via_path=True, extra_env=env,
                      stderr_contains="invalid local provider catalog")

        # Validation must consume exactly one document and reject non-object
        # models; otherwise a valid trailing document could hide bytes that get
        # baked verbatim.
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

    def _bare_separator(self) -> Path:
        # A bare `--` separator (no following arguments) passes through to the
        # image's own default command: no project default, no "shell" shortcut,
        # nothing extra.
        workspace = self.tmp / "bare-separator"
        workspace.mkdir()
        write_json(workspace / ".opencode-sandbox.json",
                   {"image": "opencode2:test", "workspace": ".", "command": ["/bin/true"]})
        self.run_ok(workspace, args=("--",))
        argv = self.argv()
        self.has_line(argv, "opencode2:test")
        # Every argument after the image reference would be a command; there
        # must be none.
        index = argv.index("opencode2:test")
        tail = [line for line in argv[index + 1:] if line != "opencode2:test"]
        if any(tail):
            fail("bare -- should not pass any command to the container")
        key = sha256_16(str(workspace))
        self.has_line(argv, f"{workspace}:/workspace/{key}")
        return workspace

    def _dangling_image(self, workspace: Path) -> None:
        # A dangling --image (no value) must fail cleanly, before Podman is
        # ever called.
        self.argv_log.unlink(missing_ok=True)
        self.build_argv_log.unlink(missing_ok=True)
        self.run_fail(workspace, "launcher accepted a dangling --image",
                      args=("--image",),
                      stderr_contains="--image requires a value")
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

    def _explicit_shell_argument(self, workspace: Path) -> None:
        # `-- shell` runs the literal `shell` program; only the unseparated
        # `shell` argument takes the bash -l shortcut.
        alias_workspace = self.tmp / "shell-alias"
        alias_workspace.mkdir()
        shutil.copyfile(workspace / ".opencode-sandbox.json",
                        alias_workspace / ".opencode-sandbox.json")
        self.run_ok(alias_workspace, args=("--", "shell"))
        argv = self.argv()
        self.has_line(argv, "shell")
        self.no_line(argv, "bash")

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
        self.run_ok(workspace, args=("--rebuild", "--image", image),
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
        # exists and --rebuild is not passed.
        self.build_argv_log.unlink(missing_ok=True)
        self.run_ok(workspace, args=("--image", image),
                    extra_env={"PODMAN_IMAGE_EXISTS": "1"})
        if self.build_argv_log.exists():
            fail("launcher rebuilt despite an existing hash-pinned image")
        self.has_line(self.argv(), f"{image}:{sha}")

        # A tag on the --image name is rejected rather than silently mangled.
        self.run_fail(workspace, "launcher accepted a tagged --image name",
                      args=("--image", f"{image}:tagged"),
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
                      args=("--image", image),
                      extra_env={"PODMAN_IMAGE_EXISTS": "0"},
                      stderr_contains="git repository")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    try:
        suite = Suite(tmp)
        suite._basic_env_forwarding()
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
        default_home, default_workspace = suite._no_config_defaults()
        suite._gitconfig_mounts(default_home, default_workspace)
        global_real, global_link = suite._global_router_config(default_home, default_workspace)
        suite._global_router_overlap(global_real, global_link, default_home, default_workspace)
        suite._global_and_workspace_router(global_real, global_link,
                                           default_home, default_workspace)
        router = global_real / "opencode2" / "model-router.json"
        suite._malformed_global_router(default_home, default_workspace, global_link, router)
        suite._partial_config(default_home, default_workspace)
        suite._podman_secrets(default_home, default_workspace)
        suite._data_volume_override(default_home, default_workspace)
        suite._provider_catalog_build()
        bare_workspace = suite._bare_separator()
        suite._dangling_image(bare_workspace)
        suite._non_utf8_config()
        suite._command_must_be_array()
        suite._explicit_shell_argument(bare_workspace)
        suite._image_fqn_build()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
