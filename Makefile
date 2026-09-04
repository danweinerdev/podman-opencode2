.PHONY: build

# Fully qualified image name (no tag). When set, the build is tagged
# $(IMAGE):<git short hash, 8 chars> and $(IMAGE):latest.
IMAGE ?=

build:
	./examples/opencode-container.sh --rebuild $(if $(strip $(IMAGE)),--image "$(strip $(IMAGE))") -- /bin/true
