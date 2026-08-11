# claude-read-write-monitor

A Claude Code plugin that records the exact files and line ranges an agent reads and edits, and
serves a live per-session dashboard.

Design, decisions and research notes live in `~/.syns/read-write-monitoring`.

## Install

For development, load it for one session:

```sh
claude --plugin-dir ~/IdeaProjects/claude-read-write-monitor
```

The dashboard URL for the session is printed at session start. The index at
<http://127.0.0.1:7788> lists every session on this machine and opens the newest.

To share it off the machine: `bb connect expose 7788`.

## What it records

| Hook | Recorded |
|---|---|
| `PostToolUse(Read)` | path, exact line range returned, file length, size |
| `PostToolUse(Edit)` | path, exact changed line spans in pre- and post-edit coordinates |
| `PostToolUse(Write)` | path, create/update, written spans, file length |
| `InstructionsLoaded` | `CLAUDE.md` and rules files, with why they loaded |
| `PreCompact`, `SessionStart(compact)` | compaction boundaries |
| `SessionStart`, `SessionEnd` | session metadata |

Subagent activity is tagged with `agent_type` and kept in its own lane.

**No file content is ever stored** — only paths, line numbers and counts.

Reads that don't go through the `Read` tool are not recorded: `@file` references (no hook fires
for these at all), `cat`/`sed` via Bash, Grep, WebFetch and MCP results. This is deliberate; the
dashboard says so on the page.

## Layout

```
.claude-plugin/plugin.json   manifest
hooks/hooks.json             hook wiring
scripts/record.py            hook recorder — appends one JSON line per event
scripts/serve.py             shared local server (ensure | run | stop | status)
web/viewer.html              the dashboard, self-contained
```

State: `${CLAUDE_PLUGIN_DATA}/sessions/<session_id>/events.jsonl`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RWM_PORT` | `7788` | Server port |
| `RWM_DATA_DIR` | `${CLAUDE_PLUGIN_DATA}` | Where sessions are stored |
| `RWM_IDLE_MINUTES` | `120` | Idle time before the server exits |

## Server

```sh
python3 scripts/serve.py status   # is it up
python3 scripts/serve.py run      # foreground
python3 scripts/serve.py stop
```

It starts itself on `SessionStart` and exits on its own after `RWM_IDLE_MINUTES` with no requests
and no session activity, since `SessionEnd` is not guaranteed to fire.
