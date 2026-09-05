#!/usr/bin/env python3
"""In-container driver for the model-router dispatch integration test.

Runs inside the opencode2 container (via `podman exec`) so that every
127.0.0.1 exchange -- mock providers, server health, agent metadata, and
session dispatch -- stays inside the container's network namespace. The
host-side test (model-router-dispatch.test.py) only builds, runs, and
execs; it never needs loopback access into the container.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCATION = "location%5Bdirectory%5D=%2Fworkspace"
MOCK_PROVIDER = Path(__file__).resolve().parent / "mock-model-router-provider.py"

BASE_URL: str
AUTH: str
MOCK: subprocess.Popen | None = None


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def api(method: str, url: str, body: bytes | None = None,
        content_type: str | None = None) -> bytes | None:
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", AUTH)
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        # curl --fail semantics: callers decide whether the error is fatal.
        return None


def api_json(method: str, url: str, **kwargs: object) -> dict:
    raw = api(method, url, **kwargs)
    if raw is None:
        fail(f"API call failed: {method} {url}")
    return json.loads(raw.decode())


def poll_until(description: str, attempts: int, check) -> None:
    for _ in range(attempts):
        if check():
            return
        time.sleep(0.05)
    fail(f"timed out waiting for {description}")


def api_poll_json(url: str) -> dict | None:
    try:
        raw = api("GET", url)
        return json.loads(raw.decode()) if raw is not None else None
    except ValueError:
        return None


def start_mock(parent_port: int, worker_port: int, ready: Path, log: Path) -> None:
    global MOCK
    MOCK = subprocess.Popen(
        [sys.executable, str(MOCK_PROVIDER),
         "--log", str(log), "--ready", str(ready),
         "--parent-port", str(parent_port), "--worker-port", str(worker_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    poll_until("mock providers to start", 100,
               lambda: ready.exists() and ready.stat().st_size > 0)


def dispatch(mode: str) -> str:
    created = api_json("POST", f"{BASE_URL}/api/session",
                       body=json.dumps({"agent": "orchestrator",
                                        "model": {"providerID": "fake-parent",
                                                  "id": "parent-model"},
                                        "location": {"directory": "/workspace"}}).encode(),
                       content_type="application/json")
    if created.get("data", {}).get("agent") != "orchestrator":
        fail(f"session for {mode} was not created on the orchestrator: {created}")
    session = created["data"]["id"]
    prompt = api("POST", f"{BASE_URL}/api/session/{session}/prompt",
                 body=json.dumps({"text": f"MODEL_ROUTER_{mode.upper()}"}).encode(),
                 content_type="application/json")
    if prompt is None:
        fail(f"prompting the {mode} session failed")
    if api("POST", f"{BASE_URL}/api/session/{session}/wait", body=b"") is None:
        fail(f"waiting for the {mode} session failed")
    completed = api_json("GET", f"{BASE_URL}/api/session/{session}")
    data = completed.get("data", {})
    if not (data.get("agent") == "orchestrator"
            and data.get("model", {}).get("providerID") == "fake-parent"
            and data.get("model", {}).get("id") == "parent-model"):
        print(json.dumps(completed, indent=2), file=sys.stderr)
        fail(f"{mode} session did not stay on the parent model")
    return session


def children_for(parent: str) -> list:
    data = api_json("GET", f"{BASE_URL}/api/session?parentID={parent}&directory=%2Fworkspace&limit=20")
    return data.get("data", [])


def main() -> None:
    global BASE_URL, AUTH, MOCK
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-port", type=int, required=True)
    parser.add_argument("--parent-port", type=int, required=True)
    parser.add_argument("--worker-port", type=int, required=True)
    args = parser.parse_args()

    password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    if not password:
        fail("OPENCODE_SERVER_PASSWORD is not set in the container environment")
    BASE_URL = f"http://127.0.0.1:{args.server_port}"
    AUTH = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()

    tmp = Path(tempfile.mkdtemp(prefix="model-router-dispatch."))
    try:
        start_mock(args.parent_port, args.worker_port,
                   tmp / "ready.json", tmp / "requests.jsonl")

        poll_until("opencode server health", 200,
                   lambda: api("GET", f"{BASE_URL}/api/health") is not None)
        if api("GET", f"{BASE_URL}/api/health") is None:
            fail("opencode server did not become healthy")

        parent_agent: dict | None = None
        worker_agent: dict | None = None

        def agents_ready() -> bool:
            nonlocal parent_agent, worker_agent
            parent_agent = api_poll_json(f"{BASE_URL}/api/agent/orchestrator?{LOCATION}")
            worker_agent = api_poll_json(f"{BASE_URL}/api/agent/extractor?{LOCATION}")
            return (parent_agent or {}).get("data", {}).get("id") == "orchestrator" and \
                   (worker_agent or {}).get("data", {}).get("id") == "extractor"

        poll_until("agent metadata", 200, agents_ready)
        if parent_agent is None or worker_agent is None:
            fail("agent metadata never became available")
        parent_agent, worker_agent = parent_agent["data"], worker_agent["data"]

        plugin = api_json("GET", f"{BASE_URL}/api/plugin?{LOCATION}")
        if not any(p.get("id") == "opencode-model-router" and p.get("status") == "active"
                   for p in plugin.get("data", [])):
            fail(f"opencode-model-router plugin is not active: {json.dumps(plugin)}")

        if parent_agent.get("model") != {"providerID": "fake-parent", "id": "parent-model"}:
            fail(f"orchestrator agent is not routed to the parent model: {parent_agent.get('model')}")
        if worker_agent.get("model") != {"providerID": "fake-worker", "id": "worker-model"}:
            fail(f"extractor agent is not routed to the worker model: {worker_agent.get('model')}")
        for agent, marker in ((parent_agent, "parent"), (worker_agent, "worker")):
            if agent.get("request", {}).get("headers", {}).get("x-router-route") != marker or \
                    agent.get("request", {}).get("body", {}).get("router_marker") != marker:
                fail(f"{marker} agent request rewrite is missing: {agent.get('request')}")

        foreground = dispatch("foreground")
        background = dispatch("background")

        for parent in (foreground, background):
            children: list = []

            def extractor_present(bound_parent=parent) -> bool:
                nonlocal children
                children = children_for(bound_parent)
                return any(c.get("agent") == "extractor" for c in children)

            poll_until("extractor child session", 200, extractor_present)
            child = next(c for c in children if c.get("agent") == "extractor")
            if api("POST", f"{BASE_URL}/api/session/{child['id']}/wait", body=b"") is None:
                fail(f"waiting for the extractor child of {parent} failed")
            final = children_for(parent)
            if not any(c.get("parentID") == parent and c.get("agent") == "extractor"
                       and c.get("model", {}).get("providerID") == "fake-worker"
                       and c.get("model", {}).get("id") == "worker-model"
                       for c in final):
                fail(f"extractor child of {parent} did not run on the worker model")

        records = [json.loads(line)
                   for line in (tmp / "requests.jsonl").read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        if not any(r.get("provider") == "parent" and r.get("model") == "parent-model"
                   for r in records):
            fail("mock parent provider never saw a parent-model request")
        if not any(r.get("provider") == "worker" and r.get("model") == "worker-model"
                   for r in records):
            fail("mock worker provider never saw a worker-model request")
        if not all(r.get("model") == "parent-model"
                   for r in records if r.get("provider") == "parent"):
            fail("mock parent provider saw a request for the wrong model")
        if not all(r.get("model") == "worker-model"
                   for r in records if r.get("provider") == "worker"):
            fail("mock worker provider saw a request for the wrong model")

        print("model-router foreground/background dispatch integration test passed "
              "(sandbox model_router block overrides global routing)")
    finally:
        if MOCK is not None:
            MOCK.terminate()
            MOCK = None
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
