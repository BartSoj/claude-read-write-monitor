#!/usr/bin/env python3
"""read-write-monitor: hook recorder.

Reads one hook payload on stdin and appends one compact JSON record to
<data>/sessions/<session_id>/events.jsonl.

Never stores file content. Only paths, line numbers, counts and timings.

Wired to: SessionStart, SessionEnd, PreCompact, InstructionsLoaded,
and PostToolUse(Read|Edit|Write). See docs/EVENTS.md for the record schema.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import time

SCHEMA_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- storage

def data_dir() -> str:
    d = os.environ.get("RWM_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        d = os.path.expanduser("~/.claude/plugins/data/read-write-monitor")
    return d


def session_dir(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in session_id or "unknown")
    return os.path.join(data_dir(), "sessions", safe)


def append(session_id: str, rec: dict) -> None:
    """Append one record under an exclusive lock, stamping a monotonic seq.

    Hooks for parallel tool calls run as parallel processes, so the lock is
    what gives `seq` a total order. `seq` order is hook-completion order,
    which is the order edits actually landed on disk.
    """
    sd = session_dir(session_id)
    os.makedirs(sd, exist_ok=True)
    lock_path = os.path.join(sd, ".lock")
    seq_path = os.path.join(sd, "seq")
    with open(lock_path, "a+") as lf:
        try:
            import fcntl
            fcntl.flock(lf, fcntl.LOCK_EX)
        except Exception:
            pass  # best effort on platforms without flock
        try:
            n = int(open(seq_path).read().strip())
        except Exception:
            n = 0
        n += 1
        rec["seq"] = n
        with open(os.path.join(sd, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
        with open(seq_path, "w") as f:
            f.write(str(n))


def write_meta(session_id: str, patch: dict) -> None:
    sd = session_dir(session_id)
    os.makedirs(sd, exist_ok=True)
    path = os.path.join(sd, "meta.json")
    try:
        meta = json.load(open(path))
    except Exception:
        meta = {}
    meta.update(patch)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- line math

def line_count(text: str | None) -> int:
    """Line count using Claude Code's convention: a trailing newline opens a
    final empty line, so "a\\nb\\n" is 3 lines. Matches Read's `totalLines`."""
    if not text:
        return 0
    return text.count("\n") + 1


def edit_spans(original: str, old: str, new: str, replace_all: bool) -> list[dict]:
    """Exact line spans for an Edit, computed from string offsets.

    Returns [{"old": [s, e], "new": [s, e]}] with 1-based inclusive bounds in
    the pre-edit and post-edit files respectively. An empty span is encoded as
    e < s (an insertion or deletion point).
    """
    spans: list[dict] = []
    shift = 0
    pos = 0
    while True:
        idx = original.find(old, pos)
        if idx < 0:
            break
        start = original.count("\n", 0, idx) + 1
        old_end = start + old.count("\n")
        new_start = start + shift
        new_end = new_start + new.count("\n")
        spans.append({"old": [start, old_end], "new": [new_start, new_end]})
        shift += new.count("\n") - old.count("\n")
        pos = idx + max(len(old), 1)
        if not replace_all:
            break
    return spans


def hunk_spans(patch: list | None) -> list[dict]:
    """Fallback line spans from `structuredPatch`. Wider than the true edit:
    hunks carry three lines of surrounding context."""
    out: list[dict] = []
    for h in patch or []:
        try:
            os_, ol = int(h["oldStart"]), int(h.get("oldLines", 0))
            ns_, nl = int(h["newStart"]), int(h.get("newLines", 0))
        except Exception:
            continue
        out.append({"old": [os_, os_ + ol - 1], "new": [ns_, ns_ + nl - 1]})
    return out


def diff_spans(before: str, after: str) -> list[dict]:
    """Line spans for a whole-file rewrite, from a line-level diff."""
    a = before.split("\n")
    b = after.split("\n")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append({"old": [i1 + 1, i2], "new": [j1 + 1, j2]})
    return out


def touched(spans: list[dict], key: str) -> int:
    return sum(max(0, s[key][1] - s[key][0] + 1) for s in spans)


# ---------------------------------------------------------------- records

def base_record(d: dict, kind: str) -> dict:
    rec = {
        "v": SCHEMA_VERSION,
        "kind": kind,
        "ts": int(time.time() * 1000),
        "prompt_id": d.get("prompt_id"),
        "agent_id": d.get("agent_id"),
        "agent_type": d.get("agent_type"),
    }
    eff = d.get("effort")
    if isinstance(eff, dict):
        rec["effort"] = eff.get("level")
    return {k: v for k, v in rec.items() if v is not None}


def on_read(d: dict, rec: dict) -> dict | None:
    f = (d.get("tool_response") or {}).get("file") or {}
    start = f.get("startLine")
    n = f.get("numLines")
    path = f.get("filePath") or (d.get("tool_input") or {}).get("file_path")
    if not path or start is None or n is None:
        return None
    rec.update({
        "path": path,
        "start": int(start),
        "end": int(start) + int(n) - 1,
        "total_lines": f.get("totalLines"),
        "chars": len(f.get("content") or ""),
        "tool_use_id": d.get("tool_use_id"),
        "duration_ms": d.get("duration_ms"),
    })
    return rec


def on_edit(d: dict, rec: dict) -> dict | None:
    ti = d.get("tool_input") or {}
    tr = d.get("tool_response") or {}
    path = tr.get("filePath") or ti.get("file_path")
    if not path:
        return None
    original = tr.get("originalFile")
    old = tr.get("oldString", ti.get("old_string"))
    new = tr.get("newString", ti.get("new_string"))
    replace_all = bool(tr.get("replaceAll", ti.get("replace_all", False)))

    spans: list[dict] = []
    precision = "hunk"
    if isinstance(original, str) and isinstance(old, str) and isinstance(new, str) and old in original:
        spans = edit_spans(original, old, new, replace_all)
        precision = "exact"
    if not spans:
        spans = hunk_spans(tr.get("structuredPatch"))
        precision = "hunk"

    before = line_count(original) if isinstance(original, str) else None
    delta = sum((s["new"][1] - s["new"][0]) - (s["old"][1] - s["old"][0]) for s in spans)
    rec.update({
        "path": path,
        "spans": spans,
        "precision": precision,
        "replace_all": replace_all,
        "lines_written": touched(spans, "new"),
        "delta": delta,
        "total_lines_before": before,
        "total_lines_after": (before + delta) if before is not None else None,
        "tool_use_id": d.get("tool_use_id"),
        "duration_ms": d.get("duration_ms"),
    })
    return {k: v for k, v in rec.items() if v is not None}


def on_write(d: dict, rec: dict) -> dict | None:
    ti = d.get("tool_input") or {}
    tr = d.get("tool_response") or {}
    path = tr.get("filePath") or ti.get("file_path")
    if not path:
        return None
    content = tr.get("content", ti.get("content")) or ""
    original = tr.get("originalFile")
    mode = tr.get("type") or ("update" if isinstance(original, str) else "create")
    total_after = line_count(content)

    spans: list[dict] = []
    precision = "exact"
    barrier = False
    if mode == "create" or not isinstance(original, str):
        # Whole file replaced with no visible predecessor: line numbers recorded
        # before this point can no longer be projected forward.
        spans = [{"old": [1, line_count(original) if isinstance(original, str) else 0],
                  "new": [1, total_after]}]
        barrier = mode != "create"
    else:
        spans = diff_spans(original, content)

    rec.update({
        "path": path,
        "mode": mode,
        "spans": spans,
        "precision": precision,
        "barrier": barrier or None,
        "lines_written": touched(spans, "new"),
        "total_lines_before": line_count(original) if isinstance(original, str) else None,
        "total_lines_after": total_after,
        "chars": len(content),
        "tool_use_id": d.get("tool_use_id"),
        "duration_ms": d.get("duration_ms"),
    })
    return {k: v for k, v in rec.items() if v is not None}


def on_instructions(d: dict, rec: dict) -> dict | None:
    path = d.get("file_path")
    if not path:
        return None
    total, chars = None, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        total, chars = line_count(text), len(text)
    except Exception:
        pass
    rec.update({
        "path": path,
        "memory_type": d.get("memory_type"),
        "load_reason": d.get("load_reason"),
        "trigger_file_path": d.get("trigger_file_path"),
        "parent_file_path": d.get("parent_file_path"),
        "total_lines": total,
        "chars": chars,
    })
    return {k: v for k, v in rec.items() if v is not None}


# ---------------------------------------------------------------- server

def ensure_server() -> None:
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, "serve.py"), "ensure"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ).wait(timeout=10)
    except Exception:
        pass


def viewer_url(session_id: str) -> str:
    port = os.environ.get("RWM_PORT", "7788")
    return f"http://127.0.0.1:{port}/s/{session_id}"


# ---------------------------------------------------------------- entry

def main() -> int:
    try:
        d = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    sid = d.get("session_id")
    if not sid:
        return 0
    event = d.get("hook_event_name")
    out: dict = {}

    if event == "SessionStart":
        source = d.get("source")
        kind = "compact" if source == "compact" else "session"
        rec = base_record(d, kind)
        if kind == "compact":
            rec["phase"] = "post"
        else:
            rec.update({"phase": "start", "source": source, "cwd": d.get("cwd"),
                        "model": d.get("model")})
        append(sid, {k: v for k, v in rec.items() if v is not None})
        write_meta(sid, {"session_id": sid, "cwd": d.get("cwd"),
                         "started_at": int(time.time() * 1000),
                         "source": source, "model": d.get("model"),
                         "title": d.get("session_title"), "closed": False})
        ensure_server()
        out = {"systemMessage": f"read/write monitor → {viewer_url(sid)}"}

    elif event == "SessionEnd":
        rec = base_record(d, "session")
        rec.update({"phase": "end", "reason": d.get("reason")})
        append(sid, rec)
        write_meta(sid, {"closed": True, "ended_at": int(time.time() * 1000)})

    elif event == "PreCompact":
        rec = base_record(d, "compact")
        rec.update({"phase": "pre", "trigger": d.get("trigger")})
        append(sid, rec)

    elif event == "InstructionsLoaded":
        rec = on_instructions(d, base_record(d, "instructions"))
        if rec:
            append(sid, rec)

    elif event == "PostToolUse":
        tool = d.get("tool_name")
        handler = {"Read": on_read, "Edit": on_edit, "Write": on_write}.get(tool)
        if handler:
            rec = handler(d, base_record(d, tool.lower()))
            if rec:
                append(sid, rec)

    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A monitoring hook must never disturb the session.
        sys.exit(0)
