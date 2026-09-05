#!/usr/bin/env python3
"""Local-provider catalog build integration test.

Python port of the retired local-provider-build.test.sh. Builds the real
image with present, changed, and absent provider catalogs and checks what
ends up baked in.
"""

import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Scratch lives under the repository (gitignored) so podman can always mount
# the catalog file, regardless of where the system temp dir happens to be.
TMP = ROOT / ".cache" / f"local-provider-build-{os.getpid()}"
TAG = f"opencode2:local-provider-integration-{os.getpid()}"

DUPLICATE_KEY_CATALOG = (
    '{"provider":{"hidden":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:18080/v1","apiKey":"must-not-be-baked"},"models":{"hidden":{}}}},"provider":{"local-openai":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:8080/v1"},"models":{"canonical-model":{}}}}}'
)
INVALID_CATALOG = (
    '{"provider":{"local":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:18080/v1"},"models":{"broken":null}}}}'
)


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def podman_build(catalog: Path | None, digest: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["podman", "build"]
    if catalog is not None:
        cmd += ["--security-opt", "label=disable",
                f"--volume={catalog}:/run/opencode2-build-config/local-providers.json:ro",
                "--build-arg", f"LOCAL_PROVIDERS_SHA256={digest}"]
    else:
        cmd += ["--build-arg", "LOCAL_PROVIDERS_SHA256=absent"]
    cmd += ["--build-arg", f"USER_UID={os.getuid()}",
            "--build-arg", f"USER_GID={os.getgid()}",
            "--build-arg", f"USERNAME={pwd.getpwuid(os.getuid()).pw_name}",
            "-t", TAG, "-f", str(ROOT / "Containerfile"), str(ROOT)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def build_with_catalog(catalog: Path) -> None:
    digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    proc = podman_build(catalog, digest)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        fail(f"build with catalog {catalog.name} failed (exit {proc.returncode})")


def build_must_succeed(desc: str) -> None:
    proc = podman_build(None)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        fail(f"{desc} failed (exit {proc.returncode})")


def build_must_fail(desc: str, catalog: Path | None, digest: str, log_name: str) -> None:
    proc = podman_build(catalog, digest)
    (TMP / log_name).write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode == 0:
        fail(desc)


def run_container(script: str) -> None:
    proc = subprocess.run(
        ["podman", "run", "--rm", "--pull=never", "--entrypoint", "sh", TAG, "-lc", script],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        fail(f"container check failed (exit {proc.returncode}):\n{script}")


def cleanup() -> None:
    subprocess.run(["podman", "image", "rm", "-f", TAG],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    shutil.rmtree(TMP, ignore_errors=True)
    if TMP.parent.is_dir() and not any(TMP.parent.iterdir()):
        TMP.parent.rmdir()


def main() -> None:
    TMP.mkdir(parents=True)
    providers = TMP / "providers.json"
    shutil.copyfile(ROOT / "examples" / "local-providers.json.example", providers)
    build_with_catalog(providers)
    run_container(
        'test "$(stat -c %a /opt/opencode/config/opencode/opencode.json)" = 644; '
        'opencode2 models --standalone | grep -Fx "local-openai/model-1"'
    )

    # The digest must invalidate the install layer when catalog content changes.
    catalog = json.loads(providers.read_text(encoding="utf-8"))
    catalog["provider"]["local-openai"]["models"] = {"changed-model": {"name": "Changed model"}}
    providers.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    build_with_catalog(providers)
    run_container(
        'models="$(opencode2 models --standalone)"; '
        'printf "%s\\n" "${models}" | grep -Fx "local-openai/changed-model"; '
        '! printf "%s\\n" "${models}" | grep -Fx "local-openai/model-1"'
    )

    # Rebuilding without the mount and with the explicit absent key removes a
    # previously imported catalog rather than reusing its cached layer.
    build_must_succeed("absent-catalog build")
    run_container("test ! -e /opt/opencode/config/opencode/opencode.json")

    # Duplicate JSON keys are lossy in jq. Canonicalizing the validated object
    # before installation ensures bytes hidden behind an overwritten key never
    # survive in the image layer, including for direct builds.
    duplicate = TMP / "duplicate-key.json"
    duplicate.write_text(DUPLICATE_KEY_CATALOG + "\n", encoding="utf-8")
    build_with_catalog(duplicate)
    run_container(
        '! grep -Fq "must-not-be-baked" /opt/opencode/config/opencode/opencode.json; '
        'opencode2 models --standalone | grep -Fx "local-openai/canonical-model"'
    )

    # The Containerfile itself verifies both integrity and minimum shape so
    # direct builds cannot bypass the launcher's checks.
    build_must_fail("build accepted a local-provider digest mismatch",
                    TMP / "providers.json", "0" * 64, "mismatch.log")
    invalid = TMP / "invalid.json"
    invalid.write_text(INVALID_CATALOG + "\n", encoding="utf-8")
    invalid_digest = hashlib.sha256(invalid.read_bytes()).hexdigest()
    build_must_fail("build accepted an invalid local-provider catalog",
                    invalid, invalid_digest, "invalid.log")

    print("local-provider build integration tests passed")


def run_test() -> int:
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1
    except Exception as exc:  # noqa: BLE001 -- last-resort guard mirroring set -e + trap
        print(f"local-provider-build test failed: {exc}", file=sys.stderr)
        return 1
    return 0


try:
    status = run_test()
finally:
    cleanup()
sys.exit(status)
