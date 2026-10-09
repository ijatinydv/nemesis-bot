#!/usr/bin/env python3
"""Starter /decide server for the YeNo × Builderr Volume Bot Challenge."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def decide(observation: dict[str, Any]) -> dict[str, Any]:
    """Replace HOLD with your strategy. Never edit evaluator-owned accounting."""
    del observation
    return {"action": "HOLD"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            response = decide(json.loads(self.rfile.read(length)))
            body = json.dumps(response, separators=(",", ":")).encode()
        except Exception:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
