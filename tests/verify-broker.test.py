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

import contextlib
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


def make_linked(root: Path, checkout: str = "main", name: str = "main",
                pointer: str | None = None) -> tuple[Path, Path]:
    """A `<root>/.bare` common directory with one worktree, as `git worktree add` leaves it."""
    workspace = root / checkout
    workspace.mkdir(parents=True)
    metadata = root / ".bare" / "worktrees" / name
    metadata.mkdir(parents=True)
    if pointer is None:
        pointer = f"gitdir: {os.path.relpath(metadata, workspace)}\n"
    (workspace / ".git").write_text(pointer)
    (metadata / "commondir").write_text("../..\n")
    (metadata / "gitdir").write_text(str(workspace / ".git") + "\n")
    return workspace, metadata


def rejected(workspace: Path) -> bool:
    with open(os.devnull, "w") as sink, contextlib.redirect_stderr(sink):
        try:
            verify.workspace_git_common(str(workspace))
        except SystemExit as error:
            return error.code == verify.USAGE_ERROR
    return False


def test_linked_checkout_mount_and_copy() -> None:
    with tempfile.TemporaryDirectory() as root:
        workspace, metadata = make_linked(Path(root))
        common = str(metadata.parent.parent)
        for pointer in ("gitdir: ../.bare/worktrees/main\n", f"gitdir: {metadata}\n"):
            (workspace / ".git").write_text(pointer)
            linked = verify.workspace_git_common(str(workspace))
            check(linked == verify.LinkedGit(common, "main", "main"),
                  f"valid linked checkout should use only its enclosing .bare: {pointer} -> {linked}")
            script = verify.worker_script(["git", "status"], linked)
            argv = verify.build_argv("image", "2g", "2", "none", str(workspace), script, linked.common)
            mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
            check(mounts == [f"{workspace}:/src:ro", f"{common}:/git-common:ro"],
                  f"only checkout and its .bare may be mounted RO: {mounts}")
            check("cp -a /src/. /tmp/w/main/" in script and
                  "cp -a /git-common/. /tmp/w/.bare/" in script and
                  "cd /tmp/w/main && exec git status" in script,
                  f"linked checkout and metadata must be copied before git runs: {script}")
            check("printf '%s\\n' 'gitdir: ../.bare/worktrees/main' > /tmp/w/main/.git" in script and
                  "printf '%s\\n' /tmp/w/main/.git > /tmp/w/.bare/worktrees/main/gitdir" in script,
                  f"copied metadata must point to scratch, not to the host: {script}")


def test_any_worktree_of_the_bare_is_accepted() -> None:
    # Git names the metadata directory after the checkout, suffixed on collision,
    # and `git worktree add feat/x` nests the checkout; both must work.
    with tempfile.TemporaryDirectory() as root:
        workspace, metadata = make_linked(Path(root), "feat/verify-sandbox", "verify-sandbox1")
        linked = verify.workspace_git_common(str(workspace))
        check(linked == verify.LinkedGit(str(metadata.parent.parent), "feat/verify-sandbox", "verify-sandbox1"),
              f"nested, renamed worktree should be accepted: {linked}")
        script = verify.worker_script(["true"], linked)
        check("printf '%s\\n' 'gitdir: ../../.bare/worktrees/verify-sandbox1' > /tmp/w/feat/verify-sandbox/.git"
              in script and "cd /tmp/w/feat/verify-sandbox && exec true" in script,
              f"scratch must mirror the nested layout: {script}")
    with tempfile.TemporaryDirectory() as root:
        # worktree.useRelativePaths writes a relative backlink.
        workspace, metadata = make_linked(Path(root), "review")
        (metadata / "gitdir").write_text("../../../review/.git\n")
        check(not rejected(workspace), "relative backlink to this checkout must be accepted")


def test_unexpected_git_metadata_is_rejected() -> None:
    for pointer in ("gitdir: ../other/worktrees/main\n", "gitdir: ../../secret\n",
                    "gitdir: /tmp/elsewhere\n", "gitdir: ../.bare/worktrees/main\nextra\n", "garbage\n",
                    "gitdir: ../.bare/worktrees/main", "gitdir: ../.bare\n"):
        with tempfile.TemporaryDirectory() as root:
            workspace, _ = make_linked(Path(root), pointer=pointer)
            check(rejected(workspace), f"must refuse unexpected git pointer {pointer!r}")
    with tempfile.TemporaryDirectory() as root:
        workspace, metadata = make_linked(Path(root))
        (metadata / "commondir").write_text("../../../elsewhere\n")
        check(rejected(workspace), "must refuse unexpected common directory pointer")
        (metadata / "commondir").write_text("../..\n")
        (metadata / "gitdir").write_text("/other/repo/.git\n")
        check(rejected(workspace), "must refuse unexpected reverse git pointer")
        (metadata / "gitdir").unlink()
        (metadata / "gitdir").symlink_to(workspace / ".git")
        check(rejected(workspace), "must refuse symlinked backlink before rewriting it")
    with tempfile.TemporaryDirectory() as root:
        # Another worktree's metadata cannot be borrowed: its backlink names that checkout.
        make_linked(Path(root), "main")
        workspace = Path(root) / "impostor"
        workspace.mkdir()
        (workspace / ".git").write_text("gitdir: ../.bare/worktrees/main\n")
        check(rejected(workspace), "must refuse metadata belonging to another checkout")
    with tempfile.TemporaryDirectory() as root:
        workspace, metadata = make_linked(Path(root))
        (metadata / "gitdir").unlink()
        metadata.rename(metadata.with_name("elsewhere"))
        metadata.symlink_to(metadata.with_name("elsewhere"), target_is_directory=True)
        check(rejected(workspace), "must refuse symlinked metadata directory")
    with tempfile.TemporaryDirectory() as outer:
        # A .bare that does not enclose the checkout cannot be named, even with a
        # backlink that agrees: that would let a pointer choose any host directory.
        foreign = Path(outer) / "foreign"
        _, metadata = make_linked(foreign)
        workspace = Path(outer) / "repo" / "main"
        workspace.mkdir(parents=True)
        (workspace / ".git").write_text(f"gitdir: {metadata}\n")
        (metadata / "gitdir").write_text(str(workspace / ".git") + "\n")
        check(rejected(workspace), "must refuse a .bare outside the checkout's root")
    with tempfile.TemporaryDirectory() as root:
        workspace = Path(root) / ".bare" / "inner"
        workspace.mkdir(parents=True)
        (workspace / ".git").write_text("gitdir: ../worktrees/inner\n")
        check(rejected(workspace), "a checkout inside .bare itself must be refused")


def test_ordinary_checkout_still_needs_only_workspace() -> None:
    with tempfile.TemporaryDirectory() as root:
        workspace = Path(root) / "repo"
        (workspace / ".git").mkdir(parents=True)
        check(verify.workspace_git_common(str(workspace)) is None,
              "ordinary checkout must not require external metadata")
        check(not rejected(workspace), "ordinary checkout must remain accepted")
        check(rejected(Path(root)), "missing .git must be rejected")


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

def test_config_source_follows_the_sentinel() -> None:
    with tempfile.TemporaryDirectory() as root:
        workspace = Path(root) / "repo"
        workspace.mkdir()
        (workspace / ".opencode-sandbox.json").write_text('{"verify": {"network": "pasta"}}')
        sentinel = Path(root) / ".opencode-sandbox"
        check(verify.sandbox_config_path(str(workspace), str(sentinel)) ==
              str(workspace / ".opencode-sandbox.json"),
              "on the host the project's own config must be read")
        sentinel.touch()
        os.environ["OPENCODE_SANDBOX_CONFIG"] = str(workspace / ".opencode-sandbox.json")
        try:
            chosen = verify.sandbox_config_path(str(workspace), str(sentinel))
        finally:
            del os.environ["OPENCODE_SANDBOX_CONFIG"]
        check(chosen == "/run/opencode/sandbox.json",
              f"inside the image only the launcher's RO mount may be read, never the workspace: {chosen}")


def test_unreadable_config_is_an_error_not_defaults() -> None:
    with tempfile.TemporaryDirectory() as root:
        check(verify.load_verify_config(str(Path(root) / "absent.json")) == {},
              "a config that does not exist means defaults")
        hidden = Path(root) / "denied"
        hidden.mkdir()
        (hidden / "sandbox.json").write_text('{"verify": {"checks": {"x": "true"}}}')
        hidden.chmod(0o000)
        try:
            with open(os.devnull, "w") as sink, contextlib.redirect_stderr(sink):
                try:
                    verify.load_verify_config(str(hidden / "sandbox.json"))
                    refused = False
                except SystemExit:
                    refused = True
        finally:
            hidden.chmod(0o700)
        check(refused or os.geteuid() == 0,
              "a config that exists but cannot be read must refuse, not fall back to defaults")


def test_image_carries_the_sentinel() -> None:
    containerfile = (REPO / "Containerfile").read_text()
    verify_containerfile = (REPO / "Containerfile.verify").read_text()
    check("RUN touch /.opencode-sandbox && chmod 0444 /.opencode-sandbox" in containerfile,
          "the base image must bake the read-only sandbox sentinel")
    check(containerfile.index("/.opencode-sandbox ") < containerfile.index("\nUSER "),
          "the sentinel must be created as root, before the image drops privileges")
    check(".opencode-sandbox\n" not in verify_containerfile and "touch /.opencode-sandbox" not in verify_containerfile,
          "the verify worker is not the sandbox and must not carry the sentinel")


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
