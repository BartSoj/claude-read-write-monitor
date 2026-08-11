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


# ------------------------------------------------------------------ aggregation
# Cross-session rollups. Aggregating here rather than in the browser keeps every
# event of every session off the wire; the viewer only ever sees the totals.

_git_root_cache: dict[str, str] = {}
_agg_cache: dict[str, tuple[float, dict]] = {}


def git_root(path: str | None) -> str | None:
    if not path:
        return None
    if path in _git_root_cache:
        return _git_root_cache[path] or None
    root = ""
    if os.path.isdir(path):
        try:
            out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=3)
            root = out.stdout.strip()
        except Exception:
            root = ""
    _git_root_cache[path] = root
    return root or None


def tracked_files(project: str) -> list[str]:
    """The file universe, so 'never touched' is answerable. Tracked files only —
    an untracked build artifact nobody reads is not a finding."""
    try:
        out = subprocess.run(["git", "-C", project, "ls-files", "-z"],
                             capture_output=True, text=True, timeout=20)
        return [p for p in out.stdout.split("\0") if p]
    except Exception:
        return []


def _session_meta(name: str) -> dict:
    try:
        return json.load(open(os.path.join(sessions_dir(), name, "meta.json")))
    except Exception:
        return {}


def project_of(meta: dict) -> str | None:
    p = meta.get("project")
    if p:
        return p
    cwd = meta.get("cwd")
    return git_root(cwd) or cwd


def _newest_mtime() -> float:
    newest = 0.0
    try:
        for name in os.listdir(sessions_dir()):
            ev = os.path.join(sessions_dir(), name, "events.jsonl")
            try:
                newest = max(newest, os.path.getmtime(ev))
            except OSError:
                pass
    except OSError:
        pass
    return newest


def list_projects() -> list[dict]:
    out: dict[str, dict] = {}
    for s in list_sessions():
        proj = project_of(_session_meta(s["id"])) or s.get("cwd") or "unknown"
        e = out.setdefault(proj, {"project": proj, "name": os.path.basename(proj) or proj,
                                  "sessions": 0, "active": 0, "updated_at": 0})
        e["sessions"] += 1
        e["active"] += 0 if s["closed"] else 1
        e["updated_at"] = max(e["updated_at"], s["updated_at"])
    return sorted(out.values(), key=lambda p: p["updated_at"], reverse=True)


def aggregate(project: str) -> dict:
    key = project
    stamp = _newest_mtime()
    hit = _agg_cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]

    files: dict[str, dict] = {}
    sessions: list[dict] = []
    lanes = {"main": dict.fromkeys(
        ("reads", "read_lines", "read_chars", "writes", "write_lines"), 0)}
    lanes["sub"] = dict(lanes["main"])
    instr = {"loads": 0, "chars": 0}
    starts = 0
    for s in list_sessions():
        meta = _session_meta(s["id"])
        if (project_of(meta) or s.get("cwd")) != project:
            continue
        sessions.append({"id": s["id"], "closed": s["closed"],
                         "started_at": meta.get("started_at"), "updated_at": s["updated_at"]})
        last_instr: dict[str, int] = {}
        for e in read_events(s["id"], 0)["events"]:
            if e.get("kind") == "session" and e.get("phase") == "start":
                starts += 1
            # Claude Code fires InstructionsLoaded twice for the same file within a
            # second at session start. Collapse the duplicate rather than bill it twice.
            if e.get("kind") == "instructions":
                prev = last_instr.get(e.get("path", ""))
                ts = e.get("ts") or 0
                if prev is not None and abs(ts - prev) < 5000:
                    continue
                last_instr[e.get("path", "")] = ts
            path = e.get("path")
            if not path:
                continue
            f = files.setdefault(path, {
                "path": path, "reads": 0, "read_lines": 0, "read_chars": 0,
                "writes": 0, "write_lines": 0, "total_lines": 0,
                "sessions": set(), "last_ts": 0, "instructions": False,
            })
            lane = lanes["sub" if e.get("agent_id") else "main"]
            f["sessions"].add(s["id"])
            f["last_ts"] = max(f["last_ts"], e.get("ts") or 0)
            if e.get("total_lines"):
                f["total_lines"] = max(f["total_lines"], e["total_lines"])
            if e.get("total_lines_after"):
                f["total_lines"] = max(f["total_lines"], e["total_lines_after"])
            kind = e.get("kind")
            if kind in ("read", "instructions"):
                lines = (max(0, e.get("end", 0) - e.get("start", 0) + 1) if kind == "read"
                         else e.get("total_lines") or 0)
                f["reads"] += 1
                f["read_chars"] += e.get("chars") or 0
                f["read_lines"] += lines
                lane["reads"] += 1
                lane["read_chars"] += e.get("chars") or 0
                lane["read_lines"] += lines
                if kind == "instructions":
                    f["instructions"] = True
                    instr["loads"] += 1
                    instr["chars"] += e.get("chars") or 0
            elif kind in ("edit", "write"):
                f["writes"] += 1
                f["write_lines"] += e.get("lines_written") or 0
                lane["writes"] += 1
                lane["write_lines"] += e.get("lines_written") or 0

    for f in files.values():
        f["sessions"] = len(f["sessions"])
        f["rel"] = os.path.relpath(f["path"], project) if f["path"].startswith(project) else f["path"]

    universe = tracked_files(project)
    touched_rel = {f["rel"] for f in files.values()}
    untouched = sorted(p for p in universe if p not in touched_rel)

    # Folder rollup: every directory prefix carries its whole subtree, so you can
    # read the table at any depth without re-summing children yourself.
    folders: dict[str, dict] = {}

    def bump(rel: str, **kw):
        parts = rel.split("/")[:-1]
        for i in range(len(parts) + 1):
            d = "/".join(parts[:i]) or "."
            e = folders.setdefault(d, {"dir": d, "depth": i, "files_total": 0, "files_touched": 0,
                                       "reads": 0, "read_lines": 0, "read_chars": 0,
                                       "writes": 0, "write_lines": 0})
            for k, v in kw.items():
                e[k] += v

    in_project = [f for f in files.values() if not f["rel"].startswith("..") and not f["rel"].startswith("/")]
    for f in in_project:
        bump(f["rel"], files_touched=1, reads=f["reads"], read_lines=f["read_lines"],
             read_chars=f["read_chars"], writes=f["writes"], write_lines=f["write_lines"])
    for rel in universe:
        bump(rel, files_total=1)
    for rel in touched_rel:
        if rel not in set(universe) and not rel.startswith(".."):
            bump(rel, files_total=1)   # touched but untracked still counts in the denominator

    result = {
        "project": project,
        "sessions": sorted(sessions, key=lambda s: s["updated_at"], reverse=True),
        "files": sorted(files.values(), key=lambda f: f["read_chars"], reverse=True),
        "folders": sorted(folders.values(), key=lambda d: d["read_chars"], reverse=True),
        "lanes": lanes,
        "instructions": instr,
        # A single session_id can span several resume cycles, each of which reloads
        # instructions into a fresh context. This is the honest denominator.
        "windows": starts,
        "universe": len(universe),
        "untouched": untouched[:400],
        "untouched_total": len(untouched),
        "outside": [f["rel"] for f in files.values() if f["rel"].startswith("..") or f["rel"].startswith("/")][:100],
    }
    _agg_cache.clear()
    _agg_cache[key] = (stamp, result)
    return result


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
            out = list_sessions()
            for s in out:
                s["project"] = project_of(_session_meta(s["id"]))
            return self._json({"sessions": out})
        if p == "/api/projects":
            return self._json({"projects": list_projects()})
        if p == "/api/aggregate":
            proj = q.get("project", [""])[0]
            if not proj:
                return self._send(400, b"project required", "text/plain")
            return self._json(aggregate(proj))
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
