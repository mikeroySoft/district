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
from urllib.parse import parse_qs, urlparse

from district import atlas, host, status

DEFAULT_PORT = 8760
ACTIONS = ("add", "apply", "rm")
DISTRICT = [sys.executable, "-m", "district"]


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return addr == "localhost"


def act_allowed(client: str, explicit_host: bool, header: str | None) -> bool:
    """Same rule as agent-factory: actions only from loopback unless --host opened the port on purpose,
    and only with the custom header (forces a CORS preflight a stray page cannot pass)."""
    return (is_loopback(client) or explicit_host) and header == "1"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # chunked streaming for /api/act
    explicit_host = False  # main() sets True when --host was given and is not loopback

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, "text/html; charset=utf-8", atlas.page(status.fleet(host.load())).encode())
        elif url.path == "/api/fleet":
            fleet = status.fleet(host.load())
            self._send(200, "application/json", json.dumps({"fleet": fleet, "data": atlas.data(fleet)}).encode())
        elif url.path == "/api/detect":
            # `add --dry-run` runs gh repo view (fork parent) and the check proposal; it writes nothing
            target = parse_qs(url.query).get("target", [""])[0].strip()
            if not target:
                self._send(400, "text/plain; charset=utf-8", b"target required\n")
                return
            proc = subprocess.run([*DISTRICT, "add", "--dry-run", target], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            body = {"ok": proc.returncode == 0, "output": proc.stdout + proc.stderr}
            self._send(200, "application/json", json.dumps(body).encode())
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/act":
            self.send_error(404)
            return
        if not act_allowed(self.client_address[0], self.explicit_host, self.headers.get("X-District-Act")):
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


def units(host_arg: str | None, port: int) -> dict[str, str]:
    """district-dashboard.service + district-metrics.{service,timer}; PATH carried like the factory units."""
    exe = f"{sys.executable} -m district"
    env = f"Environment=PATH={os.environ['PATH']}\n"
    bind = f" --host {host_arg}" if host_arg else ""
    return {
        "district-dashboard.service": (
            "[Unit]\nDescription=District dashboard (fleet atlas + management)\nAfter=network.target\n\n"
            f"[Service]\n{env}ExecStart={exe} dashboard{bind} --port {port} --no-open\n"
            "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n"
        ),
        "district-metrics.service": (
            "[Unit]\nDescription=District metrics refresh (git + gh per factory)\n\n"
            f"[Service]\nType=oneshot\n{env}ExecStart={exe} metrics --refresh\n"
        ),
        "district-metrics.timer": (
            "[Unit]\nDescription=Refresh District metrics hourly\n\n"
            "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=1h\n\n[Install]\nWantedBy=timers.target\n"
        ),
    }


def install(host_arg: str | None, port: int) -> int:
    udir = host.unit_dir()
    udir.mkdir(parents=True, exist_ok=True)
    wanted = units(host_arg, port)
    for name, body in wanted.items():
        path = udir / name
        if not path.exists() or path.read_text() != body:
            path.write_text(body)
            print(f"wrote {path}")
    host.systemctl("daemon-reload")
    for unit in ("district-dashboard.service", "district-metrics.timer"):
        host.systemctl("enable", "--now", unit)
    host.systemctl("restart", "district-dashboard.service")
    print("started district-dashboard.service and district-metrics.timer (hourly district metrics --refresh)")
    print("stop with: systemctl --user disable --now district-dashboard.service district-metrics.timer")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district dashboard", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1); anything else also opens /api/act")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--install", action="store_true", help="write and enable district-dashboard.service and district-metrics.timer")
    args = parser.parse_args(argv)
    if args.install:
        return install(args.host, args.port)
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
