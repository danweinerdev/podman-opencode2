#!/usr/bin/env python3
"""Tests for bin/oc2-verify, the disposable verification worker broker.

These assert on the broker's privileged decisions rather than on container
behaviour, so no container engine is required: the whole point of oc2-verify is
that a caller cannot widen what a worker can see, and the assertions below are
what makes that claim checkable. A change that adds a read-write host mount,
drops the read-only flag, forwards the agent environment, or mounts the engine
socket fails here instead of in production.

Silent on success; raises with a diff on failure, matching tests/launcher-env.test.py.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Final, NoReturn

REPO: Final[Path] = Path(__file__).resolve().parent.parent
MODULE_PATH: Final[Path] = REPO / "bin" / "oc2-verify"

spec = importlib.util.spec_from_loader(
    "oc2_verify", importlib.machinery.SourceFileLoader("oc2_verify", str(MODULE_PATH))
)
assert spec and spec.loader
verify = importlib.util.module_from_spec(spec)
sys.modules["oc2_verify"] = verify
spec.loader.exec_module(verify)

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def fail(message: str) -> NoReturn:
    raise AssertionError(message)


def argv_for(command: Any) -> list[str]:
    """The exact podman argv the broker would run, with a placeholder container name."""
    return verify.build_argv(
        "localhost/opencode2-verify:latest", "2g", "2", "none", "/work/repo",
        verify.worker_script(command),
    )


# --- The worker's reach ------------------------------------------------------

def test_workspace_is_read_only() -> None:
    argv = argv_for(["ruff", "check", "."])
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
    check(mounts == ["/work/repo:/src:ro"], f"workspace must enter read-only, got {mounts}")
    check(all(m.endswith(":ro") for m in mounts), f"every mount must end in :ro, got {mounts}")


def test_only_the_workspace_is_mounted() -> None:
    argv = argv_for("ruff check .")
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
    check(len(mounts) == 1, f"exactly one mount expected, got {mounts}")
    check(not any("/run" in m or ".sock" in m or "/home" in m.split(":")[0] for m in mounts),
          f"no host state or engine socket may be mounted, got {mounts}")


def test_no_engine_socket_or_host_home_in_argv() -> None:
    argv = argv_for("mypy .")
    joined = " ".join(argv)
    for forbidden in ("opencode-container-engine.sock", "/run/user", "docker.sock"):
        check(forbidden not in joined, f"argv must not reference {forbidden}: {joined}")


def test_worker_is_disposable_and_never_pulls() -> None:
    argv = argv_for("true")
    check("--rm" in argv, "worker must be started with --rm so its layer is discarded")
    check("--pull=never" in argv, "worker must never pull at run time; an unreviewed reference must not be fetched")
    check("--userns=keep-id" in argv, "keep-id keeps workspace file ownership correct without a baked uid")
    check("--memory" in argv and "--cpus" in argv, "resource ceilings are required: a lane must not exhaust the host")


def test_scratch_is_a_copy_not_the_mount() -> None:
    script = verify.worker_script(["pytest", "-q"])
    check("cp -a /src/. /tmp/w/" in script, "the command must run against a copy, not the read-only mount")
    check("cd /tmp/w" in script, "the command must run inside the scratch copy")
    check(script.index("cp -a") < script.index("exec"), "the copy must complete before the command runs")


# --- Argument fidelity -------------------------------------------------------

def test_list_command_preserves_argument_boundaries() -> None:
    script = verify.worker_script(["python3", "-c", "print('hi')", "a b", "c;d"])
    tail = script.split("exec ", 1)[1]
    parsed = shlex.split(tail)
    check(parsed == ["python3", "-c", "print('hi')", "a b", "c;d"],
          f"argv must survive the round trip exactly, got {parsed}")


def test_string_check_is_parsed_once_not_quoted() -> None:
    script = verify.worker_script("ruff check . && mypy .")
    check("ruff check . && mypy ." in script, "a configured check is shell text and must stay shell text")
    check("( " in script, "a configured check must be wrapped so its operators cannot escape the scratch cwd")


def test_describe_command_is_reparsable() -> None:
    described = verify.describe_command(["python3", "-c", "print('hi')"])
    check(shlex.split(described) == ["python3", "-c", "print('hi')"], f"report must re-parse: {described}")


# --- Environment -------------------------------------------------------------

def test_environment_is_minimal() -> None:
    env = verify.env_for_worker()
    values = [env[i + 1] for i, part in enumerate(env) if part == "--env"]
    joined = " ".join(values)
    check("HOME=/tmp/w" in joined, "HOME must live in the scratch copy")
    for forbidden in ("API_KEY", "TOKEN", "SECRET", "OPENCODE_", "AUTH"):
        check(forbidden not in joined, f"worker env must not carry {forbidden}: {values}")


# --- Configuration -----------------------------------------------------------

def with_config(payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
    try:
        return verify.load_verify_config(handle.name)
    finally:
        os.unlink(handle.name)


def test_unknown_verify_field_is_refused() -> None:
    import contextlib

    for key, payload in (
        ("typo", {"verify": {"imagem": "x"}}),
        ("bad network", {"verify": {"network": "host"}}),
        ("bad timeout", {"verify": {"timeout_seconds": 99999}}),
        ("bad checks", {"verify": {"checks": {"a": ""}}}),
    ):
        try:
            # Each rejection prints its reason on stderr; the suite stays silent on success.
            with open(os.devnull, "w") as sink, contextlib.redirect_stderr(sink):
                with_config(payload)
        except SystemExit:
            continue
        FAILURES.append(f"verify config must reject {key}, but it was accepted")


def test_absent_block_is_defaults() -> None:
    check(with_config({"schema_version": 1}) == {}, "a config with no verify block means defaults, not an error")
    block = with_config({"verify": {"image": "quay.example/proj:1", "checks": {"lint": "ruff check ."}}})
    check(block.get("image") == "quay.example/proj:1", "a project may supply its own worker image")


def test_default_image_is_the_baked_verify_worker() -> None:
    check(verify.DEFAULT_IMAGE == "localhost/opencode2-verify:latest",
          f"unexpected default image {verify.DEFAULT_IMAGE}")
    check((REPO / "Containerfile.verify").is_file(), "Containerfile.verify must exist to build DEFAULT_IMAGE")


def main() -> None:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    if FAILURES:
        for failure in FAILURES:
            print(f"FAIL: {failure}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
