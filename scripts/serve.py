#!/usr/bin/env python3
"""read-write-monitor: local dashboard server.

One shared server for every session on this machine. It is a reader of the event
log — hooks never talk to it, they only append files. That keeps the hook path
fast and lets the dashboard be rewritten without touching the recorder.

Live updates are pushed, not polled: a stream endpoint tails a session's log and
sends new records the moment they land, and a follow endpoint tails the
machine-wide start log so a page can switch to a new session as it starts.

  serve.py ensure   start it if it isn't already listening (called by SessionStart)
  serve.py run      run in the foreground
  serve.py stop     shut it down
  serve.py export <session_id> [file.json]   write one session as a replay file
  serve.py import <file.json> [--force]      store a replay file as a session on this machine

Env: RWM_PORT (default 7788), RWM_DATA_DIR, RWM_IDLE_MINUTES (default 120),
RWM_TREE_MAX, RWM_IGNORE, RWM_INSTRUCTION_FILES, RWM_THEME, RWM_FOLLOW_RECENT_MINUTES.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tree  # noqa: E402

WEB = os.path.join(os.path.dirname(HERE), "web")
PORT = int(os.environ.get("RWM_PORT", "7788"))
IDLE_SECONDS = int(os.environ.get("RWM_IDLE_MINUTES", "120")) * 60
# Bumped when the viewer needs endpoints an older server lacks; `ensure` replaces
# an older server on the same data directory instead of leaving it serving stale code.
API_VERSION = 2
# How often a stream checks its file. A stat is microseconds; this bounds the
# server's share of tool-call-to-highlight latency.
TICK = 0.04
PING_SECONDS = 15

DEFAULT_INSTRUCTION_FILES = "CLAUDE.md,CLAUDE.local.md,AGENTS.md,GEMINI.md,.claude/rules/**,.cursor/rules/**,.github/copilot-instructions.md,SKILL.md"

_last_request = time.time()


def touch() -> None:
    global _last_request
    _last_request = time.time()


def data_dir() -> str:
    d = os.environ.get("RWM_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        d = os.path.expanduser("~/.claude/plugins/data/read-write-monitor")
    return d


def sessions_dir() -> str:
    return os.path.join(data_dir(), "sessions")


# ------------------------------------------------------------------ sessions
# Thousands of sessions accumulate. meta.json is parsed once per change, not per request.

_meta_cache: dict[str, tuple[float, dict]] = {}
_git_root_cache: dict[str, str] = {}


def session_meta(name: str) -> dict:
    path = os.path.join(sessions_dir(), name, "meta.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _meta_cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        meta = json.load(open(path))
    except Exception:
        meta = {}
    _meta_cache[name] = (mtime, meta)
    return meta


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


def project_of(meta: dict) -> str | None:
    p = meta.get("project")
    if p:
        return p
    cwd = meta.get("cwd")
    return git_root(cwd) or cwd


def list_sessions(project: str | None = None) -> list[dict]:
    root = sessions_dir()
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        ev = os.path.join(root, name, "events.jsonl")
        try:
            st = os.stat(ev)
        except OSError:
            continue
        meta = session_meta(name)
        proj = project_of(meta) or meta.get("cwd")
        if project and proj != project:
            continue
        out.append({
            "id": name,
            "cwd": meta.get("cwd"),
            "project": proj,
            "title": meta.get("title"),
            "label": meta.get("label"),
            "closed": bool(meta.get("closed")),
            "started_at": meta.get("started_at"),
            "updated_at": int(st.st_mtime * 1000),
            "bytes": st.st_size,
        })
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def read_chunk(session_id: str, offset: int) -> tuple[list[dict], int]:
    """Whole records from `offset` on, and the offset after the last whole line."""
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
    return events, next_offset


def read_events(session_id: str, offset: int) -> dict:
    events, next_offset = read_chunk(session_id, offset)
    return {"offset": next_offset, "events": events, "meta": session_meta(session_id)}


def list_projects() -> list[dict]:
    out: dict[str, dict] = {}
    for s in list_sessions():
        proj = s["project"] or "unknown"
        e = out.setdefault(proj, {"project": proj, "name": os.path.basename(proj) or proj,
                                  "sessions": 0, "active": 0, "updated_at": 0})
        e["sessions"] += 1
        e["active"] += 0 if s["closed"] else 1
        e["updated_at"] = max(e["updated_at"], s["updated_at"])
    return sorted(out.values(), key=lambda p: p["updated_at"], reverse=True)


# ------------------------------------------------------------------ file tree

_tree_cache: dict[str, tuple[float, dict]] = {}
TREE_TTL = 5.0


def file_tree(root: str) -> dict:
    """The static layout the Tree view draws, from git when there is one and a
    filesystem walk when there is not. Cached briefly: a page load and the
    project rollup ask for it together."""
    hit = _tree_cache.get(root)
    if hit and time.time() - hit[0] < TREE_TTL:
        return hit[1]
    if os.path.isdir(root):
        result = tree.list_tree(root)
    else:
        result = {"root": root, "source": "missing", "files": [], "total": 0,
                  "complete": True, "truncated": False, "omitted": {}}
    result["real"] = os.path.realpath(root)
    if len(_tree_cache) > 32:
        _tree_cache.clear()
    _tree_cache[root] = (time.time(), result)
    return result


# ------------------------------------------------------------------ aggregation
# Cross-session rollups. Aggregating here rather than in the browser keeps every
# event of every session off the wire; the viewer only ever sees the totals.

_agg_cache: dict[str, tuple[tuple, dict]] = {}
COUNTED = ("read", "instructions", "edit", "write")


def aggregate(project: str) -> dict:
    sessions_in = list_sessions(project)
    stamp = (len(sessions_in), max((s["updated_at"] for s in sessions_in), default=0))
    hit = _agg_cache.get(project)
    if hit and hit[0] == stamp:
        return hit[1]

    files: dict[str, dict] = {}
    sessions: list[dict] = []
    lanes = {"main": dict.fromkeys(
        ("reads", "read_lines", "read_chars", "writes", "write_lines"), 0)}
    lanes["sub"] = dict(lanes["main"])
    instr = {"loads": 0, "chars": 0}
    starts = 0
    shell_reads = 0
    for s in sessions_in:
        sessions.append({"id": s["id"], "closed": s["closed"], "label": s.get("label"),
                         "started_at": s.get("started_at"), "updated_at": s["updated_at"]})
        last_instr: dict[str, int] = {}
        for e in read_chunk(s["id"], 0)[0]:
            kind = e.get("kind")
            if kind == "session" and e.get("phase") == "start":
                starts += 1
            if kind not in COUNTED:
                continue
            # Claude Code fires InstructionsLoaded twice for the same file within a
            # second at session start. Collapse the duplicate rather than bill it twice.
            if kind == "instructions":
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
            if kind in ("read", "instructions"):
                lines = (max(0, e.get("end", 0) - e.get("start", 0) + 1) if kind == "read"
                         else e.get("total_lines") or 0)
                f["reads"] += 1
                f["read_chars"] += e.get("chars") or 0
                f["read_lines"] += lines
                lane["reads"] += 1
                lane["read_chars"] += e.get("chars") or 0
                lane["read_lines"] += lines
                if e.get("source") == "bash":
                    shell_reads += 1
                if kind == "instructions":
                    f["instructions"] = True
                    instr["loads"] += 1
                    instr["chars"] += e.get("chars") or 0
            else:
                f["writes"] += 1
                f["write_lines"] += e.get("lines_written") or 0
                lane["writes"] += 1
                lane["write_lines"] += e.get("lines_written") or 0

    real = os.path.realpath(project)
    for f in files.values():
        f["sessions"] = len(f["sessions"])
        p = f["path"]
        base = project if p.startswith(project + "/") else real if p.startswith(real + "/") else None
        f["rel"] = os.path.relpath(p, base) if base else p

    listing = file_tree(project)
    universe = listing["files"]
    universe_set = set(universe)
    touched_rel = {f["rel"] for f in files.values()}
    untouched = [p for p in universe if p not in touched_rel]

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

    inside = lambda rel: not rel.startswith("..") and not rel.startswith("/")
    for f in files.values():
        if inside(f["rel"]):
            bump(f["rel"], files_touched=1, reads=f["reads"], read_lines=f["read_lines"],
                 read_chars=f["read_chars"], writes=f["writes"], write_lines=f["write_lines"])
    for rel in universe:
        bump(rel, files_total=1)
    for rel in touched_rel:
        if rel not in universe_set and inside(rel):
            bump(rel, files_total=1)   # touched but not listed still counts in the denominator

    result = {
        "project": project,
        "sessions": sessions,
        "files": sorted(files.values(), key=lambda f: f["read_chars"], reverse=True),
        "folders": sorted(folders.values(), key=lambda d: d["read_chars"], reverse=True),
        "lanes": lanes,
        "instructions": instr,
        "shell_reads": shell_reads,
        # A single session_id can span several resume cycles, each of which reloads
        # instructions into a fresh context. This is the honest denominator.
        "windows": starts,
        "universe": listing["total"],
        "universe_source": listing["source"],
        "untouched": untouched[:400],
        "untouched_total": len(untouched) + max(0, listing["total"] - len(universe)),
        "outside": [f["rel"] for f in files.values() if not inside(f["rel"])][:100],
    }
    if len(_agg_cache) > 16:
        _agg_cache.clear()
    _agg_cache[project] = (stamp, result)
    return result


# ------------------------------------------------------------------ replay files
# A session travels as one JSON bundle: its meta and its records, exactly as logged.
# Paths and line numbers only, like the log itself.

BUNDLE_FORMAT = "rwm-session"
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def export_bundle(session_id: str) -> dict | None:
    if not SAFE_ID.match(session_id or ""):
        return None
    if not os.path.exists(os.path.join(sessions_dir(), session_id, "events.jsonl")):
        return None
    events, _ = read_chunk(session_id, 0)
    return {"format": BUNDLE_FORMAT, "v": 1, "id": session_id,
            "meta": session_meta(session_id), "events": events}


def import_bundle(bundle: dict, force: bool = False) -> str:
    """Store a bundle as a session on this machine, so it is listed and replayable here."""
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError("not a read-write-monitor session file")
    sid = str(bundle.get("id") or "")
    if not SAFE_ID.match(sid):
        raise ValueError("the file's session id is not usable as a directory name")
    events = bundle.get("events")
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        raise ValueError("the file holds no records")
    sd = os.path.join(sessions_dir(), sid)
    if os.path.exists(os.path.join(sd, "events.jsonl")) and not force:
        raise FileExistsError(f"session {sid} already exists here (use --force to replace it)")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n")
    meta = dict(bundle.get("meta") or {})
    meta.update({"session_id": sid, "closed": True, "imported_at": int(time.time() * 1000)})
    with open(os.path.join(sd, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    with open(os.path.join(sd, "seq"), "w") as f:
        f.write(str(max([int(e.get("seq") or 0) for e in events] + [0])))
    return sid


# ------------------------------------------------------------------ expectation sets
# The files a task should read and the files it should change, written by a person and kept
# on this machine, outside every project, so an agent at work never sees the answer key.
#
#   # expectation set: new-guide
#   root: /abs/path/to/project
#   read needs/jobs.md
#   read methods/*.md          (a glob is satisfied by any one match)
#   write guides/*.md

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def expectations_dir() -> str:
    return os.path.join(data_dir(), "expectations")


def _clean_pattern(p) -> str | None:
    p = str(p).strip()
    if not p or "\n" in p or p.startswith("/") or p.startswith("~") or ".." in p.split("/"):
        return None
    return p


def parse_expectations(text: str, name: str) -> dict:
    root, reads, writes, note = None, [], [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            if body and not body.lower().startswith("expectation set:"):
                note.append(body)
            continue
        low = line.lower()
        if low.startswith("root:"):
            root = line[5:].strip() or None
            continue
        bucket = reads
        if low.startswith("write "):
            bucket, line = writes, line[6:]
        elif low.startswith("read "):
            line = line[5:]
        p = _clean_pattern(line)
        if p and p not in bucket:
            bucket.append(p)
    return {"name": name, "root": root, "reads": reads, "writes": writes, "note": " ".join(note)[:400]}


def format_expectations(s: dict) -> str:
    lines = [f"# expectation set: {s['name']}"]
    if s.get("note"):
        lines.append(f"# {s['note']}")
    if s.get("root"):
        lines.append(f"root: {s['root']}")
    lines += [f"read {p}" for p in s.get("reads") or []]
    lines += [f"write {p}" for p in s.get("writes") or []]
    return "\n".join(lines) + "\n"


def list_expectations(root: str | None = None) -> list[dict]:
    try:
        names = sorted(n for n in os.listdir(expectations_dir()) if n.endswith(".txt"))
    except OSError:
        return []
    out = []
    target = norm(root) if root else None
    for n in names:
        try:
            with open(os.path.join(expectations_dir(), n), encoding="utf-8") as f:
                s = parse_expectations(f.read(65536), n[:-4])
        except OSError:
            continue
        if target and s["root"] and norm(s["root"]) != target:
            continue
        out.append(s)
    return out


def save_expectations(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    name = str(payload.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("name: letters, digits, dot, dash or underscore, up to 64 characters")
    s = {"name": name, "root": str(payload.get("root") or "").strip() or None,
         "note": str(payload.get("note") or "").strip().replace("\n", " ")[:400],
         "reads": [], "writes": []}
    for key in ("reads", "writes"):
        for p in payload.get(key) or []:
            c = _clean_pattern(p)
            if c is None:
                raise ValueError(f"not a project-relative path: {p!r}")
            if c not in s[key]:
                s[key].append(c)
    os.makedirs(expectations_dir(), exist_ok=True)
    path = os.path.join(expectations_dir(), name + ".txt")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(format_expectations(s))
    os.replace(path + ".tmp", path)
    return s


# ------------------------------------------------------------------ follow

def norm(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path)).rstrip("/") or "/"


def belongs(meta: dict, target: str) -> bool:
    for p in (meta.get("cwd"), meta.get("project")):
        if p:
            p = norm(p)
            if p == target or p.startswith(target + "/"):
                return True
    return False


def config() -> dict:
    return {
        "instruction_files": [p.strip() for p in os.environ.get(
            "RWM_INSTRUCTION_FILES", DEFAULT_INSTRUCTION_FILES).split(",") if p.strip()],
        "theme": os.environ.get("RWM_THEME") or None,
        "home": os.path.expanduser("~"),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence stderr chatter
        pass

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
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

    # -- server-sent events. The body is close-delimited; each stream holds one thread.

    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, obj) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

    def _ping(self, last: float) -> float:
        if time.time() - last < PING_SECONDS:
            return last
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()
        touch()  # an open dashboard keeps the server alive
        return time.time()

    def stream(self, sid: str, offset: int) -> None:
        """Tail one session's log. Sends `events` batches and `meta` changes."""
        path = os.path.join(sessions_dir(), sid, "events.jsonl")
        meta_path = os.path.join(sessions_dir(), sid, "meta.json")
        self._sse_open()
        last_ping, size, meta_mtime = time.time(), -1, -1.0
        try:
            self._sse("hello", {"sid": sid, "server_ms": int(time.time() * 1000)})
            while True:
                try:
                    cur = os.path.getsize(path)
                except OSError:
                    cur = 0
                if cur != size:
                    size = cur
                    events, nxt = read_chunk(sid, offset)
                    if events or nxt != offset:
                        offset = nxt
                        self._sse("events", {"offset": offset, "events": events,
                                             "server_ms": int(time.time() * 1000)})
                    if nxt < cur:
                        size = -1  # a partial line is still being written
                try:
                    mt = os.path.getmtime(meta_path)
                except OSError:
                    mt = 0.0
                if mt != meta_mtime:
                    meta_mtime = mt
                    self._sse("meta", session_meta(sid))
                last_ping = self._ping(last_ping)
                time.sleep(TICK)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def follow(self, target: str) -> None:
        """Announce the newest session under `target` now, and every new one as it starts."""
        starts = os.path.join(data_dir(), "starts.jsonl")
        self._sse_open()
        recent_ms = int(os.environ.get("RWM_FOLLOW_RECENT_MINUTES", "30")) * 60_000
        now = int(time.time() * 1000)
        try:
            pos = os.path.getsize(starts)
        except OSError:
            pos = 0
        last_ping = time.time()
        try:
            current = next((s for s in list_sessions()
                            if belongs(s, target) and not s["closed"]
                            and now - s["updated_at"] < recent_ms), None)
            if current:
                self._sse("session", {"id": current["id"], "cwd": current["cwd"], "initial": True})
            else:
                self._sse("waiting", {"project": target})
            while True:
                try:
                    size = os.path.getsize(starts)
                except OSError:
                    size = 0
                if size < pos:
                    pos = 0
                if size > pos:
                    with open(starts, "rb") as f:
                        f.seek(pos)
                        blob = f.read()
                    cut = blob.rfind(b"\n")
                    if cut >= 0:
                        pos += cut + 1
                        for line in blob[:cut].split(b"\n"):
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue
                            if belongs(rec, target):
                                self._sse("session", {"id": rec.get("id"), "cwd": rec.get("cwd"),
                                                      "source": rec.get("source")})
                last_ping = self._ping(last_ping)
                time.sleep(TICK)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_GET(self):
        touch()
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        arg = lambda k, d="": q.get(k, [d])[0]

        if p in ("/", "/index.html", "/viewer.html") or p.startswith("/s/"):
            return self._file("viewer.html")
        if p == "/health":
            return self._json({"ok": True, "port": PORT, "data": data_dir(),
                               "api": API_VERSION, "code": HERE, "pid": os.getpid()})
        if p == "/api/config":
            return self._json(config())
        if p == "/api/sessions":
            out = list_sessions(arg("project") or None)
            try:
                limit = int(arg("limit", "0"))
            except ValueError:
                limit = 0
            return self._json({"sessions": out[:limit] if limit > 0 else out, "total": len(out)})
        if p == "/api/projects":
            return self._json({"projects": list_projects()})
        if p == "/api/tree":
            root = arg("root")
            if not root:
                return self._send(400, b"root required", "text/plain")
            return self._json(file_tree(norm(root) if root.startswith("~") else root))
        if p == "/api/aggregate":
            proj = arg("project")
            if not proj:
                return self._send(400, b"project required", "text/plain")
            return self._json(aggregate(proj))
        if p == "/api/expectations":
            return self._json({"dir": expectations_dir(), "sets": list_expectations(arg("root") or None)})
        if p.startswith("/api/export/"):
            sid = unquote(p[len("/api/export/"):]).strip("/")
            bundle = export_bundle(sid)
            if not bundle:
                return self._send(404, b"no such session", "text/plain")
            label = re.sub(r"[^A-Za-z0-9]+", "-", str(bundle["meta"].get("label") or "")).strip("-")[:40].lower()
            fname = f"rwm-{label + '-' if label else ''}{sid[:8]}.json"
            return self._send(200, json.dumps(bundle).encode(), "application/json; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{fname}"'})
        if p.startswith("/api/events/"):
            sid = unquote(p[len("/api/events/"):]).strip("/")
            try:
                offset = int(arg("offset", "0"))
            except ValueError:
                offset = 0
            return self._json(read_events(sid, offset))
        if p.startswith("/api/stream/"):
            sid = unquote(p[len("/api/stream/"):]).strip("/")
            try:
                offset = int(arg("offset", "0"))
            except ValueError:
                offset = 0
            return self.stream(sid, offset)
        if p == "/api/follow":
            target = arg("project")
            if not target:
                return self._send(400, b"project required", "text/plain")
            return self.follow(norm(target))
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        touch()
        if urlparse(self.path).path != "/api/expectations":
            return self._send(404, b"not found", "text/plain")
        # A page on another origin cannot set a custom header on a simple request, so this
        # keeps a stray form post from writing into the data directory.
        if self.headers.get("X-RWM") != "1":
            return self._send(403, b"missing X-RWM header", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 65536:
            return self._send(413, b"body must be 1 byte to 64 KB", "text/plain")
        try:
            saved = save_expectations(json.loads(self.rfile.read(n)))
        except (ValueError, TypeError) as exc:
            return self._send(400, str(exc).encode(), "text/plain; charset=utf-8")
        return self._json(saved)


def _idle_watchdog() -> None:
    while True:
        time.sleep(60)
        newest = 0.0
        for path in (os.path.join(data_dir(), "starts.jsonl"),):
            try:
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                pass
        try:
            for name in os.listdir(sessions_dir()):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(sessions_dir(), name, "events.jsonl")))
                except OSError:
                    pass
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
    # Bind before claiming the pid file: a start that loses the port to a running server must not
    # overwrite that server's pid, or nothing can stop it later.
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    with open(pid_path(), "w") as f:
        f.write(str(os.getpid()))
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    # Parse every session's metadata once up front, so the first page load after a start
    # does not pay for thousands of meta.json files.
    threading.Thread(target=list_sessions, daemon=True).start()
    srv.serve_forever()
    return 0


def _listening_pid() -> int | None:
    """The process listening on PORT, when it is one of ours: servers from before the pid moved into
    /health left only a pid file, which an earlier failed start may have overwritten."""
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=3).stdout.split()
        for token in out:
            pid = int(token)
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout
            if "serve.py" in cmd:
                return pid
    except Exception:
        pass
    return None


def stop() -> bool:
    candidates = []
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
            candidates.append(int(json.load(r)["pid"]))
    except Exception:
        pass
    try:
        candidates.append(int(open(pid_path()).read().strip()))
    except Exception:
        pass
    listening = _listening_pid()
    if listening:
        candidates.append(listening)
    for pid in dict.fromkeys(candidates):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        for _ in range(30):
            if not is_up():
                return True
            time.sleep(0.1)
    return not is_up()


def ensure() -> int:
    if is_up():
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                health = json.load(r)
        except Exception:
            return 0
        running = health.get("data")
        # A server started by hand, or by a differently-configured install, holds the
        # port and would silently serve a different data directory than the one this
        # session records into. Say so rather than appear to work.
        if running and os.path.realpath(running) != os.path.realpath(data_dir()):
            sys.stderr.write(
                f"read-write-monitor: port {PORT} is served by another instance reading "
                f"{running}, not {data_dir()}. Stop it (serve.py stop) or set RWM_PORT.\n")
            return 1
        # Same data, older code: a plugin update left the previous server running.
        if int(health.get("api") or 1) >= API_VERSION or not stop():
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
        print("stopped" if stop() else "not running")
        sys.exit(0)
    if cmd == "status":
        print(json.dumps({"up": is_up(), "port": PORT, "data": data_dir()}))
        sys.exit(0)
    if cmd == "export" and len(sys.argv) > 2:
        bundle = export_bundle(sys.argv[2])
        if not bundle:
            sys.exit(f"no session {sys.argv[2]} in {sessions_dir()}")
        text = json.dumps(bundle)
        if len(sys.argv) > 3:
            with open(sys.argv[3], "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {sys.argv[3]}: {len(bundle['events'])} records")
        else:
            print(text)
        sys.exit(0)
    if cmd == "import" and len(sys.argv) > 2:
        try:
            with open(sys.argv[2], encoding="utf-8") as f:
                sid = import_bundle(json.load(f), force="--force" in sys.argv)
        except (OSError, ValueError, FileExistsError) as exc:
            sys.exit(f"import failed: {exc}")
        print(f"imported {sid} into {sessions_dir()}")
        sys.exit(0)
    sys.exit(run())
