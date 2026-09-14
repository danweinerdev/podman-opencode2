#!/usr/bin/env python3
"""Tag the local opencode2:latest image into a registry path under both the
8-character git hash of HEAD and 'latest', optionally pushing both."""

import argparse
import subprocess
import sys

LOCAL_IMAGE = "opencode2:latest"


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"error: {' '.join(cmd)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "image_path",
        help="registry path, e.g. registry.example.com/opencode2:<tag> "
             "or registry.example.com (tag portion is ignored)",
    )
    parser.add_argument("--push", action="store_true", help="push both tags after tagging")
    args = parser.parse_args()

    # Accept either a bare registry path or a full <path>/opencode2:<tag> spec;
    # strip any trailing tag and any trailing /opencode2 so we can rebuild both tags.
    base = args.image_path.rstrip("/")
    if ":" in base.rsplit("/", 1)[-1]:
        base = base.rsplit(":", 1)[0]
    if base.rsplit("/", 1)[-1] == "opencode2":
        base = base.rsplit("/", 1)[0]
    if not base:
        sys.exit("error: could not determine a registry path from the argument")
    repo = f"{base}/opencode2"

    image_id = run(["podman", "image", "inspect", "--format", "{{.Id}}", LOCAL_IMAGE])
    git_hash = run(["git", "rev-parse", "--short=8", "HEAD"])

    targets = [f"{repo}:{git_hash}", f"{repo}:latest"]
    for target in targets:
        run(["podman", "tag", image_id, target])
        print(f"tagged {target}")

    if args.push:
        for target in targets:
            run(["podman", "push", target])
            print(f"pushed {target}")

    print("\nimages created:")
    for target in targets:
        print(f"  {target}")


if __name__ == "__main__":
    main()
