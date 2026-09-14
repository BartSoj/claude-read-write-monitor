# claude-read-write-monitor

A Claude Code plugin that records the exact files and line ranges an agent reads, searches and
edits, and serves a live local dashboard: a map of the project's files that lights up as the agent
works, a per-session timeline, and a rollup across sessions.

**No file content is ever stored** — only paths, line numbers and counts. The one piece of prompt
text kept is a session label: the first prompt, cut at 80 characters, stored in the session's
`meta.json` so the session picker is readable. Set `RWM_LABEL_PROMPTS=0` to turn it off.

## See it

Two recorded sessions in a markdown wiki of about 400 files that agents maintain. Every tile is a file:
**blue** when read, **orange** when written. A white ring marks a file the task was expected to read or
change and has not yet. Both are replays with every wait over a second cut, so several minutes of
work play in about one. Click a GIF to download the 1080p MP4.

### A new page: does the agent read what it needs before it writes?

> Write the guide we planned on scheduling and monitoring — how to set up a standing monitor that keeps
> running — using only what the atlas already holds, no web.

[![Demo 1: an agent reads its way through a wiki before writing a new guide](https://raw.githubusercontent.com/BartSoj/claude-read-write-monitor/main/media/demo-1.gif)](https://raw.githubusercontent.com/BartSoj/claude-read-write-monitor/main/media/demo-1.mp4)

- **Expected:** 14 files to read (the schema, the guide template, the scope rulings, the need, its
  methods and sources, the sibling guides) and 6 to change.
- **What happened:** the agent read 13 of the 14 before writing anything, then created the guide and
  added backlinks from the pages that should point to it. That took about 9 minutes; the first edit
  came at 2:00, after 25 files.
- **The miss:** the ring left on `_meta/scope.md`. The agent found it with a search and never opened
  it. That points at the wiki's structure, not at the prompt: the guide template does not link the
  scope rulings.
- **One dark tile:** `log.md` was appended from a Python heredoc, which this recording predates. The
  monitor now catches such writes by modification time.

### One change: does it reach everything that depends on it?

> Heads up: Bluesky announced on 10 September that its public search API now needs an API key. Don't
> probe or fetch anything — record it as reported and update every page that relies on it being
> keyless.

The announcement is made up for the test.

[![Demo 2: one reported change spreads to every page that depends on it](https://raw.githubusercontent.com/BartSoj/claude-read-write-monitor/main/media/demo-2.gif)](https://raw.githubusercontent.com/BartSoj/claude-read-write-monitor/main/media/demo-2.mp4)

- **Expected:** 9 files to read and 6 to change: the source page, the two need pages whose source
  ladders list its auth as `none`, the index line that calls it keyless, a roadmap row, and the log.
- **What happened:** one search for the source's link put rings on exactly those pages, then each
  turned orange. The run ended at 9 / 9 and 6 / 6, in under 5 minutes; the first edit came at 2:44,
  after 12 files.

## This repository is the implementation only

The design is in a public Syns repository,
[`bartsoj/read-write-monitoring`](https://syns.dev/bartsoj/read-write-monitoring): `SPEC.md` is the
specification (architecture, event schema, line-drift model, server API, views, blind spots),
`DECISIONS.md` the reasons behind it, `RESEARCH.md` what Claude Code's hooks were observed to provide,
and `STATUS.md` what is verified and what is not. Nothing here decides what the tool should be.

## Requirements

- Claude Code with plugin support.
- `python3` on `PATH`. Standard library only; tested with 3.13.
- macOS is verified. Linux is expected to work but is not verified yet. Windows is not supported.
- `git` is optional: without it the file listing walks the folder.

## Install

The repository is its own marketplace:

```sh
claude plugin marketplace add BartSoj/claude-read-write-monitor
claude plugin install read-write-monitor@claude-read-write-monitor --scope user
```

To update to a new release:

```sh
claude plugin marketplace update claude-read-write-monitor
claude plugin update read-write-monitor@claude-read-write-monitor   # restart to apply
```

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
it. Files a command changed that the parser could not name — from a Python heredoc, a formatter, `git`
— are recorded too, found by modification time within the command's run (`RWM_BASH_WRITE_SCAN=0` turns
that off). The parser follows `cd`, pipes, variables and `for` loops, never parses a heredoc body as commands,
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

Two layouts, switched with the Map / Outline buttons or `l`: **Map** packs folders as blocks side by
side; **Outline** puts every folder on its own row, one under another and indented by depth, with its
files as one run of tiles that wraps at the edge of the screen. Hover a tile for the file's path and
the line ranges read and written; click it for a panel listing every read, write and search match in
order, with the time, the exact lines and the tool or shell command that did it.

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
| `?replay=<session_id>&speed=<n>&step=1&gaps=skip` | replay, below |
| `?layout=map\|outline` | a Tree view layout (remembered per browser) |
| `?expect=<name>` | an expectation set, below |
| `?projector=1` | projector mode, below |
| `?theme=light\|dark\|stage` | a theme |

Keys: `t` opens the Tree view, `p` toggles projector mode, `l` switches layout, `h` hides the tool
and replay bars, `Esc` closes the file panel.

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
http://127.0.0.1:7788/?replay=<session_id>&speed=4
```

Any recorded session plays back on the Tree view, from the **Replay** button or the URL. It plays at
its original pace divided by `speed`, starting just before the first prompt, or one **step** at a
time: a step is one read, search or write, so stepping skips every wait in between. `step=1` opens
paused at the start; `gaps=skip` caps each wait at one second while playing.

| Key | |
|---|---|
| `→` / `←` | next / previous step (going back rebuilds the tree up to that step) |
| `Space` | play or pause |
| `Home` / `End` | the start / the end |
| `+` / `-` | double / halve the speed |
| `g` | skip waits on or off |
| `r` | restart and play |

The bar under the counters has the same controls and a slider over all steps.

**Replay files.** *Save replay file* downloads the session as one JSON file (its records and
metadata: paths and line numbers, no file content). *Open replay file* plays such a file in any
dashboard, on any machine, without storing it; if the project folder is not there, the tree shows the
files the session touched. To keep a file as a session on a machine, so it is listed and replayable
by id, import it:

```sh
python3 scripts/serve.py export <session_id> session.json
python3 scripts/serve.py import session.json          # --force replaces an existing session
```

### Expectation sets

A list of the files a task should read and the files it should change. With a set chosen (the
selector in the Tree view's tool bar, or `?expect=<name>`), every listed file carries a ring until
the session has read or written it, and two counters show `expected reads 9 / 11` and
`expected writes 4 / 6`; hovering them lists what is still missing. Rings left at the end are the
misses: a file the agent needed and never opened, or a dependent it never updated.

*Edit expectations* makes a click on a tile add or remove it (as a read or a write), or edit the set
as text; *Save* stores it. Sets are plain text files kept on this machine, outside every project, so an
agent at work never sees them: `${RWM_DATA_DIR}/expectations/<name>.txt`.

```
# expectation set: new-guide
# optional note
root: /abs/path/to/project
read CLAUDE.md
read needs/standing-monitors.md
read methods/*.md          # a glob is satisfied by any one matching file, and rings no tile
write guides/*monitor*.md
write index.md
```

Paths are relative to `root`; a set without `root` is offered for every project.

### Static replay export

A recorded session can become one self-contained `index.html` that replays with no server: open it
from disk, host it on any static site, or frame it in slides.

```sh
python3 scripts/export_replay.py <session_id> --out replays/demo --expect <set> \
    [--tree <folder>] [--name <label>] [--at 1] [--reserve 80] [--keep-dotfiles] [--speed <n>] [--forbid <word>]
```

- **What goes in:** the viewer, the session's records, the file tree, and the expectation set. No
  file content, since the records hold none.
- **Paths:** rewritten relative to the project, under `--name` (default: the folder name). Anything
  outside the project keeps only its file name. The host-specific fields `cwd`, `model` and
  `transcript_path` are dropped.
- **The tree:** the listing of `--tree`. By default that is the project as it is now; give a folder
  holding its state at recording time to show that. Files the session created still animate in.
  Dot-folders such as `.claude/` are hidden; what the session did in them still counts.
- **What it looks like:** the Tree view in map layout, stage palette and projector sizes, with no
  header or tool bars, sized for 1920×1080. `--reserve` pixels stay free at the bottom, for a caption.
  It opens paused at step `--at`, at speed `--speed` (default ×1), with every wait capped at one second.
- **Page query:** the file's own query overrides what was baked in: `index.html?autoplay=1` plays from
  the opening step, `?speed=<n>` sets the speed, `?gaps=full` keeps the recorded waits.
- **Refusals:** the export will not write a file that contains your home directory path or any
  `--forbid` word.

**Embedding.** A page that frames the replay drives it with
`iframe.contentWindow.postMessage({type: "rwm", cmd}, "*")`, where `cmd` is `next`, `prev`, `home`
(back to the opening step), `end`, `play`, `pause`, `toggle`, `faster`, `slower` or `speed:<n>`.
`faster` and `slower` move along ×0.25 ×0.5 ×0.75 ×1 ×1.5 ×2 ×3 ×4 ×6 ×8 ×12 ×16. After every change the
replay posts
`{type: "rwm", event: "state", step, total, playing, speed, expectedReads: [done, of], expectedWrites: [done, of]}`
to its parent. Framed, the replay handles no keys: every keydown is cancelled and posted to the parent
as `{type: "rwm", event: "key", key, code, shiftKey, altKey, metaKey, ctrlKey}`, and a click inside
hands focus back, so the framing page stays the only keyboard owner. Opened on its own, it answers
`→` `←` `Space` `Home` `End` `+` `-`.

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
- **Which lines a shell command changed.** Shell writes carry no line spans, so line numbers read
  before them are not projected across them. Writes the parser cannot name (a Python heredoc, a
  formatter, `git checkout`) are still caught: after each Bash call, project files whose modification
  time falls inside that command's run are recorded as changed. Files beyond `RWM_TREE_MAX`, or in a
  project too large to list within half a second, are not checked.
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
| `RWM_BASH_WRITE_SCAN` | on | `0` stops checking which project files changed during a Bash command |
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

## Server

```sh
python3 scripts/serve.py status   # is it up
python3 scripts/serve.py run      # foreground
python3 scripts/serve.py stop
python3 scripts/serve.py export <session_id> [file.json]
python3 scripts/serve.py import <file.json> [--force]
```

It starts itself on `SessionStart` and exits on its own after `RWM_IDLE_MINUTES` with no requests
and no session activity, since `SessionEnd` is not guaranteed to fire. An open dashboard counts as
activity. After a plugin update, the next session replaces a server still running the older code.

Run by hand, the server reads `RWM_DATA_DIR`, else `~/.claude/plugins/data/read-write-monitor`, which
is not necessarily where the installed plugin records; set `RWM_DATA_DIR` to match.
`python3 scripts/serve.py ensure` reports when the port is held by a server reading a different
directory.

Live updates are pushed to the page over server-sent events. The API is specified in the design
repository's `SPEC.md`.

## Working on it

```sh
python3 -m unittest discover -s tests
claude --plugin-dir path/to/claude-read-write-monitor
```

An installed plugin runs from a copy in the plugin cache, so editing a clone changes nothing until a
release. `--plugin-dir` loads a clone for one session. If the plugin is also installed, keep the two
apart for that session:

```sh
RWM_DATA_DIR=/tmp/rwm-dev RWM_PORT=7789 claude --plugin-dir path/to/claude-read-write-monitor \
  --settings '{"enabledPlugins":{"read-write-monitor@claude-read-write-monitor":false}}'
```

```
.claude-plugin/plugin.json       manifest
.claude-plugin/marketplace.json  the repository as its own marketplace
hooks/hooks.json                 hook wiring
scripts/record.py                hook recorder — appends JSON lines per event
scripts/shell_reads.py           infers reads, searches, listings and writes from Bash commands
scripts/tree.py                  project file listing, from git or a filesystem walk
scripts/serve.py                 shared local server (ensure | run | stop | status | export | import)
scripts/export_replay.py         a recorded session as one self-contained replay page
scripts/latency.py               development tool: tool call to highlight latency
web/viewer.html                  the dashboard, self-contained
tests/                           unit tests: recorder, shell parser, listing, server, export
media/                           the demo GIFs and MP4s shown above
```

State: `${CLAUDE_PLUGIN_DATA}/sessions/<session_id>/events.jsonl` and `meta.json`, plus
`${CLAUDE_PLUGIN_DATA}/starts.jsonl`, one line per session start.

## Contributing

The specification comes before the code. The spec, the decisions behind it and the evidence for what
Claude Code's hooks provide are in a public Syns repository,
[`bartsoj/read-write-monitoring`](https://syns.dev/bartsoj/read-write-monitoring). Read it before you
change anything here. Bugs and questions are welcome as issues.

1. Fork the spec: `syns fork bartsoj/read-write-monitoring`.
2. Change the spec in your fork first: the behaviour in `SPEC.md`, and a decision in `DECISIONS.md`
   when behaviour changes.
3. Fork this repository and implement against your spec. Tests must pass.
4. Make your Syns fork public and link it from a pull request here. Code and spec are reviewed
   together and merged together.

## Release

1. `python3 -m unittest discover -s tests`, and a live session with `--plugin-dir`.
2. Bump `version` in `.claude-plugin/plugin.json`. Installs move only when it changes.
3. Record what was verified in the design repository's `STATUS.md`.
4. `git tag -a vX.Y.Z -m "read-write-monitor X.Y.Z" && git push origin main --tags`.

## License

MIT
