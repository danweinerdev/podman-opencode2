#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
TAG="opencode2:model-router-dispatch-$$"
CONTAINER="opencode2-model-router-dispatch-$$"
PASSWORD="model-router-test-password"
MOCK_PID=""

cleanup() {
  local status=$?
  if ((status != 0)); then
    printf '%s\n' 'model-router integration test failed; server log:' >&2
    podman logs "${CONTAINER}" >&2 2>/dev/null || true
    if [[ -s "${TMP}/requests.jsonl" ]]; then
      printf '%s\n' 'mock provider requests:' >&2
      jq -s '.' "${TMP}/requests.jsonl" >&2 || true
    fi
  fi
  podman rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  podman image rm -f "${TAG}" >/dev/null 2>&1 || true
  if [[ -n "${MOCK_PID}" ]]; then kill "${MOCK_PID}" >/dev/null 2>&1 || true; fi
  rm -rf "${TMP}"
  return "${status}"
}
trap cleanup EXIT

python3 "${ROOT}/tests/mock-model-router-provider.py" \
  --log "${TMP}/requests.jsonl" --ready "${TMP}/ready.json" &
MOCK_PID=$!
for _ in {1..100}; do
  [[ -s "${TMP}/ready.json" ]] && break
  sleep 0.05
done
[[ -s "${TMP}/ready.json" ]] || { printf 'mock providers did not start\n' >&2; exit 1; }

PARENT_PORT="$(jq -er '.parent' "${TMP}/ready.json")"
WORKER_PORT="$(jq -er '.worker' "${TMP}/ready.json")"
OPENCODE_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"

jq -n \
  --arg parent "http://127.0.0.1:${PARENT_PORT}/v1" \
  --arg worker "http://127.0.0.1:${WORKER_PORT}/v1" \
  '{
    "$schema": "https://opencode.ai/config.json",
    provider: {
      "fake-parent": {
        npm: "@ai-sdk/openai-compatible",
        name: "Fake parent provider",
        options: {baseURL: $parent, apiKey: "parent-test-key"},
        models: {"parent-model": {name: "Parent model"}}
      },
      "fake-worker": {
        npm: "@ai-sdk/openai-compatible",
        name: "Fake worker provider",
        options: {baseURL: $worker, apiKey: "worker-test-key"},
        models: {"worker-model": {name: "Worker model"}}
      }
    }
  }' >"${TMP}/providers.json"

jq -n '{
  schema_version: 1,
  pin_default_agent_model: true,
  profiles: {
    orchestration: {
      model: "fake-parent/parent-model",
      request: {headers: {"x-router-route": "parent"}, body: {router_marker: "parent"}}
    },
    extraction: {
      model: "fake-worker/worker-model",
      request: {headers: {"x-router-route": "worker"}, body: {router_marker: "worker"}}
    }
  },
  agents: {
    orchestrator: "orchestration",
    extractor: "extraction",
    title: "orchestration"
  },
  default_agent: "orchestrator"
}' >"${TMP}/router.json"

podman build \
  --security-opt label=disable \
  --build-arg "USER_UID=$(id -u)" \
  --build-arg "USER_GID=$(id -g)" \
  --build-arg "USERNAME=$(id -un)" \
  -t "${TAG}" -f "${ROOT}/Containerfile" "${ROOT}" >/dev/null

podman run -d --name "${CONTAINER}" --pull=never \
  --userns=keep-id --security-opt label=disable --network host \
  -e "OPENCODE_SERVER_PASSWORD=${PASSWORD}" \
  -e OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router.json \
  -v "${ROOT}:/workspace" \
  -v "${TMP}/providers.json:/opt/opencode/config/opencode/opencode.json:ro" \
  -v "${TMP}/router.json:/run/opencode/model-router.json:ro" \
  -w /workspace --entrypoint opencode2 "${TAG}" \
  serve --hostname 127.0.0.1 --port "${OPENCODE_PORT}" >/dev/null

BASE_URL="http://127.0.0.1:${OPENCODE_PORT}"
api() {
  curl --fail --silent --show-error --max-time 60 -u "opencode:${PASSWORD}" "$@"
}

for _ in {1..200}; do
  if api "${BASE_URL}/api/health" >/dev/null 2>&1; then break; fi
  sleep 0.05
done
api "${BASE_URL}/api/health" >/dev/null

LOCATION='location%5Bdirectory%5D=%2Fworkspace'
PARENT_AGENT=''
WORKER_AGENT=''
for _ in {1..200}; do
  PARENT_AGENT="$(api "${BASE_URL}/api/agent/orchestrator?${LOCATION}" 2>/dev/null || true)"
  WORKER_AGENT="$(api "${BASE_URL}/api/agent/extractor?${LOCATION}" 2>/dev/null || true)"
  if jq -e '.data.id == "orchestrator"' <<<"${PARENT_AGENT}" >/dev/null 2>&1 &&
    jq -e '.data.id == "extractor"' <<<"${WORKER_AGENT}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.05
done
jq -e '.data.id == "orchestrator"' <<<"${PARENT_AGENT}" >/dev/null
jq -e '.data.id == "extractor"' <<<"${WORKER_AGENT}" >/dev/null
PLUGIN="$(api "${BASE_URL}/api/plugin?${LOCATION}")"
jq -e '.data[] | select(.id == "opencode-model-router" and .status == "active")' <<<"${PLUGIN}" >/dev/null
jq -e '.data.model == {providerID:"fake-parent", id:"parent-model"}' <<<"${PARENT_AGENT}" >/dev/null
jq -e '.data.model == {providerID:"fake-worker", id:"worker-model"}' <<<"${WORKER_AGENT}" >/dev/null
jq -e '.data.request.headers["x-router-route"] == "parent" and .data.request.body.router_marker == "parent"' \
  <<<"${PARENT_AGENT}" >/dev/null
jq -e '.data.request.headers["x-router-route"] == "worker" and .data.request.body.router_marker == "worker"' \
  <<<"${WORKER_AGENT}" >/dev/null

dispatch() {
  local mode="$1"
  local completed
  local created
  local session
  created="$(api -H 'Content-Type: application/json' \
    -d '{"agent":"orchestrator","model":{"providerID":"fake-parent","id":"parent-model"},"location":{"directory":"/workspace"}}' \
    "${BASE_URL}/api/session")"
  jq -e '.data.agent == "orchestrator"' <<<"${created}" >/dev/null
  session="$(jq -er '.data.id' <<<"${created}")"
  api -H 'Content-Type: application/json' \
    -d "{\"text\":\"MODEL_ROUTER_${mode^^}\"}" \
    "${BASE_URL}/api/session/${session}/prompt" >/dev/null
  api -X POST "${BASE_URL}/api/session/${session}/wait" >/dev/null
  completed="$(api "${BASE_URL}/api/session/${session}")"
  jq -e '.data.agent == "orchestrator" and
    .data.model.providerID == "fake-parent" and .data.model.id == "parent-model"' \
    <<<"${completed}" >/dev/null || { jq . <<<"${completed}" >&2; return 1; }
  printf '%s\n' "${session}"
}

FOREGROUND_SESSION="$(dispatch foreground)"
BACKGROUND_SESSION="$(dispatch background)"

children_for() {
  api "${BASE_URL}/api/session?parentID=$1&directory=%2Fworkspace&limit=20"
}

for parent in "${FOREGROUND_SESSION}" "${BACKGROUND_SESSION}"; do
  children=''
  for _ in {1..200}; do
    children="$(children_for "${parent}")"
    if jq -e '.data | any(.agent == "extractor")' <<<"${children}" >/dev/null; then break; fi
    sleep 0.05
  done
  child="$(jq -er 'first(.data[] | select(.agent == "extractor")) | .id' <<<"${children}")"
  api -X POST "${BASE_URL}/api/session/${child}/wait" >/dev/null
  jq -e --arg parent "${parent}" '
    .data | any(.parentID == $parent and .agent == "extractor" and
      .model.providerID == "fake-worker" and .model.id == "worker-model")
  ' <<<"$(children_for "${parent}")" >/dev/null
done

REQUESTS="$(jq -s '.' "${TMP}/requests.jsonl")"
jq -e 'any(.[]; .provider == "parent" and .model == "parent-model")' <<<"${REQUESTS}" >/dev/null
jq -e 'any(.[]; .provider == "worker" and .model == "worker-model")' <<<"${REQUESTS}" >/dev/null
jq -e 'all(.[] | select(.provider == "parent"); .model == "parent-model")' <<<"${REQUESTS}" >/dev/null
jq -e 'all(.[] | select(.provider == "worker"); .model == "worker-model")' <<<"${REQUESTS}" >/dev/null

printf 'model-router foreground/background dispatch integration test passed\n'
