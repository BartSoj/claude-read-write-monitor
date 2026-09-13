#!/usr/bin/env python3
"""read-write-monitor: hook recorder.

Reads one hook payload on stdin and appends compact JSON records to
<data>/sessions/<session_id>/events.jsonl.

Never stores file content. Only paths, line numbers, counts and timings.

Wired to: SessionStart, SessionEnd, PreCompact, InstructionsLoaded, UserPromptSubmit,
PreToolUse and PostToolUse (Read|Edit|Write|Grep|Glob|Bash). See SPEC.md for the schema.
"""

from __future__ import annotations

import json
import os
import sys
import time

SCHEMA_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))

# Paths listed by one search or glob. Names only, but a glob over a monorepo can
# return tens of thousands; the count is kept in `n` either way.
MAX_LISTED = 1000


# ---------------------------------------------------------------- storage

def data_dir() -> str:
    d = os.environ.get("RWM_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        d = os.path.expanduser("~/.claude/plugins/data/read-write-monitor")
    return d


def session_dir(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in session_id or "unknown")
    return os.path.join(data_dir(), "sessions", safe)


def append(session_id: str, recs: list[dict]) -> None:
    """Append records under an exclusive lock, stamping a monotonic seq.

    Hooks for parallel tool calls run as parallel processes, so the lock is
    what gives `seq` a total order. `seq` order is hook-completion order,
    which is the order edits actually landed on disk.
    """
    if not recs:
        return
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
        lines = []
        for rec in recs:
            n += 1
            rec["seq"] = n
            lines.append(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
        # One write call, so a reader tailing the file sees whole batches.
        with open(os.path.join(sd, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write("".join(lines))
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


def note_start(session_id: str, cwd: str | None, source: str | None) -> None:
    """One line per session start, machine-wide. The server tails this file so a
    follow page can switch to a new session the moment it starts, without
    rescanning thousands of session directories."""
    rec = {"id": session_id, "cwd": cwd, "source": source, "ts": int(time.time() * 1000)}
    with open(os.path.join(data_dir(), "starts.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")


def label_prompts() -> bool:
    return os.environ.get("RWM_LABEL_PROMPTS", "1").strip().lower() not in ("0", "false", "no", "off")


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
    import difflib  # only Write(update) needs it; keeps every other hook's startup lean
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


def absolute(path: str | None, cwd: str | None) -> str | None:
    if not path:
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


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


def clean(rec: dict) -> dict:
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
    return clean(rec)


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
    return clean(rec)


def listed_paths(tr: dict, cwd: str | None) -> tuple[list[str], int]:
    """Files named by a Grep or Glob result. `filenames` in files mode; in content
    and count mode the paths prefix each output line."""
    names = tr.get("filenames")
    if not isinstance(names, list):
        names = []
        seen = set()
        for line in str(tr.get("content") or "").split("\n"):
            head = line.split(":", 1)[0]
            if head and head not in seen:
                seen.add(head)
                names.append(head)
    out = []
    for p in names:
        ap = absolute(str(p), cwd)
        if ap:
            out.append(ap)
    n = tr.get("numFiles") if isinstance(tr.get("numFiles"), int) else len(out)
    return out[:MAX_LISTED], max(n, len(out))


def on_search(d: dict, rec: dict) -> dict | None:
    """Grep and Glob: which files the search named. Their text is not kept."""
    ti = d.get("tool_input") or {}
    tr = d.get("tool_response") or {}
    if not isinstance(tr, dict):
        return None
    cwd = d.get("cwd")
    files, n = listed_paths(tr, cwd)
    tool = d.get("tool_name")
    rec.update({
        "tool": tool,
        "mode": "list" if tool == "Glob" else "search",
        "pattern": ti.get("pattern"),
        "scope": absolute(ti.get("path"), cwd) or cwd,
        "files": files,
        "n": n,
        "files_only": tool == "Glob" or ti.get("output_mode", "files_with_matches") != "content",
        "tool_use_id": d.get("tool_use_id"),
        "duration_ms": d.get("duration_ms"),
    })
    return clean(rec)


def on_bash(d: dict, base: dict) -> list[dict]:
    """Reads and searches inferred from a shell command. A heuristic, and every
    record it produces says so with `source: "bash"`."""
    import shell_reads
    ti = d.get("tool_input") or {}
    tr = d.get("tool_response") or {}
    if not isinstance(tr, dict) or tr.get("interrupted"):
        return []
    stdout = tr.get("stdout") or ""
    cwd = d.get("cwd") or os.getcwd()
    out: list[dict] = []
    for act in shell_reads.parse(ti.get("command") or "", cwd)[:40]:
        rec = dict(base)
        rec.update({"source": "bash", "cmd": act.get("cmd"), "tool_use_id": d.get("tool_use_id")})
        if act["op"] == "write":
            # A redirect, tee, sed -i, cp/mv target or touch. The command says which file and how,
            # not which lines, so there are no spans; a heredoc's line count is kept when known.
            after = shell_reads.resolve_read({"op": "read", "cmd": "cat", "path": act.get("path"),
                                              "start": None, "end": None, "from_end": None})
            mode = act.get("mode")
            rec.update({"kind": "edit" if mode in ("append", "in-place") else "write",
                        "path": act.get("path"), "mode": mode, "precision": "shell",
                        "lines_written": act.get("lines"),
                        "total_lines_after": after.get("total_lines") if after else None})
            out.append(clean(rec))
            continue
        if act["op"] == "read":
            r = shell_reads.resolve_read(act)
            if not r:
                continue
            size = 0
            try:
                size = os.path.getsize(r["path"])
            except OSError:
                pass
            total = r.get("total_lines") or 0
            lines = max(0, r["end"] - r["start"] + 1)
            rec.update({"kind": "read", "path": r["path"], "start": r["start"], "end": r["end"],
                        "total_lines": total,
                        # Bytes, scaled to the range read. An estimate, and marked as one.
                        "chars": int(size * lines / total) if total else size,
                        "chars_est": True})
        else:
            files = shell_reads.hits(act, stdout, cwd, limit=MAX_LISTED)
            rec.update({"kind": "search", "tool": act.get("cmd"),
                        "mode": "list" if act["op"] == "list" else "search",
                        "pattern": act.get("pattern"), "scope": (act.get("scope") or [None])[0],
                        "files": files, "n": len(files),
                        "files_only": act["op"] == "list" or bool(act.get("files_only"))})
        out.append(clean(rec))

    # A command can change files the parser cannot name: a Python heredoc, a formatter, git.
    # Whatever changed on disk while it ran is a write too, recorded once.
    named = {r.get("path") for r in out if r.get("kind") in ("edit", "write")}
    for path in bash_changed_files(d):
        if path in named:
            continue
        after = shell_reads.resolve_read({"op": "read", "cmd": "cat", "path": path,
                                          "start": None, "end": None, "from_end": None})
        rec = dict(base)
        rec.update({"kind": "edit", "source": "bash", "cmd": "changed on disk", "path": path,
                    "mode": "changed", "precision": "mtime", "tool_use_id": d.get("tool_use_id"),
                    "total_lines_after": after.get("total_lines") if after else None})
        out.append(clean(rec))
    return out


# Time allowed between a Bash command finishing and its PostToolUse hook starting to look.
SCAN_SLACK_MS = 1500


def changed_since(root: str, rels: list[str], since_ms: int, until_ms: int) -> list[str]:
    """Files under `root` whose modification time falls in [since_ms, until_ms]."""
    out = []
    for rel in rels:
        path = os.path.normpath(os.path.join(root, rel))
        try:
            mtime = os.stat(path).st_mtime_ns // 1_000_000
        except OSError:
            continue
        if since_ms <= mtime <= until_ms:
            out.append(path)
    return out


def recent_writes(session_id: str, since_ms: int, tool_use_id: str | None) -> set[str]:
    """Paths this session already recorded as written since `since_ms` by another tool call."""
    path = os.path.join(session_dir(session_id), "events.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    seen = set()
    for line in tail:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if (e.get("kind") in ("edit", "write") and (e.get("ts") or 0) >= since_ms
                and e.get("tool_use_id") != tool_use_id):
            seen.add(e.get("path"))
    return seen


def bash_changed_files(d: dict) -> list[str]:
    """Project files a Bash command changed, whatever wrote them, found by modification time in
    the command's own window. Runs in the async PostToolUse hook, after the command has finished.
    Off with RWM_BASH_WRITE_SCAN=0; skipped when the project cannot be listed quickly and whole."""
    if os.environ.get("RWM_BASH_WRITE_SCAN", "1").strip().lower() in ("0", "false", "no", "off"):
        return []
    cwd, duration = d.get("cwd"), d.get("duration_ms")
    if not cwd or not os.path.isdir(cwd) or not isinstance(duration, (int, float)):
        return []
    import tree
    now = int(time.time() * 1000)
    since = now - int(duration) - SCAN_SLACK_MS
    root = git_root(cwd) or cwd
    listing = tree.list_tree(root, budget_s=0.5)
    if not listing.get("complete"):
        return []   # a partial listing would report some changes and silently miss others
    skip = recent_writes(d.get("session_id") or "", since, d.get("tool_use_id"))
    # A second of slack past now covers filesystem timestamps that run slightly ahead.
    return [p for p in changed_since(root, listing["files"], since, now + 1000) if p not in skip]


def on_pending(d: dict, base: dict) -> list[dict]:
    """PreToolUse: the moment a call is made, before it runs or asks permission.
    Lets the dashboard light a file up immediately; PostToolUse confirms it with
    exact ranges. A call that never completes leaves only this record."""
    tool = d.get("tool_name")
    ti = d.get("tool_input") or {}
    cwd = d.get("cwd")
    rec = dict(base, kind="pending", tool=tool, tool_use_id=d.get("tool_use_id"))
    if tool == "Read":
        rec.update({"op": "read", "path": absolute(ti.get("file_path"), cwd)})
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        rec.update({"op": "write", "path": absolute(ti.get("file_path") or ti.get("notebook_path"), cwd)})
    elif tool in ("Grep", "Glob"):
        rec.update({"op": "list" if tool == "Glob" else "search", "pattern": ti.get("pattern"),
                    "scope": absolute(ti.get("path"), cwd) or cwd})
    elif tool == "Bash":
        import shell_reads
        out = []
        for act in shell_reads.parse(ti.get("command") or "", cwd or os.getcwd())[:40]:
            r = dict(rec, op=act["op"], source="bash", cmd=act.get("cmd"))
            if act["op"] in ("read", "write"):
                r["path"] = act.get("path")
            else:
                r.update({"pattern": act.get("pattern"), "scope": (act.get("scope") or [None])[0]})
            out.append(clean(r))
        return out
    else:
        return []
    return [clean(rec)] if rec.get("path") or rec.get("op") in ("search", "list") else []


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
    return clean(rec)


# ---------------------------------------------------------------- server

def ensure_server() -> None:
    """Start the server check detached. SessionStart is the one synchronous hook, and
    waiting for it held every session start for ~190 ms; the server is up within a
    fraction of a second either way, long before anyone opens the link."""
    import subprocess
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, "serve.py"), "ensure"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def git_root(path: str | None) -> str | None:
    """Repository root for `path`, which is the unit sessions get grouped into."""
    import subprocess
    if not path or not os.path.isdir(path):
        return None
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or None
    except Exception:
        return None


def viewer_url(session_id: str) -> str:
    port = os.environ.get("RWM_PORT", "7788")
    return f"http://127.0.0.1:{port}/s/{session_id}?view=tree"


# ---------------------------------------------------------------- entry

TOOL_HANDLERS = {"Read": on_read, "Edit": on_edit, "Write": on_write,
                 "Grep": on_search, "Glob": on_search}


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
    sys.path.insert(0, HERE)

    if event == "SessionStart":
        source = d.get("source")
        kind = "compact" if source == "compact" else "session"
        rec = base_record(d, kind)
        if kind == "compact":
            rec["phase"] = "post"
        else:
            rec.update({"phase": "start", "source": source, "cwd": d.get("cwd"),
                        "model": d.get("model")})
        append(sid, [clean(rec)])
        cwd = d.get("cwd")
        write_meta(sid, clean({"session_id": sid, "cwd": cwd,
                               "project": git_root(cwd) or cwd,
                               "started_at": int(time.time() * 1000),
                               "source": source, "model": d.get("model"),
                               "title": d.get("session_title"),
                               "transcript_path": d.get("transcript_path"),
                               "closed": False}))
        if kind == "session":
            note_start(sid, cwd, source)
        ensure_server()
        out = {"systemMessage": f"read/write monitor → {viewer_url(sid)}"}

    elif event == "SessionEnd":
        rec = base_record(d, "session")
        rec.update({"phase": "end", "reason": d.get("reason")})
        append(sid, [rec])
        write_meta(sid, {"closed": True, "ended_at": int(time.time() * 1000)})

    elif event == "PreCompact":
        rec = base_record(d, "compact")
        rec.update({"phase": "pre", "trigger": d.get("trigger")})
        append(sid, [rec])

    elif event == "InstructionsLoaded":
        rec = on_instructions(d, base_record(d, "instructions"))
        if rec:
            append(sid, [rec])

    elif event == "UserPromptSubmit":
        # The prompt's text is never logged as an event. Its first line labels the
        # session in the picker unless RWM_LABEL_PROMPTS=0, because prompts can be private.
        append(sid, [base_record(d, "prompt")])
        if label_prompts():
            meta_path = os.path.join(session_dir(sid), "meta.json")
            try:
                has_label = bool(json.load(open(meta_path)).get("label"))
            except Exception:
                has_label = False
            text = " ".join(str(d.get("prompt") or "").split())
            if text and not has_label:
                write_meta(sid, {"label": text[:80] + ("…" if len(text) > 80 else "")})

    elif event == "PreToolUse":
        append(sid, on_pending(d, base_record(d, "pending")))

    elif event == "PostToolUse":
        tool = d.get("tool_name")
        if tool == "Bash":
            append(sid, on_bash(d, base_record(d, "read")))
        else:
            handler = TOOL_HANDLERS.get(tool)
            if handler:
                rec = handler(d, base_record(d, "search" if tool in ("Grep", "Glob") else tool.lower()))
                if rec:
                    append(sid, [rec])

    elif event == "PostToolUseFailure":
        rec = base_record(d, "failed")
        rec.update({"tool": d.get("tool_name"), "tool_use_id": d.get("tool_use_id")})
        append(sid, [clean(rec)])

    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A monitoring hook must never disturb the session.
        sys.exit(0)
