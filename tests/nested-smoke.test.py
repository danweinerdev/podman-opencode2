#!/usr/bin/env python3
"""Live smoke test for the sandbox image and the launcher's nested mode.

Unlike tests/launcher-env.test.py, which asserts the launcher's argv against a
mocked engine, this drives the real launcher against a real Podman and the
image built from this repository. It therefore runs only where podman is on
PATH and localhost/opencode2:latest exists (make build); otherwise it prints a
skip line and exits 0. The mocked-engine launcher suite cannot cover this: it
needs a real engine and image.

Each case runs in a scratch git workspace under /tmp. The workspace, the
images it builds, and the volumes its runs create are removed afterwards;
volumes created by anything else are left alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Final, NoReturn

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
LAUNCHER: Final[Path] = ROOT / "bin" / "opencode-container"
IMAGE: Final[str] = "localhost/opencode2:latest"
SMALL_IMAGE: Final[str] = "docker.io/library/alpine:latest"

SETUID_HELPERS: Final[list[str]] = ["/usr/bin/newgidmap", "/usr/bin/newuidmap"]
NET_RAW: Final[int] = 1 << 13
SYS_ADMIN: Final[int] = 1 << 21
CAP_LINE: Final[re.Pattern[str]] = re.compile(
    r"^Cap(Eff|Prm|Amb|Bnd|Inh):\s+([0-9a-fA-F]+)$", re.MULTILINE
)


def fail(message: str) -> NoReturn:
    print(f"nested-smoke: {message}", file=sys.stderr)
    sys.exit(1)


def skip(reason: str) -> NoReturn:
    print(f"nested-smoke: skipped: {reason}")
    sys.exit(0)


def run(
    argv: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def require_podman() -> str:
    podman = shutil.which("podman")
    if podman is None:
        skip("podman is not installed")
    if run([podman, "image", "exists", IMAGE]).returncode != 0:
        skip(f"{IMAGE} is not built (run `make build`)")
    return podman


def capabilities(output: str, label: str) -> dict[str, int]:
    found = {name: int(bits, 16) for name, bits in CAP_LINE.findall(output)}
    for required in ("Eff", "Prm", "Inh", "Amb"):
        if required not in found:
            fail(f"{label}: no Cap{required} in:\n{output}")
    return found


def build_image(podman: str, tag: str, containerfile: str, context: Path) -> None:
    proc = run([podman, "build", "-q", "-t", tag, "-f", str(context / containerfile),
                str(context)])
    if proc.returncode != 0:
        fail(f"unable to build {tag}:\n{proc.stdout}{proc.stderr}")


def build_derived(
    podman: str, tmp: Path, name: str, instructions: str, tag: str
) -> str:
    """Build a throwaway image from instructions and return its reference."""
    directory = tmp / name
    directory.mkdir()
    (directory / "Containerfile").write_text(instructions, encoding="utf-8")
    reference = f"localhost/opencode2-smoke-{tag}:latest"
    build_image(podman, reference, "Containerfile", directory)
    return reference


class Sandbox:
    """A scratch git workspace with a sandbox config and its own volumes."""

    def __init__(self, root: Path, config: dict[str, Any], podman: str) -> None:
        self.root = root
        self.podman = podman
        root.mkdir(parents=True)
        config_path = root / ".opencode-sandbox.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", ".opencode-sandbox.json"],
            ["git", "-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid",
             "commit", "-qm", "smoke"],
        ):
            proc = run(argv, cwd=root)
            if proc.returncode != 0:
                fail(f"scratch workspace {argv[0]} failed:\n{proc.stderr}")
        key = hashlib.sha256(str(os.path.realpath(root)).encode("utf-8")).hexdigest()
        self.project_key = key[:16]

    def set_config(self, changes: dict[str, Any]) -> None:
        path = self.root / ".opencode-sandbox.json"
        config = json.loads(path.read_text(encoding="utf-8")) | changes
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def run(self, script: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        proc = run([str(LAUNCHER), "run", "--", "sh", "-c", script], cwd=self.root)
        if proc.returncode != expect:
            fail(
                f"launcher run exited {proc.returncode}, expected {expect} "
                f"({self.root}):\n{proc.stdout}{proc.stderr}"
            )
        return proc

    def run_refused(self, script: str) -> subprocess.CompletedProcess[str]:
        proc = run([str(LAUNCHER), "run", "--", "sh", "-c", script], cwd=self.root)
        if proc.returncode == 0:
            fail(f"the launcher accepted a run it must refuse ({self.root})")
        return proc

    def run_shell(self) -> subprocess.CompletedProcess[str]:
        proc = run([str(LAUNCHER), "shell"], cwd=self.root)
        if proc.returncode != 0:
            fail(f"launcher shell exited {proc.returncode} ({self.root}):\n"
                 f"{proc.stdout}{proc.stderr}")
        return proc

    def cleanup(self) -> None:
        for volume in (
            f"opencode2-data-{self.project_key}",
            f"opencode2-containers-{self.project_key}",
        ):
            run([self.podman, "volume", "rm", "-f", volume])
        shutil.rmtree(self.root, ignore_errors=True)


def image_uid(podman: str) -> int:
    proc = run([podman, "run", "--rm", "--entrypoint", "/usr/bin/id", IMAGE, "-u"])
    if proc.returncode != 0:
        fail(f"unable to read the image's user:\n{proc.stderr}")
    return int(proc.stdout.strip())


def main() -> None:
    podman = require_podman()
    uid = image_uid(podman)

    tmp = Path(tempfile.mkdtemp(prefix="nested-smoke."))
    images: list[str] = []
    sandboxes: list[Sandbox] = []
    try:
        # --- Nested mode: identity, capabilities, cgroup, setuid policy ------
        sandbox = Sandbox(
            tmp / "nested",
            {"schema_version": 1, "image": IMAGE, "containers": "nested"},
            podman,
        )
        sandboxes.append(sandbox)
        probe = sandbox.run(
            "id -u\n"
            "grep -E '^Cap(Eff|Prm|Inh|Amb):' /proc/self/status\n"
            "cat /proc/self/cgroup\n"
            "/usr/bin/find / -xdev -type f -perm /6000 2>/dev/null | /usr/bin/sort\n"
        )
        if probe.stdout.splitlines()[0] != str(uid):
            fail(f"nested run is not the image uid {uid}:\n{probe.stdout}")
        caps = capabilities(probe.stdout, "nested without capabilities")
        for set_name in ("Eff", "Prm", "Inh", "Amb"):
            if caps[set_name] != 0:
                fail(f"nested mode kept Cap{set_name} with no capabilities configured: "
                     f"{caps[set_name]:#x}")
        if "0::/agent/self" not in probe.stdout.splitlines():
            fail(f"the agent is not in its delegated cgroup:\n{probe.stdout}")
        setuid_files = sorted(
            line for line in probe.stdout.splitlines() if line.startswith("/")
        )
        if setuid_files != SETUID_HELPERS:
            fail(f"unexpected setuid/setgid files inside the sandbox: {setuid_files}")

        # --- A configured capability survives the drop exactly --------------
        sandbox.set_config({"capabilities": ["NET_RAW"]})
        probe = sandbox.run(
            "grep -E '^Cap(Eff|Prm|Inh|Amb):' /proc/self/status\n"
        )
        caps = capabilities(probe.stdout, "nested with NET_RAW")
        for set_name in ("Eff", "Prm", "Inh", "Amb"):
            if caps[set_name] != NET_RAW:
                fail(f"Cap{set_name} is not exactly NET_RAW with "
                     f"\"capabilities\": [\"NET_RAW\"]: {caps[set_name]:#x}")

        # --- The agent cannot raise its own subtree's limits -----------------
        sandbox.set_config({"capabilities": []})
        probe = sandbox.run(
            "if printf 1024 > /sys/fs/cgroup/agent/memory.max 2>/dev/null; then\n"
            "  echo WROTE\n"
            "else\n"
            "  echo DENIED\n"
            "fi\n"
            "cat /sys/fs/cgroup/agent/memory.max\n"
        )
        if probe.stdout.splitlines()[:1] != ["DENIED"]:
            fail("the agent wrote /sys/fs/cgroup/agent/memory.max; its own cgroup "
                 "limits must stay out of reach:\n" + probe.stdout)
        if probe.stdout.splitlines()[-1:] == ["1024"]:
            fail("the agent's memory.max changed despite the write being denied:\n"
                 + probe.stdout)

        # --- The inner engine enforces an inner memory limit ----------------
        sandbox.set_config({"capabilities": []})
        inner = sandbox.run(
            f"podman image exists {SMALL_IMAGE} || podman pull -q {SMALL_IMAGE}\n"
            f"podman run --rm --memory 64m {SMALL_IMAGE} "
            "cat /sys/fs/cgroup/memory.max\n"
        )
        if inner.stdout.strip().splitlines()[-1:] != ["67108864"]:
            fail("an inner container's memory.max is not 67108864:\n" + inner.stdout)

        # --- A project environment value reaches the agent ------------------
        value = "value with = and\nnewlines"
        sandbox.set_config({"env": {"set": {"SMOKE_VALUE": value}}})
        probe = sandbox.run('printf %s "$SMOKE_VALUE"')
        if probe.stdout != value:
            fail(f"the agent did not receive its environment value: {probe.stdout!r}")

        # --- A derived image cannot opt out of the prelude ------------------
        no_entrypoint = tmp / "derived-no-entrypoint"
        no_entrypoint.mkdir()
        (no_entrypoint / "Containerfile").write_text(
            f"FROM {IMAGE}\nENTRYPOINT []\n", encoding="utf-8"
        )
        tag = "localhost/opencode2-smoke-noentry:latest"
        build_image(podman, tag, "Containerfile", no_entrypoint)
        images.append(tag)
        sandbox = Sandbox(
            tmp / "no-entrypoint",
            {"schema_version": 1, "image": tag, "containers": "nested"},
            podman,
        )
        sandboxes.append(sandbox)
        probe = sandbox.run("id -u")
        if probe.stdout.strip() != str(uid):
            fail(f"a derived image with ENTRYPOINT [] ran as {probe.stdout.strip()}, "
                 f"not the image uid {uid}")

        # --- A derived image that adds a setuid file is refused -------------
        setuid = tmp / "derived-setuid"
        setuid.mkdir()
        (setuid / "Containerfile").write_text(
            f"FROM {IMAGE}\n"
            "USER root\n"
            "RUN cp /bin/true /usr/local/bin/suid-helper && "
            "chmod 4755 /usr/local/bin/suid-helper\n"
            f"USER {uid}\n",
            encoding="utf-8",
        )
        tag = "localhost/opencode2-smoke-setuid:latest"
        build_image(podman, tag, "Containerfile", setuid)
        images.append(tag)
        sandbox = Sandbox(
            tmp / "setuid",
            {"schema_version": 1, "image": tag, "containers": "nested"},
            podman,
        )
        sandboxes.append(sandbox)
        proc = sandbox.run_refused("true")
        for needle in ("unexpected setuid/setgid files", "/usr/local/bin/suid-helper"):
            if needle not in proc.stderr:
                fail(f"the setuid refusal does not name {needle!r}:\n{proc.stderr}")

        # --- A file capability anywhere on the image is refused -------------
        # /var/tmp is outside the paths an earlier sweep covered. The whole
        # filesystem is checked, and the refusal names the file.
        tag = build_derived(
            podman, tmp, "derived-file-cap",
            f"FROM {IMAGE}\n"
            "USER root\n"
            "RUN cp /bin/true /var/tmp/cap-helper && chmod 0755 /var/tmp/cap-helper "
            "&& setcap cap_net_raw+ep /var/tmp/cap-helper\n"
            f"USER {uid}\n",
            "file-cap",
        )
        images.append(tag)
        sandbox = Sandbox(
            tmp / "file-cap",
            {"schema_version": 1, "image": tag, "containers": "nested"},
            podman,
        )
        sandboxes.append(sandbox)
        proc = sandbox.run_refused("true")
        for needle in ("file capabilities", "/var/tmp/cap-helper"):
            if needle not in proc.stderr:
                fail(f"the file-capability refusal does not name {needle!r}:\n"
                     f"{proc.stderr}")

        # --- An image ENV cannot supply the capability list -----------------
        # The launcher always passes OPENCODE_AGENT_CAPABILITIES, empty when
        # nothing is configured, so an image that sets it gains nothing.
        tag = build_derived(
            podman, tmp, "derived-caps-env",
            f"FROM {IMAGE}\nENV OPENCODE_AGENT_CAPABILITIES=sys_admin\n",
            "caps-env",
        )
        images.append(tag)
        sandbox = Sandbox(
            tmp / "caps-env",
            {"schema_version": 1, "image": tag, "containers": "nested"},
            podman,
        )
        sandboxes.append(sandbox)
        probe = sandbox.run("grep -E '^Cap(Eff|Prm|Inh|Amb):' /proc/self/status\n")
        caps = capabilities(probe.stdout, "an image ENV capability list")
        for set_name in ("Eff", "Prm", "Inh", "Amb"):
            if caps[set_name] != 0:
                fail(f"an image ENV supplied capabilities: Cap{set_name} is "
                     f"{caps[set_name]:#x} with nothing configured")

        # --- The shell subcommand takes the same nested path ----------------
        # `shell` runs `bash -l`. A login shell reads .bash_profile even
        # without a terminal, so a probe image can prove the launcher's shell
        # path drops to the image user in the delegated cgroup.
        tag = build_derived(
            podman, tmp, "derived-shell",
            f"FROM {IMAGE}\n"
            "USER root\n"
            f"RUN home=$(getent passwd {uid} | cut -d: -f6); "
            "printf '%s\\n' 'echo SHELL_OK' 'id -u' 'cat /proc/self/cgroup' "
            '> "$home/.bash_profile"\n'
            f"USER {uid}\n",
            "shell",
        )
        images.append(tag)
        sandbox = Sandbox(
            tmp / "shell",
            {"schema_version": 1, "image": tag, "containers": "nested"},
            podman,
        )
        sandboxes.append(sandbox)
        probe = sandbox.run_shell()
        lines = probe.stdout.splitlines()
        for needle in ("SHELL_OK", str(uid), "0::/agent/self"):
            if needle not in lines:
                fail(f"launcher shell did not report {needle!r}:\n"
                     f"{probe.stdout}{probe.stderr}")

        # --- Engine-free mode: the image user, no SYS_ADMIN in the bounding set
        sandbox = Sandbox(
            tmp / "off",
            {"schema_version": 1, "image": IMAGE, "containers": False},
            podman,
        )
        sandboxes.append(sandbox)
        probe = sandbox.run("id -u\ngrep -E '^CapBnd:' /proc/self/status\n")
        if probe.stdout.splitlines()[0] != str(uid):
            fail(f"the engine-free mode is not the image uid {uid}:\n{probe.stdout}")
        match = re.search(r"^CapBnd:\s+([0-9a-fA-F]+)$", probe.stdout, re.MULTILINE)
        if match is None:
            fail(f"engine-free mode reported no CapBnd:\n{probe.stdout}")
        bounding = int(match.group(1), 16)
        if bounding & SYS_ADMIN:
            fail(f"engine-free mode has SYS_ADMIN in its bounding set: {bounding:#x}")
    finally:
        for sandbox in sandboxes:
            sandbox.cleanup()
        for image in images:
            run([podman, "rmi", "-f", image])
        shutil.rmtree(tmp, ignore_errors=True)
    print(
        "nested-smoke: PASS: nested identity, capabilities, cgroup limits, "
        "image policy, environment delivery, and the shell path"
    )


if __name__ == "__main__":
    main()
