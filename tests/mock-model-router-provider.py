#!/usr/bin/env python3
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class State:
    def __init__(self, log_path):
        self.log_path = Path(log_path)
        self.lock = threading.Lock()

    def log(self, record):
        with self.lock, self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")


def completion_chunk(model, delta, finish_reason=None):
    return {
        "id": "chatcmpl-model-router-test",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def handler_for(provider, state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_GET(self):
            if self.path.endswith("/models"):
                model = "parent-model" if provider == "parent" else "worker-model"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"object": "list", "data": [{"id": model}]}).encode())
                return
            self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            tools = [tool.get("function", {}).get("name") for tool in body.get("tools", [])]
            messages = body.get("messages", [])
            transcript = json.dumps(messages, separators=(",", ":"))
            route_header = self.headers.get("x-router-route")
            state.log(
                {
                    "provider": provider,
                    "path": self.path,
                    "model": body.get("model"),
                    "route_header": route_header,
                    "route_body": body.get("router_marker"),
                    "tools": tools,
                }
            )

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            model = body.get("model", "unknown")
            if provider == "parent" and "subagent" in tools and not any(
                message.get("role") == "tool" for message in messages
            ):
                background = "MODEL_ROUTER_BACKGROUND" in transcript
                arguments = json.dumps(
                    {
                        "agent": "extractor",
                        "description": "Verify worker routing",
                        "prompt": "Return exactly WORKER_OK.",
                        "background": background,
                    },
                    separators=(",", ":"),
                )
                chunks = [
                    completion_chunk(
                        model,
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call-{'background' if background else 'foreground'}",
                                    "type": "function",
                                    "function": {"name": "subagent", "arguments": arguments},
                                }
                            ],
                        },
                    ),
                    completion_chunk(model, {}, "tool_calls"),
                ]
            else:
                if provider == "worker":
                    time.sleep(0.2)
                    text = "WORKER_OK"
                else:
                    text = "PARENT_OK"
                chunks = [
                    completion_chunk(model, {"role": "assistant", "content": text}),
                    completion_chunk(model, {}, "stop"),
                ]

            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--ready", required=True)
    args = parser.parse_args()

    state = State(args.log)
    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), handler_for("parent", state)),
        ThreadingHTTPServer(("127.0.0.1", 0), handler_for("worker", state)),
    ]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()

    Path(args.ready).write_text(
        json.dumps({"parent": servers[0].server_port, "worker": servers[1].server_port}),
        encoding="utf-8",
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()


if __name__ == "__main__":
    main()
