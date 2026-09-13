#!/usr/bin/env python3
"""read-write-monitor: export one recorded session as a replay that needs no server.

Writes <out>/index.html: the viewer, the session's records, a snapshot of the file tree and,
optionally, an expectation set, all in one file. It opens from file:// or any static host, starts
paused, and can be driven by a page that frames it (see README, "Static replay export").

  export_replay.py <session_id> --out <dir> [--expect <set>] [--tree <dir>] [--name <label>]
                   [--at <step>] [--reserve <px>] [--keep-dotfiles] [--speed <n>] [--forbid <word> ...]

Paths are rewritten relative to the project, under --name; paths outside it keep only their file
name. No file content is included: the records never hold any.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import serve  # noqa: E402
import tree  # noqa: E402

VIEWER = os.path.join(os.path.dirname(HERE), "web", "viewer.html")
PATH_KEYS = ("path", "scope", "trigger_file_path", "parent_file_path")
DROP_KEYS = ("cwd", "model", "transcript_path")
STEP_KINDS = ("read", "instructions", "edit", "write", "search")
DEFAULT_INSTRUCTION_FILES = serve.DEFAULT_INSTRUCTION_FILES.split(",")

# Stands in for the network: the viewer asks its usual API and gets the embedded data back.
SHIM = """(() => {
  const S = window.RWM_STATIC;
  const reply = (v) => Promise.resolve(new Response(JSON.stringify(v), { headers: { "Content-Type": "application/json" } }));
  window.fetch = (url) => {
    const u = String(url);
    if (u.startsWith("/api/config")) return reply(S.config);
    if (u.startsWith("/api/tree")) return reply(S.listing);
    if (u.startsWith("/api/events/")) return reply({ offset: S.events.length, events: S.events, meta: S.meta });
    if (u.startsWith("/api/expectations")) return reply({ dir: "", sets: S.expectations });
    if (u.startsWith("/api/sessions")) return reply({ sessions: [], total: 0 });
    if (u.startsWith("/api/projects")) return reply({ projects: [] });
    return Promise.reject(new Error("offline replay: " + u));
  };
  window.EventSource = class { addEventListener() {} close() {} };
  const replace = history.replaceState.bind(history);
  history.replaceState = (...args) => { try { replace(...args); } catch (e) {} };
  try { localStorage.getItem("rwm"); } catch (e) {
    const m = new Map();
    Object.defineProperty(window, "localStorage", { value: {
      getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) } });
  }
})();"""


def relative(path: str, roots: tuple[str, ...], name: str) -> str:
    for base in roots:
        if path == base:
            return name
        if path.startswith(base + "/"):
            return name + "/" + path[len(base) + 1:]
    return "outside/" + os.path.basename(path.rstrip("/"))


def sanitize(events: list[dict], root: str, name: str) -> list[dict]:
    """Records with every path made relative to the project and host-specific fields dropped."""
    roots = tuple(dict.fromkeys([root.rstrip("/"), os.path.realpath(root).rstrip("/")]))
    out = []
    for e in events:
        e = {k: v for k, v in e.items() if k not in DROP_KEYS}
        for k in PATH_KEYS:
            if isinstance(e.get(k), str) and e[k].startswith("/"):
                e[k] = relative(e[k], roots, name)
        if isinstance(e.get("files"), list):
            e["files"] = [relative(f, roots, name) if isinstance(f, str) and f.startswith("/") else f
                          for f in e["files"]]
        out.append(e)
    return out


def is_dot(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.split("/"))


def count_steps(events: list[dict]) -> int:
    """The replay's steps: reads, instruction loads (not repeated within 5 s), searches, writes."""
    steps, last = 0, {}
    for e in events:
        if e.get("kind") not in STEP_KINDS or not e.get("ts"):
            continue
        if e["kind"] == "instructions":
            prev = last.get(e.get("path"))
            if prev is not None and abs(e["ts"] - prev) < 5000:
                continue
            last[e.get("path")] = e["ts"]
        steps += 1
    return steps


def build(session_id: str, name: str | None = None, tree_dir: str | None = None, expect: str | None = None,
          at: int = 1, reserve: int = 80, keep_dotfiles: bool = False, speed: float | None = None) -> tuple[str, dict]:
    bundle = serve.export_bundle(session_id)
    if not bundle:
        raise SystemExit(f"no session {session_id} in {serve.sessions_dir()}")
    meta = bundle["meta"]
    root = (meta.get("project") or meta.get("cwd") or "").rstrip("/")
    if not root:
        raise SystemExit("the session records no project folder")
    name = name or os.path.basename(root) or "project"
    listing = tree.list_tree(tree_dir or root)
    files = [f for f in listing["files"] if keep_dotfiles or not is_dot(f)]
    sets = []
    if expect:
        match = [s for s in serve.list_expectations(root) if s["name"] == expect]
        if not match:
            raise SystemExit(f"no expectation set {expect!r} for {root}")
        sets = [{"name": expect, "root": None, "reads": match[0]["reads"], "writes": match[0]["writes"],
                 "note": match[0].get("note", "")}]
    events = sanitize(bundle["events"], root, name)
    query = {"replay": session_id, "view": "tree", "layout": "map", "projector": "1", "theme": "stage",
             "embed": "1", "step": "1", "at": str(at), "reserve": str(reserve)}
    if expect:
        query["expect"] = expect
    if speed:
        query["speed"] = f"{speed:g}"
    static = {
        "query": "&".join(f"{k}={v}" for k, v in query.items()),
        "hideDot": not keep_dotfiles,
        "config": {"instruction_files": DEFAULT_INSTRUCTION_FILES, "theme": "stage", "home": ""},
        "listing": {"root": name, "real": name, "source": "snapshot", "files": files, "total": len(files),
                    "complete": True, "truncated": False, "omitted": {}},
        "events": events,
        "meta": {"session_id": session_id, "label": meta.get("label"), "project": name, "cwd": name, "closed": True},
        "expectations": sets,
    }
    html = open(VIEWER, encoding="utf-8").read()
    data = json.dumps(static, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    head = f"<script>window.RWM_STATIC = {data};</script>\n<script>{SHIM}</script>\n<script>"
    cut = html.index("<script>")
    html = html[:cut] + head + html[cut + len("<script>"):]
    html = html.replace("<title>read/write monitor</title>", f"<title>{name} · replay</title>", 1)
    info = {"steps": count_steps(bundle["events"]), "records": len(events), "files": len(files),
            "expectation": expect, "bytes": len(html.encode("utf-8"))}
    return html, info


def check(html: str, forbid: list[str]) -> list[str]:
    """What must not leave the machine: the home directory, and any word the caller forbids."""
    problems = []
    home = os.path.expanduser("~")
    if home and home != "/" and home in html:
        problems.append(f"contains the home directory path {home}")
    low = html.lower()
    for word in forbid:
        if word and word.lower() in low:
            problems.append(f"contains the forbidden word {word!r}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id")
    ap.add_argument("--out", required=True, help="directory to write index.html into")
    ap.add_argument("--expect", help="expectation set to include")
    ap.add_argument("--tree", help="folder whose listing is the tree (default: the project as it is now)")
    ap.add_argument("--name", help="label for the project root (default: its folder name)")
    ap.add_argument("--at", type=int, default=1, help="step the replay opens at, and returns to on Home (default 1)")
    ap.add_argument("--reserve", type=int, default=80, help="pixels left free at the bottom (default 80)")
    ap.add_argument("--keep-dotfiles", action="store_true", help="show dot-folders such as .claude/")
    ap.add_argument("--speed", type=float, help="playback speed the replay opens with (default 1; ?speed= overrides)")
    ap.add_argument("--forbid", action="append", default=[], help="fail if this word appears in the output")
    a = ap.parse_args()
    html, info = build(a.session_id, a.name, a.tree, a.expect, a.at, a.reserve, a.keep_dotfiles, a.speed)
    problems = check(html, a.forbid)
    if problems:
        print("not written: " + "; ".join(problems), file=sys.stderr)
        return 1
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "index.html")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(path + ".tmp", path)
    print(f"wrote {path}: {info['steps']} steps, {info['records']} records, {info['files']} files in the tree, "
          f"{info['bytes'] // 1024} KB" + (f", expecting {info['expectation']}" if info["expectation"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
