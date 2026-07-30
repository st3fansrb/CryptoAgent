"""Local HTTP trigger for CryptoAgent.

Lets n8n (or any scheduler) run a decision cycle via a simple HTTP request,
which is the portable way to drive the agent when n8n's "Execute Command" node
is unavailable. Uses ONLY the Python standard library — no new dependencies.

Binds to localhost only (never exposed to the network).

Endpoints:
    GET /run     -> run one decision cycle; returns the cycle summary as JSON
    GET /health  -> liveness check

Usage:
    python server.py            # listens on http://127.0.0.1:8000
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from main import run_cycle

HOST = "127.0.0.1"
PORT = 8000


class _Handler(BaseHTTPRequestHandler):
    """Minimal request handler exposing /run and /health."""

    def _send(self, code: int, payload: dict) -> None:
        """Serialize ``payload`` to JSON and write the HTTP response."""
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - required name by BaseHTTPRequestHandler
        """Handle GET requests for /run and /health."""
        if self.path.startswith("/run"):
            try:
                summary = run_cycle()
                self._send(200, {"ok": True, "summary": summary})
            except Exception as exc:  # noqa: BLE001 - report any cycle error
                self._send(500, {"ok": False, "error": str(exc)})
        elif self.path.startswith("/health"):
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, *args) -> None:  # noqa: D401 - silence default logging
        """Suppress the default per-request stderr logging."""
        return


def main() -> None:
    """Start the threaded HTTP server and serve until interrupted."""
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    print(f"CryptoAgent trigger server listening on http://{HOST}:{PORT}")
    print("  GET /run     -> run one cycle")
    print("  GET /health  -> liveness check")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.server_close()


if __name__ == "__main__":
    main()
