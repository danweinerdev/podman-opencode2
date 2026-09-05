#!/usr/bin/env python3
"""Model-router foreground/background dispatch integration test.

Python port of the retired model-router-dispatch.test.sh. Builds the real
image, serves it with a
workspace model_router block that must override a user-global routing layer,
and asserts both dispatch modes reach the worker provider.

The assertion driver (model-router-dispatch-driver.py) runs inside the
container via `podman exec`, so all 127.0.0.1 traffic stays in the
container's network namespace: the host never needs loopback access into
the container, and the test works wherever podman can run it.
"""

import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Container-side mount point for the repository root (kept in sync with the
# `-v ${ROOT}:/workspace` flag below).
WORKSPACE_IN_CONTAINER = "/workspace"
SCRATCH = ROOT / ".cache" / f"model-router-dispatch-{os.getpid()}"
TAG = f"opencode2:model-router-dispatch-{os.getpid()}"
CONTAINER = f"opencode2-model-router-dispatch-{os.getpid()}"
PASSWORD = "model-router-test-password"


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def ephemeral_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def container_path(host_path: Path) -> str:
    """Path of a repository file as seen from inside the container."""
    return f"{WORKSPACE_IN_CONTAINER}/{host_path.resolve().relative_to(ROOT).as_posix()}"


def expect_ok(proc: subprocess.CompletedProcess, desc: str) -> None:
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        fail(f"{desc} failed (exit {proc.returncode})")


def cleanup() -> None:
    subprocess.run(["podman", "rm", "-f", CONTAINER],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["podman", "image", "rm", "-f", TAG],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    if SCRATCH.parent.is_dir() and not any(SCRATCH.parent.iterdir()):
        SCRATCH.parent.rmdir()


def main() -> None:
    SCRATCH.mkdir(parents=True)
    # The driver binds the mock ports inside the container, so reserve them
    # here first; a fresh container netns makes collisions effectively
    # impossible.
    parent_port = ephemeral_port()
    worker_port = ephemeral_port()
    server_port = ephemeral_port()

    write_json(SCRATCH / "providers.json", {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "fake-parent": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Fake parent provider",
                "options": {"baseURL": f"http://127.0.0.1:{parent_port}/v1",
                            "apiKey": "parent-test-key"},
                "models": {"parent-model": {"name": "Parent model"}},
            },
            "fake-worker": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Fake worker provider",
                "options": {"baseURL": f"http://127.0.0.1:{worker_port}/v1",
                            "apiKey": "worker-test-key"},
                "models": {"worker-model": {"name": "Worker model"}},
            },
        },
    })

    # The user-global layer routes both agents to the OPPOSITE provider on
    # purpose. Only the workspace model_router block below may correct it, so
    # the final assertions prove the sandbox block's profiles/agents actually
    # won.
    write_json(SCRATCH / "router-global.json", {
        "schema_version": 1,
        "pin_default_agent_model": True,
        "profiles": {
            "orchestration": {"model": "fake-worker/worker-model"},
            "extraction": {"model": "fake-parent/parent-model"},
        },
        "agents": {"orchestrator": "orchestration", "extractor": "extraction"},
        "default_agent": "orchestrator",
    })

    # A full sandbox config (extra keys included) whose top-level model_router
    # block is the workspace override: it must shallow-merge over the global
    # layer and re-route both agents to the correct providers.
    write_json(SCRATCH / "sandbox.json", {
        "schema_version": 1,
        "workspace": ".",
        "model_router": {
            "schema_version": 1,
            "pin_default_agent_model": True,
            "profiles": {
                "orchestration": {
                    "model": "fake-parent/parent-model",
                    "request": {"headers": {"x-router-route": "parent"},
                                "body": {"router_marker": "parent"}},
                },
                "extraction": {
                    "model": "fake-worker/worker-model",
                    "request": {"headers": {"x-router-route": "worker"},
                                "body": {"router_marker": "worker"}},
                },
            },
            "agents": {"orchestrator": "orchestration",
                       "extractor": "extraction",
                       "title": "orchestration"},
            "default_agent": "orchestrator",
        },
    })

    build = run(["podman", "build", "--security-opt", "label=disable",
                 "--build-arg", f"USER_UID={os.getuid()}",
                 "--build-arg", f"USER_GID={os.getgid()}",
                 "--build-arg", f"USERNAME={pwd.getpwuid(os.getuid()).pw_name}",
                 "-t", TAG, "-f", str(ROOT / "Containerfile"), str(ROOT)])
    expect_ok(build, "podman build")

    serve = run(["podman", "run", "-d", "--name", CONTAINER, "--pull=never",
                 "--userns=keep-id", "--security-opt", "label=disable",
                 "--network", "host",
                 "-e", f"OPENCODE_SERVER_PASSWORD={PASSWORD}",
                 "-e", "OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router-global.json",
                 "-e", "OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json",
                 "-v", f"{ROOT}:/workspace",
                 "-v", f"{SCRATCH}/providers.json:/opt/opencode/config/opencode/opencode.json:ro",
                 "-v", f"{SCRATCH}/router-global.json:/run/opencode/model-router-global.json:ro",
                 "-v", f"{SCRATCH}/sandbox.json:/run/opencode/sandbox.json:ro",
                 "-w", "/workspace", "--entrypoint", "opencode2", TAG,
                 "serve", "--hostname", "127.0.0.1", "--port", str(server_port)])
    expect_ok(serve, "podman run")

    # The driver (and the mock provider it locates relative to itself) must
    # be addressed by container path, not host path.
    driver = run(["podman", "exec", "-e", f"OPENCODE_SERVER_PASSWORD={PASSWORD}",
                 CONTAINER, "python3",
                 container_path(Path(__file__).with_name("model-router-dispatch-driver.py")),
                 "--server-port", str(server_port),
                 "--parent-port", str(parent_port),
                 "--worker-port", str(worker_port)])
    sys.stdout.write(driver.stdout)
    sys.stderr.write(driver.stderr)
    if driver.returncode != 0:
        print("model-router integration test failed; server log:", file=sys.stderr)
        subprocess.run(["podman", "logs", CONTAINER], stderr=sys.stderr, check=False)
        fail(f"model-router dispatch driver failed (exit {driver.returncode})")


try:
    main()
    status = 0
except SystemExit as exc:
    code = exc.code
    status = code if isinstance(code, int) else 1
except Exception as exc:  # noqa: BLE001 -- last-resort guard mirroring set -e + trap
    print(f"model-router-dispatch test failed: {exc}", file=sys.stderr)
    status = 1
finally:
    cleanup()
sys.exit(status)
