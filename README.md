# claude-read-write-monitor

A Claude Code plugin that records the exact files and line ranges an agent reads, searches and
edits, and serves a live local dashboard: a map of the project's files that lights up as the agent
works, a per-session timeline, and a rollup across sessions.

**No file content is ever stored** — only paths, line numbers and counts. The one piece of prompt
text kept is a session label: the first prompt, cut at 80 characters, stored in the session's
`meta.json` so the session picker is readable. Set `RWM_LABEL_PROMPTS=0` to turn it off.

## Install

The repository is its own marketplace:

```sh
claude plugin marketplace add BartSoj/claude-read-write-monitor
claude plugin install read-write-monitor@claude-read-write-monitor --scope user
```

Or load a working copy for one session, without installing:

```sh
claude --plugin-dir path/to/claude-read-write-monitor
```

### Iterating

An installed plugin runs from a **copy** in the plugin cache, so edits to the working copy have
no effect until they are published:

```sh
git push
claude plugin marketplace update claude-read-write-monitor
claude plugin update read-write-monitor@claude-read-write-monitor   # restart to apply
```

`plugin.json` deliberately declares no `version`, so the plugin tracks the resolved commit and
every push is an update. Add a `version` field only when the release cycle should be pinned.

To iterate without that loop, use `--plugin-dir` on the working copy.

The dashboard URL for the session is printed at session start and opens the Tree view. The index at
<http://127.0.0.1:7788> lists every session on this machine and opens the newest.

The server listens on `127.0.0.1` only. To open it from another machine, forward the port, for
example `ssh -L 7788:127.0.0.1:7788 <host>`.

## What it records

| Hook | Recorded |
|---|---|
| `PreToolUse(Read, Edit, Write, Grep, Glob, Bash)` | the call the moment it is made: read, write, search or list, and the path or pattern |
| `PostToolUse(Read)` | path, exact line range returned, file length, size |
| `PostToolUse(Edit)` | path, exact changed line spans in pre- and post-edit coordinates |
| `PostToolUse(Write)` | path, create/update, written spans, file length |
| `PostToolUse(Grep, Glob)` | pattern, scope, and the names of the files it matched (up to 1,000) |
| `PostToolUse(Bash)` | reads, searches, listings and writes inferred from the command, marked `shell` |
| `PostToolUseFailure` | that the call failed |
| `UserPromptSubmit` | a turn marker, and the first prompt as the session label |
| `InstructionsLoaded` | `CLAUDE.md` and rules files, with why they loaded |
| `PreCompact`, `SessionStart(compact)` | compaction boundaries |
| `SessionStart`, `SessionEnd` | session metadata |

Every tool hook is async, so none sits in the agent's path. The `PreToolUse` record lets the
dashboard light a file up as the call is made; the `PostToolUse` record replaces it with exact
ranges.

**Reads through Bash are inferred, not observed.** `cat`, `head`, `tail`, `sed -n`, `awk NR`, `nl`,
`bat` and `less` count as reads; `grep`, `rg`, `ag` and `git grep` as searches; `find`, `fd`, `ls`,
`tree` and `rg --files` as listings. Redirects (`>`, `>>`, including heredocs), `tee`, `sed -i`,
`perl -pi`, `cp`, `mv` and `touch` count as writes, with the line count when the command itself shows
it. The parser follows `cd`, pipes, variables and `for` loops, never parses a heredoc body as commands,
and skips anything it cannot read with confidence, such as command substitution. Read line ranges are
resolved against the file on disk; sizes for those ranges are estimates. Shell writes carry no line
spans.

Subagent activity is tagged with `agent_type` and kept in its own lane.

## Views

**Tree** — every file in the project as a tile inside its folder group, sized to fit the screen.
A folder with more than 48 files splits into its subfolders, up to three levels deep, and the
threshold rises until there are at most 40 groups. Tiles change state in place: blue when
read (brighter the more of the file was read), orange when written, split when both, a blue ring when
a search matched it, a corner notch for instruction files, hatched when only a subagent touched it,
pulsing while a call is in flight. New files pop into place. The file being worked on is named in a
ticker and a callout above its tile. A counter strip shows files read, written and untouched, lines
read and written, instruction files' share of all reading, when the first edit came and how many files
had been read by then, and elapsed time. The footer holds the legend and what the view cannot see.

**Session** — coverage strips per file (read ranges shaded by re-read depth, written ranges
alongside), summary tiles, and a chronological timeline grouped by turn with compaction
boundaries. Searches sit in their own indented lane, and shell-inferred records carry a `shell`
badge. Line numbers are projected onto the file's current state by replaying edit spans.

**Project** — every session for one project rolled up: folder table where each row covers its
whole subtree, sortable file table, cold lists for folders and files, and a read-per-line-written
ratio. "Never touched" is measured against the project's file listing: `git ls-files` (tracked plus
untracked files that are not ignored) in a repository, a filesystem walk that skips ignored and
generated files otherwise. Instruction loads are counted separately from code reading, and resume
cycles are shown alongside session count because a resumed session keeps its id and reloads
instructions into a fresh context.

### Deep links

| URL | Opens |
|---|---|
| `/` | the newest session |
| `/s/<session_id>` | one session |
| `?view=tree\|session\|project` | a view (`?tab=` still works) |
| `?project=<abs path>` | a project in the picker and the Project view |
| `?follow=<abs path>` | follow mode, below |
| `?replay=<session_id>&speed=<n>&maxgap=<s>` | replay, below |
| `?projector=1` | projector mode, below |
| `?theme=light\|dark\|stage` | a theme |

Keys: `t` opens the Tree view, `p` toggles projector mode.

### Follow mode

For demos and pairing: open the dashboard before the agent starts.

```
http://127.0.0.1:7788/?follow=/abs/path/to/project
```

The page draws the folder's tree and waits. When a session starts, resumes or clears in that folder or
below, the page switches to it at once, and again for every later one. If a session there is already
running and active in the last `RWM_FOLLOW_RECENT_MINUTES`, it is shown on load. Picking a session or
project by hand ends following for that page.

### Replay

```
http://127.0.0.1:7788/?replay=<session_id>&speed=4&maxgap=3
```

Plays a recorded session back on the Tree view at its original pace, divided by `speed`. `maxgap`
caps any pause between records at that many seconds. Playback starts just before the first prompt.
Space pauses, `r` restarts, `+` and `-` double or halve the speed.

### Projector mode

`?projector=1`, the Projector button, or `p`. Hides the header, enlarges the ticker, counters and
labels for reading from the back of a room, and switches to the Tree view and the `stage` theme: a
dark palette where read is blue and written is orange, with text lifted to at least 7:1 contrast. An
explicit `?theme=` still wins.

Themes are `light`, `dark` and `stage`. The ◑ button on the Session view cycles them and remembers the
choice; `RWM_THEME` sets the default.

## What it cannot see

- **`@file` references** in prompts. Claude Code inlines them while building the prompt; no hook fires.
- **Commands the shell parser does not know**: scripts, `git show`, `jq`, editors, and the forms it
  skips on purpose. Inferred reads can also miss or over-count.
- **Writes made from inside a program** — a Python or Node script, a formatter, `git checkout`,
  `curl -o`, `rsync`. Shell writes the parser recognises are recorded, but without line spans, so line
  numbers read before them are not projected across them.
- **WebFetch and MCP tool results.**
- **Subagent return summaries.** The subagent's own reads are recorded; what it hands back is not.

The dashboard says so on the page rather than presenting its totals as complete.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RWM_PORT` | `7788` | Server port |
| `RWM_DATA_DIR` | `${CLAUDE_PLUGIN_DATA}` | Where sessions are stored |
| `RWM_IDLE_MINUTES` | `120` | Idle time before the server exits |
| `RWM_TREE_MAX` | `5000` | Most files listed in the Tree and Project views; the shallowest are kept |
| `RWM_IGNORE` | — | Extra files to leave out of the listing: comma-separated gitignore-style patterns, `!` re-includes |
| `RWM_LABEL_PROMPTS` | on | `0` stops storing the first prompt as the session label |
| `RWM_INSTRUCTION_FILES` | `CLAUDE.md,CLAUDE.local.md,AGENTS.md,GEMINI.md,.claude/rules/**,.cursor/rules/**,.github/copilot-instructions.md,SKILL.md` | Comma-separated globs for files counted as instructions |
| `RWM_THEME` | follows the OS | Default theme: `light`, `dark` or `stage` |
| `RWM_FOLLOW_RECENT_MINUTES` | `30` | How recently a running session must have been active for follow mode to show it on load |

Hooks run with Claude Code's environment, so set these in the `env` block of Claude Code settings
(`~/.claude/settings.json`, or a project's `.claude/settings.json`):

```json
{
  "env": {
    "RWM_LABEL_PROMPTS": "0",
    "RWM_IGNORE": "fixtures/,*.snap"
  }
}
```

The server takes its environment from the session that started it. After changing a server setting,
stop it (`python3 scripts/serve.py stop`); the next session starts it again.

## Layout

```
.claude-plugin/plugin.json   manifest
hooks/hooks.json             hook wiring
scripts/record.py            hook recorder — appends JSON lines per event
scripts/shell_reads.py       infers reads, searches and listings from Bash commands
scripts/tree.py              project file listing, from git or a filesystem walk
scripts/serve.py             shared local server (ensure | run | stop | status)
scripts/latency.py           development tool: tool call to highlight latency
web/viewer.html              the dashboard, self-contained
tests/                       unit tests for tree.py and shell_reads.py
```

State: `${CLAUDE_PLUGIN_DATA}/sessions/<session_id>/events.jsonl` and `meta.json`, plus
`${CLAUDE_PLUGIN_DATA}/starts.jsonl`, one line per session start.

## Tests

```sh
python3 -m unittest discover -s tests
```

Standard library only.

## Server

```sh
python3 scripts/serve.py status   # is it up
python3 scripts/serve.py run      # foreground
python3 scripts/serve.py stop
```

It starts itself on `SessionStart` and exits on its own after `RWM_IDLE_MINUTES` with no requests
and no session activity, since `SessionEnd` is not guaranteed to fire. An open dashboard counts as
activity. After a plugin update, the next session replaces a server still running the older code.

Run by hand, the server reads `RWM_DATA_DIR`, else `~/.claude/plugins/data/read-write-monitor`, which
is not necessarily where the installed plugin records; set `RWM_DATA_DIR` to match.
`python3 scripts/serve.py ensure` reports when the port is held by a server reading a different
directory.

Live updates are pushed to the page over server-sent events. The API is described in the source of
`scripts/serve.py`.
