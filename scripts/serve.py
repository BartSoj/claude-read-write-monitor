#!/usr/bin/env python3
"""read-write-monitor: local dashboard server.

One shared server for every session on this machine. It is a dumb reader of the
event log — hooks never talk to it, they only append files. That keeps the hook
path fast and lets the dashboard be rewritten without touching the recorder.

  serve.py ensure   start it if it isn't already listening (called by SessionStart)
  serve.py run      run in the foreground
  serve.py stop     shut it down

Env: RWM_PORT (default 7788), RWM_DATA_DIR, RWM_IDLE_MINUTES (default 120).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")
PORT = int(os.environ.get("RWM_PORT", "7788"))
IDLE_SECONDS = int(os.environ.get("RWM_IDLE_MINUTES", "120")) * 60

_last_request = time.time()


def data_dir() -> str:
    d = os.environ.get("RWM_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        d = os.path.expanduser("~/.claude/plugins/data/read-write-monitor")
    return d


def sessions_dir() -> str:
    return os.path.join(data_dir(), "sessions")


def list_sessions() -> list[dict]:
    root = sessions_dir()
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        ev = os.path.join(root, name, "events.jsonl")
        if not os.path.exists(ev):
            continue
        meta = {}
        try:
            meta = json.load(open(os.path.join(root, name, "meta.json")))
        except Exception:
            pass
        st = os.stat(ev)
        out.append({
            "id": name,
            "cwd": meta.get("cwd"),
            "title": meta.get("title"),
            "closed": bool(meta.get("closed")),
            "started_at": meta.get("started_at"),
            "updated_at": int(st.st_mtime * 1000),
            "bytes": st.st_size,
        })
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def read_events(session_id: str, offset: int) -> dict:
    path = os.path.join(sessions_dir(), session_id, "events.jsonl")
    events, next_offset = [], offset
    try:
        size = os.path.getsize(path)
        if offset > size:  # log truncated or replaced
            offset, next_offset = 0, 0
        with open(path, "rb") as f:
            f.seek(offset)
            blob = f.read()
        # Only hand back whole lines; a hook may be mid-append.
        cut = blob.rfind(b"\n")
        if cut >= 0:
            next_offset = offset + cut + 1
            for line in blob[:cut].split(b"\n"):
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    meta = {}
    try:
        meta = json.load(open(os.path.join(sessions_dir(), session_id, "meta.json")))
    except Exception:
        pass
    return {"offset": next_offset, "events": events, "meta": meta}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence stderr chatter
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _file(self, name: str) -> None:
        path = os.path.join(WEB, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._send(404, b"not found", "text/plain")
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else "text/plain"
        self._send(200, body, ctype)

    def do_GET(self):
        global _last_request
        _last_request = time.time()
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._file("viewer.html")  # the viewer picks the newest session
        if p == "/health":
            return self._json({"ok": True, "port": PORT, "data": data_dir()})
        if p == "/api/sessions":
            return self._json({"sessions": list_sessions()})
        if p.startswith("/api/events/"):
            sid = unquote(p[len("/api/events/"):]).strip("/")
            try:
                offset = int(q.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            return self._json(read_events(sid, offset))
        if p.startswith("/s/"):
            return self._file("viewer.html")
        if p == "/viewer.html":
            return self._file("viewer.html")
        return self._send(404, b"not found", "text/plain")


def _idle_watchdog() -> None:
    while True:
        time.sleep(60)
        newest = 0.0
        try:
            for name in os.listdir(sessions_dir()):
                ev = os.path.join(sessions_dir(), name, "events.jsonl")
                if os.path.exists(ev):
                    newest = max(newest, os.path.getmtime(ev))
        except OSError:
            pass
        if time.time() - max(_last_request, newest) > IDLE_SECONDS:
            os._exit(0)


def is_up() -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def pid_path() -> str:
    return os.path.join(data_dir(), "server.pid")


def run() -> int:
    os.makedirs(sessions_dir(), exist_ok=True)
    with open(pid_path(), "w") as f:
        f.write(str(os.getpid()))
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
    return 0


def ensure() -> int:
    if is_up():
        return 0
    log = os.path.join(data_dir(), "server.log")
    os.makedirs(data_dir(), exist_ok=True)
    with open(log, "a") as lf:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "run"],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(30):
        if is_up():
            return 0
        time.sleep(0.1)
    return 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "ensure":
        sys.exit(ensure())
    if cmd == "stop":
        import signal
        try:
            pid = int(open(pid_path()).read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"stopped {pid}")
        except Exception as exc:
            print(f"not running ({exc})")
        sys.exit(0)
    if cmd == "status":
        print(json.dumps({"up": is_up(), "port": PORT, "data": data_dir()}))
        sys.exit(0)
    sys.exit(run())
