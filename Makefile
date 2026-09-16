.PHONY: build base dev verify-image

# Fully qualified base-image name (no tag). When set, the base is tagged
# $(IMAGE):<git short hash, 8 chars> and $(IMAGE):latest, and the dev image
# is built on top of $(IMAGE):latest.
IMAGE ?=

BASE_IMAGE ?= $(if $(strip $(IMAGE)),$(IMAGE),opencode2):latest
DEV_IMAGE ?= opencode2-dev:latest

# Base runtime image, rebuilt from Containerfile. Machine-local provider
# catalogs are mounted by the launcher at runtime, never included in builds.
base:
	podman build \
		--build-arg "USER_UID=$$(id -u)" \
		--build-arg "USER_GID=$$(id -g)" \
		--build-arg "USERNAME=$$(id -un)" \
		$(if $(strip $(IMAGE)),-t $(IMAGE):$$(git rev-parse --short=8 HEAD) -t $(IMAGE):latest,-t opencode2:latest) \
		-f Containerfile .

# Development/test image built on top of the base (podman-remote client,
# Python dev tooling). The base must exist locally first.
dev:
	podman build --pull=never \
		--build-arg "BASE_IMAGE=$(BASE_IMAGE)" \
		--build-arg "USER_UID=$$(id -u)" \
		--build-arg "USER_GID=$$(id -g)" \
		--build-arg "USERNAME=$$(id -un)" \
		-t $(DEV_IMAGE) -f Containerfile.dev .

# Disposable verification worker for review lanes. Independent of the runtime
# images and cheap to rebuild, so rebuild it whenever requirements.txt moves:
# ruff and mypy versions are pinned from there. It ships a toolchain only, so a
# project whose tests need more than Python/Node/git/jq should point its own
# sandbox config's verify.image at an image that has them.
VERIFY_IMAGE ?= opencode2-verify:latest

verify-image:
	podman build --pull=never -t $(VERIFY_IMAGE) -f Containerfile.verify .

# Full rebuild: base, then dev.
build: base dev
