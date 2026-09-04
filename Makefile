.PHONY: build

# Fully qualified image name (no tag). When set, the build is tagged
# $(IMAGE):<git short hash, 8 chars> and $(IMAGE):latest.
IMAGE ?=

build:
	./bin/opencode-container --rebuild $(if $(strip $(IMAGE)),--image "$(strip $(IMAGE))") -- /bin/true
