#!/usr/bin/env python3
"""read-write-monitor: tool-call-to-highlight latency report.

Joins the highlight times a dashboard page collected (`window.rwmLatency`, saved as
JSON) with the tool-call times in the session's Claude Code transcript.

  t_call   the transcript timestamp of the assistant entry that carries the tool_use
  hook_ms  when the recorder's hook started and stamped the record
  server   when the server read the record off disk and pushed it
  recv     when the page received the batch
  lit      the next animation frame after the tile changed

  latency.py --samples samples.json --transcript <session>.jsonl [--json]
  latency.py --samples samples.json --session <session_id>      # transcript from meta.json

Development tool: nothing in the plugin runs it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime


def data_dir() -> str:
    d = os.environ.get("RWM_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    return d or os.path.expanduser("~/.claude/plugins/data/read-write-monitor")


def tool_calls(transcript: str) -> dict[str, tuple[int, str]]:
    """tool_use_id → (epoch ms of the entry that carries it, tool name)."""
    out: dict[str, tuple[int, str]] = {}
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            msg = e.get("message") or {}
            ts = e.get("timestamp")
            if not ts or not isinstance(msg.get("content"), list):
                continue
            ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    out.setdefault(block["id"], (ms, block.get("name") or ""))
    return out


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": round(statistics.median(values)),
            "p95": round(pct(values, 0.95)), "max": round(max(values)), "min": round(min(values))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", required=True, help="JSON list saved from window.rwmLatency")
    ap.add_argument("--transcript", help="Claude Code transcript .jsonl")
    ap.add_argument("--session", help="session id; reads transcript_path from its meta.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    transcript = a.transcript
    if not transcript and a.session:
        meta = json.load(open(os.path.join(data_dir(), "sessions", a.session, "meta.json")))
        transcript = meta.get("transcript_path")
    if not transcript:
        ap.error("--transcript or --session is required")

    samples = json.load(open(a.samples))
    calls = tool_calls(transcript)
    first: dict[str, dict] = {}
    painted = 0
    for s in samples:
        # The frame after the DOM change when the page produced one; otherwise the DOM change.
        s["lit_ms"] = s.get("paint_ms") or s.get("dom_ms") or s.get("lit_ms")
        painted += bool(s.get("paint_ms"))
        sid = s.get("id")
        if sid in calls and s["lit_ms"] and (sid not in first or s["lit_ms"] < first[sid]["lit_ms"]):
            first[sid] = s

    stages = {"total": [], "call_to_hook": [], "hook_to_server": [], "server_to_page": [], "page_to_frame": []}
    rows = []
    for sid, s in first.items():
        t_call, tool = calls[sid]
        total = s["lit_ms"] - t_call
        stages["total"].append(total)
        stages["call_to_hook"].append(s["hook_ms"] - t_call)
        if s.get("server_ms"):
            stages["hook_to_server"].append(s["server_ms"] - s["hook_ms"])
            stages["server_to_page"].append(s["recv_ms"] - s["server_ms"])
        if s.get("recv_ms"):
            stages["page_to_frame"].append(s["lit_ms"] - s["recv_ms"])
        rows.append({"id": sid, "tool": tool, "kind": s.get("kind"), "total_ms": total})

    report = {"transcript": transcript, "tool_calls": len(calls), "matched": len(first),
              "stages": {k: summary(v) for k, v in stages.items()},
              "slowest": sorted(rows, key=lambda r: -r["total_ms"])[:5]}
    if a.json:
        print(json.dumps(report, indent=1))
        return 0
    print(f"{report['matched']} of {report['tool_calls']} tool calls highlighted")
    for k, v in report["stages"].items():
        if v["n"]:
            print(f"  {k:16} n={v['n']:<4} median={v['median']:>5} ms  p95={v['p95']:>5} ms  max={v['max']:>5} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
