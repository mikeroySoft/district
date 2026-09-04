"""`district dashboard [--host 127.0.0.1] [--port 8760] [--no-open]`: the bird's-eye page (PRD §5.6, §5.9).

stdlib http.server. `/` serves the District Atlas; `/api/fleet` returns the
`status --json` shape plus the atlas DATA blocks; `/api/act` POST runs one
`district` subcommand as a subprocess and streams its output. The page never
mutates state itself, so the CLI stays the audited path.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from district import host, status

DEFAULT_PORT = 8760
ACTIONS = ("add", "apply", "rm")
DISTRICT = [sys.executable, "-m", "district"]
# ponytail: static cut until atlas.py renders the page from the live fleet (deliverable 4)
ATLAS_HTML = Path(__file__).resolve().parents[1] / "atlas" / "district-atlas.html"


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return addr == "localhost"


class Handler(BaseHTTPRequestHandler):
    explicit_host = False  # main() sets True when --host was given and is not loopback

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", ATLAS_HTML.read_bytes())
        elif path == "/api/fleet":
            fleet = status.fleet(host.load())
            self._send(200, "application/json", json.dumps({"fleet": fleet}).encode())
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/act":
            self.send_error(404)
            return
        # Same rule as agent-factory: actions only from loopback unless --host opened the port on purpose,
        # and only with a custom header (forces a CORS preflight a stray page cannot pass).
        if not (is_loopback(self.client_address[0]) or self.explicit_host) or self.headers.get("X-District-Act") != "1":
            self.send_error(403)
            return
        try:
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}").get("args")
            if not (isinstance(args, list) and args and all(isinstance(a, str) for a in args) and args[0] in ACTIONS):
                raise ValueError(f"args must start with one of {', '.join(ACTIONS)}")
        except ValueError as exc:
            self._send(400, "text/plain; charset=utf-8", f"{exc}\n".encode())
            return
        self._stream([*DISTRICT, *args])

    def _stream(self, argv: list[str]) -> None:
        """Run argv and relay its stdout+stderr line by line (chunked), ending with `[exit N]`."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def chunk(data: bytes) -> None:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        chunk(f"$ district {' '.join(argv[len(DISTRICT):])}\n".encode())
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        for line in proc.stdout:
            chunk(line)
        chunk(f"[exit {proc.wait()}]\n".encode())
        self.wfile.write(b"0\r\n\r\n")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district dashboard", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1); anything else also opens /api/act")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)
    bind = args.host or "127.0.0.1"
    Handler.explicit_host = args.host is not None and not is_loopback(args.host)
    server = ThreadingHTTPServer((bind, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"district dashboard: listening on {bind}:{args.port}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0
