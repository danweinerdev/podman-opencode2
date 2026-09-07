.PHONY: build base dev

# Fully qualified base-image name (no tag). When set, the base is tagged
# $(IMAGE):<git short hash, 8 chars> and $(IMAGE):latest, and the dev image
# is built on top of $(IMAGE):latest.
IMAGE ?=

BASE_IMAGE ?= $(if $(strip $(IMAGE)),$(IMAGE),opencode2):latest
DEV_IMAGE ?= opencode2-dev:latest

# Base runtime image, rebuilt from Containerfile. Plain build: no
# machine-local provider catalog (see the README for the catalog-including
# build).
base:
	podman build \
		--build-arg "USER_UID=$$(id -u)" \
		--build-arg "USER_GID=$$(id -g)" \
		--build-arg "USERNAME=$$(id -un)" \
		--build-arg "LOCAL_PROVIDERS_SHA256=absent" \
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

# Full rebuild: base, then dev.
build: base dev
