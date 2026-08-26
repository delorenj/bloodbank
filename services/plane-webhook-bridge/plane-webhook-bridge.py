#!/usr/bin/env python3
"""Compatibility relay for the historical Plane webhook URL.

Plane should target n8n's /webhook/plane endpoint directly. Existing Plane
installations may still target this service on port 8477, so the relay verifies
the original HMAC and forwards the byte-identical body and signature headers to
n8n. All normalization and Bloodbank publication therefore have one provenance
and one implementation: the versioned n8n Plane to Bloodbank workflow.

No event envelope is constructed here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


HOST = os.environ.get("HERMES_PLANE_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("HERMES_PLANE_BRIDGE_PORT", "8477"))
SECRET = os.environ.get("PLANE_WEBHOOK_SECRET", "")
N8N_WEBHOOK_URL = os.environ.get(
    "N8N_PLANE_WEBHOOK_URL",
    "http://localhost:5678/webhook/plane",
)
TIMEOUT_SECONDS = float(os.environ.get("N8N_PLANE_WEBHOOK_TIMEOUT_SECONDS", "10"))


def validate_target(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("N8N_PLANE_WEBHOOK_URL must be an http(s) URL with a hostname")
    if parsed.username or parsed.password:
        raise ValueError("N8N_PLANE_WEBHOOK_URL must not contain credentials")


def verify_signature(raw: bytes, headers) -> bool:
    if not SECRET:
        return True
    supplied = (
        headers.get("X-Plane-Signature", "")
        or headers.get("X-Hub-Signature-256", "")
    )
    if supplied.lower().startswith("sha256="):
        supplied = supplied.split("=", 1)[1]
    expected = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied.lower(), expected)


def relay_headers(headers) -> dict[str, str]:
    out = {"Content-Type": headers.get("Content-Type", "application/json")}
    for name in ("X-Plane-Signature", "X-Hub-Signature-256", "User-Agent"):
        value = headers.get(name)
        if value:
            out[name] = value
    return out


def forward(raw: bytes, headers) -> tuple[int, bytes, str]:
    request = Request(
        N8N_WEBHOOK_URL,
        data=raw,
        headers=relay_headers(headers),
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return (
                response.status,
                response.read(),
                response.headers.get("Content-Type", "application/json"),
            )
    except HTTPError as error:
        return (
            error.code,
            error.read(),
            error.headers.get("Content-Type", "application/json"),
        )
    except URLError as error:
        raise RuntimeError(f"n8n webhook unavailable: {error.reason}") from error


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, body: dict):
        self._send(code, json.dumps(body).encode())

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self._json(
                200,
                {
                    "ok": True,
                    "mode": "n8n-compatibility-relay",
                    "hmac": bool(SECRET),
                    "target_host": urlparse(N8N_WEBHOOK_URL).hostname,
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in {"/", "/plane-webhook"}:
            self._json(404, {"error": "not found"})
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if not verify_signature(raw, self.headers):
            self._json(401, {"error": "bad signature"})
            return
        try:
            code, body, content_type = forward(raw, self.headers)
        except Exception as error:  # noqa: BLE001 - HTTP boundary
            print(f"[plane-relay] forward failed: {error}", file=sys.stderr)
            self._json(502, {"error": "n8n webhook unavailable"})
            return
        print(f"[plane-relay] {len(raw)} bytes -> n8n ({code})")
        self._send(code, body, content_type)


def selftest() -> int:
    try:
        validate_target(N8N_WEBHOOK_URL)
    except ValueError as error:
        print(f"SELFTEST: FAIL -- {error}", file=sys.stderr)
        return 1
    if not SECRET:
        print("SELFTEST: FAIL -- PLANE_WEBHOOK_SECRET is not resolved", file=sys.stderr)
        return 1
    sample = b'{"event":"issue","action":"created","data":{"id":"ticket"}}'
    signature = hmac.new(SECRET.encode(), sample, hashlib.sha256).hexdigest()
    if not verify_signature(sample, {"X-Plane-Signature": signature}):
        print("SELFTEST: FAIL -- HMAC verification failed", file=sys.stderr)
        return 1
    print(
        "SELFTEST: PASS -- compatibility relay targets "
        f"{urlparse(N8N_WEBHOOK_URL).hostname}; HMAC enabled"
    )
    return 0


def main() -> int:
    validate_target(N8N_WEBHOOK_URL)
    if "--selftest" in sys.argv:
        return selftest()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"[plane-relay] listening on {HOST}:{PORT}; forwarding to "
        f"{urlparse(N8N_WEBHOOK_URL).hostname}; "
        f"{'HMAC on' if SECRET else 'HMAC OFF'}"
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
