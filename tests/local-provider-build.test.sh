#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
TAG="opencode2:local-provider-integration-$$"
trap 'podman image rm -f "${TAG}" >/dev/null 2>&1 || true; rm -rf "${TMP}"' EXIT

build_with_catalog() {
  local catalog="$1"
  local digest
  digest="$(sha256sum "${catalog}")"
  digest="${digest%% *}"
  podman build \
    --security-opt label=disable \
    --volume "${catalog}:/run/opencode2-build-config/local-providers.json:ro" \
    --build-arg "LOCAL_PROVIDERS_SHA256=${digest}" \
    --build-arg "USER_UID=$(id -u)" \
    --build-arg "USER_GID=$(id -g)" \
    --build-arg "USERNAME=$(id -un)" \
    -t "${TAG}" -f "${ROOT}/Containerfile" "${ROOT}"
}

cp "${ROOT}/examples/local-providers.json.example" "${TMP}/providers.json"
build_with_catalog "${TMP}/providers.json"
podman run --rm --pull=never --entrypoint sh "${TAG}" -lc '
  test "$(stat -c %a /opt/opencode/config/opencode/opencode.json)" = 644
  opencode2 models --standalone | grep -Fx "local-openai/model-1"
' >/dev/null

# The digest must invalidate the install layer when catalog content changes.
jq '.provider["local-openai"].models = {"changed-model": {"name": "Changed model"}}' \
  "${TMP}/providers.json" > "${TMP}/providers.changed.json"
mv "${TMP}/providers.changed.json" "${TMP}/providers.json"
build_with_catalog "${TMP}/providers.json"
podman run --rm --pull=never --entrypoint sh "${TAG}" -lc '
  models="$(opencode2 models --standalone)"
  printf "%s\n" "${models}" | grep -Fx "local-openai/changed-model"
  ! printf "%s\n" "${models}" | grep -Fx "local-openai/model-1"
' >/dev/null

# Rebuilding without the mount and with the explicit absent key removes a
# previously imported catalog rather than reusing its cached layer.
podman build \
  --build-arg LOCAL_PROVIDERS_SHA256=absent \
  --build-arg "USER_UID=$(id -u)" \
  --build-arg "USER_GID=$(id -g)" \
  --build-arg "USERNAME=$(id -un)" \
  -t "${TAG}" -f "${ROOT}/Containerfile" "${ROOT}"
podman run --rm --pull=never --entrypoint sh "${TAG}" \
  -lc 'test ! -e /opt/opencode/config/opencode/opencode.json'

# Duplicate JSON keys are lossy in jq. Canonicalizing the validated object
# before installation ensures bytes hidden behind an overwritten key never
# survive in the image layer, including for direct builds.
printf '%s\n' '{"provider":{"hidden":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:18080/v1","apiKey":"must-not-be-baked"},"models":{"hidden":{}}}},"provider":{"local-openai":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:8080/v1"},"models":{"canonical-model":{}}}}}' \
  > "${TMP}/duplicate-key.json"
build_with_catalog "${TMP}/duplicate-key.json"
podman run --rm --pull=never --entrypoint sh "${TAG}" -lc '
  ! grep -Fq "must-not-be-baked" /opt/opencode/config/opencode/opencode.json
  opencode2 models --standalone | grep -Fx "local-openai/canonical-model"
' >/dev/null

# The Containerfile itself verifies both integrity and minimum shape so direct
# builds cannot bypass the launcher's checks.
if podman build \
  --security-opt label=disable \
  --volume "${TMP}/providers.json:/run/opencode2-build-config/local-providers.json:ro" \
  --build-arg "LOCAL_PROVIDERS_SHA256=$(printf '0%.0s' {1..64})" \
  --build-arg "USER_UID=$(id -u)" \
  --build-arg "USER_GID=$(id -g)" \
  --build-arg "USERNAME=$(id -un)" \
  -t "${TAG}" -f "${ROOT}/Containerfile" "${ROOT}" \
  >"${TMP}/mismatch.log" 2>&1; then
  printf 'build accepted a local-provider digest mismatch\n' >&2
  exit 1
fi

printf '%s\n' '{"provider":{"local":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"http://host.containers.internal:18080/v1"},"models":{"broken":null}}}}' \
  > "${TMP}/invalid.json"
INVALID_DIGEST="$(sha256sum "${TMP}/invalid.json")"
INVALID_DIGEST="${INVALID_DIGEST%% *}"
if podman build \
  --security-opt label=disable \
  --volume "${TMP}/invalid.json:/run/opencode2-build-config/local-providers.json:ro" \
  --build-arg "LOCAL_PROVIDERS_SHA256=${INVALID_DIGEST}" \
  --build-arg "USER_UID=$(id -u)" \
  --build-arg "USER_GID=$(id -g)" \
  --build-arg "USERNAME=$(id -un)" \
  -t "${TAG}" -f "${ROOT}/Containerfile" "${ROOT}" \
  >"${TMP}/invalid.log" 2>&1; then
  printf 'build accepted an invalid local-provider catalog\n' >&2
  exit 1
fi

printf 'local-provider build integration tests passed\n'
