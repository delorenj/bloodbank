"""The "Create Ticket" button: signed URLs, and the small HTTP listener in the sink process that answers a tap.

A target whose sink says `ticket = "button"` files nothing when a message arrives. Its ntfy push carries
one http action button; tapping it makes the phone POST

    https://agents.delo.sh/watch/ticket/<detection_id>?exp=<unix>&sig=<hex>

where sig = hex(HMAC-SHA256(secret, "<detection_id>:<exp>")) and exp is 7 days after the push. Traefik routes
Host(agents.delo.sh) && PathPrefix(/watch/) to this listener (host.docker.internal, the docker0 address).
The secret comes from 1Password through op and lives in memory only.

Deliberately tiny: stdlib asyncio, HTTP/1.x, one request per connection, JSON answers, the body read and
dropped (the button sends none). What a request means is the sink's business (SinkService.button_request).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import http
import json
import re
import socket
import urllib.parse

from .log import log

BUTTON_LABEL = "Create Ticket"
BUTTON_TTL_S = 7 * 86400
DEFAULT_BUTTON_URL = "https://agents.delo.sh/watch/ticket"
SIG = re.compile(r"^[0-9a-fA-F]{64}$")
EXP = re.compile(r"^[0-9]{1,12}$")
MAX_LINE = 8192  # request line or one header
MAX_HEADERS = 100
MAX_BODY = 65536
READ_TIMEOUT_S = 10  # a client that does not finish its request in this long gets a 400 and a closed socket
BIND_RETRY_S = 30
SHUTDOWN_GRACE_S = 100  # a tap whose px is running may finish before the process exits (px is never retried after a kill)


def signature(secret: bytes, detection_id: str, exp: int) -> str:
    """hex HMAC-SHA256 over "<detection_id>:<exp>"."""
    return hmac.new(secret, f"{detection_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(base: str, secret: bytes, detection_id: str, exp: int) -> str:
    return f"{base.rstrip('/')}/{urllib.parse.quote(detection_id, safe='')}?exp={exp}&sig={signature(secret, detection_id, exp)}"


def check(secret: bytes, detection_id: str, exp: str, sig: str, now: float) -> str | None:
    """None when the tap is genuine and unexpired; otherwise why not ('bad signature' or 'expired'). The
    signature is compared in constant time, and checked before the expiry, so an unsigned caller learns
    nothing about a link."""
    if not EXP.fullmatch(exp or "") or not SIG.fullmatch(sig or ""):
        return "bad signature"
    if not hmac.compare_digest(signature(secret, detection_id, int(exp)), sig.lower()):
        return "bad signature"
    if int(exp) < now:
        return "expired"
    return None


def ntfy_action(url: str) -> str:
    """ntfy's Actions header: one http button that POSTs (no body) and clears the notification on success.
    JSON form, not the comma-separated short form: the URL carries '=' and '&', which the short form's
    key=value parsing would take for parameters."""
    return json.dumps([{"action": "http", "label": BUTTON_LABEL, "url": url, "method": "POST", "clear": True}],
                      separators=(",", ":"))


# --- HTTP plumbing -------------------------------------------------------------------------------


class BadRequest(Exception):
    pass


async def _drain_chunked(reader: asyncio.StreamReader) -> None:
    total = 0
    while True:
        line = await reader.readline()
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise BadRequest("bad chunk size") from exc
        if size == 0:
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass  # trailers
            return
        total += size
        if total > MAX_BODY:
            raise BadRequest("body too large")
        await reader.readexactly(size + 2)


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, str]:
    """(METHOD, path, query) of one HTTP/1.x request. The body is read and dropped, so the socket closes clean
    (closing with unread input sends a reset, which a proxy may report as a failed request)."""
    line = await reader.readline()
    if not line.endswith(b"\n"):
        raise BadRequest("no request line")
    parts = line.decode("latin-1").strip().split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise BadRequest("not an HTTP/1.x request line")
    method, target, _ = parts
    length, chunked = 0, False
    for _ in range(MAX_HEADERS):
        header = await reader.readline()
        if header in (b"\r\n", b"\n"):
            break
        if not header.endswith(b"\n"):
            raise BadRequest("the headers broke off")
        name, _, value = header.decode("latin-1").partition(":")
        name, value = name.strip().lower(), value.strip()
        if name == "content-length":
            if not value.isdigit():
                raise BadRequest("bad Content-Length")
            length = int(value)
        elif name == "transfer-encoding":
            chunked = "chunked" in value.lower()
    else:
        raise BadRequest("too many headers")
    if chunked:
        await _drain_chunked(reader)
    elif length:
        if length > MAX_BODY:
            raise BadRequest("body too large")
        await reader.readexactly(length)
    split = urllib.parse.urlsplit(target)
    return method.upper(), split.path, split.query


def response(status: int, body: dict, extra: dict | None = None) -> bytes:
    payload = json.dumps(body, sort_keys=True).encode()
    head = [f"HTTP/1.1 {status} {http.HTTPStatus(status).phrase}", "Content-Type: application/json",
            f"Content-Length: {len(payload)}", "Cache-Control: no-store", "X-Content-Type-Options: nosniff",
            "Connection: close"]
    head += [f"{name}: {value}" for name, value in (extra or {}).items()]
    return ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + payload


def parse_listen(value: str) -> tuple[str, int]:
    """'<address>:<port>' (an IPv6 address in brackets) to (address, port)."""
    host, sep, port = (value or "").strip().rpartition(":")
    host = host.strip("[]")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("TARGET_WATCH_BUTTON_LISTEN looks like <address>:<port>, for example 172.17.0.1:18796")
    return host, int(port)


def listen_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET:
            # The docker0 address exists only once dockerd is up, and the user manager may start the sink first at
            # boot. IP_FREEBIND lets the bind succeed anyway; the kernel delivers to it once the address is there.
            try:
                sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1)
            except OSError:
                pass
        sock.bind((host, port))
        sock.listen(64)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class ButtonListener:
    """The HTTP side of the button, inside the sink process. A bind that fails (the port is taken, the address
    refused) is retried every BIND_RETRY_S in the background: the sink's real job, the alerts, never waits
    for it. `handler(method, path, query)` returns (status, JSON body)."""

    def __init__(self, handler, listen: str | None, *, retry_s: float = BIND_RETRY_S):
        self.handler = handler
        self.listen = listen
        self.retry_s = retry_s
        self.server: asyncio.Server | None = None
        self.address: tuple[str, int] | None = None
        self._binder: asyncio.Task | None = None
        self._clients: set[asyncio.Task] = set()

    async def start(self) -> None:
        if not self.listen:
            log("button_listener_off", reason="TARGET_WATCH_BUTTON_LISTEN is not set")
            return
        try:
            host, port = parse_listen(self.listen)
        except ValueError as exc:
            log("button_listener_off", reason=str(exc))
            return
        if not await self._bind(host, port, attempt=1):
            self._binder = asyncio.get_running_loop().create_task(self._retry(host, port))

    async def _bind(self, host: str, port: int, *, attempt: int) -> bool:
        try:
            sock = listen_socket(host, port)
            self.server = await asyncio.start_server(self._client, sock=sock, limit=MAX_LINE)
        except OSError as exc:
            if attempt == 1 or attempt % 20 == 0:
                log("button_listener_bind_failed", host=host, port=port, attempt=attempt, error=f"{type(exc).__name__}: {exc}")
            return False
        self.address = sock.getsockname()[:2]
        log("button_listener_started", host=self.address[0], port=self.address[1])
        return True

    async def _retry(self, host: str, port: int) -> None:
        attempt = 1
        while True:
            await asyncio.sleep(self.retry_s)
            attempt += 1
            if await self._bind(host, port, attempt=attempt):
                return

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        try:
            status, body = await self._answer(reader)
            writer.write(response(status, body, {"Allow": "POST"} if status == 405 else None))
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - a client that went away must not take anything with it
            log("button_http_write_failed", error=type(exc).__name__)
        finally:
            if task is not None:
                self._clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _answer(self, reader: asyncio.StreamReader) -> tuple[int, dict]:
        try:
            method, path, query = await asyncio.wait_for(read_request(reader), READ_TIMEOUT_S)
        except (BadRequest, ValueError, asyncio.IncompleteReadError, ConnectionError, TimeoutError) as exc:
            log("button_bad_request", error=type(exc).__name__)
            return 400, {"error": "bad request"}
        try:
            return await self.handler(method, path, query)
        except Exception as exc:  # noqa: BLE001 - one bad request never stops the listener
            log("button_handler_failed", error=type(exc).__name__)
            return 500, {"error": "internal error"}

    async def close(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        """Stop accepting; give a tap that is filing up to `grace` seconds to finish."""
        if self._binder is not None:
            self._binder.cancel()
        if self.server is not None:
            self.server.close()
        if self._clients:
            await asyncio.wait(list(self._clients), timeout=grace)
