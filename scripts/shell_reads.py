#!/usr/bin/env python3
"""read-write-monitor: infer which files a shell command read, searched, listed or wrote.

This is a HEURISTIC. Claude Code agents often read and search through the Bash
tool (`cat`, `sed -n`, `head`, `grep -r`, `find`) instead of Read/Grep/Glob.
The hook only sees the command string, the working directory and (after the
call) stdout. This module turns that into approximate actions:

    parse(command, cwd)          -> actions inferred from the command text
    resolve_read(action)         -> a read with concrete line numbers
    hits(action, stdout, cwd)    -> files named in a search/list's stdout

Rules of the road:
  * Never stores or returns file content. Files are only stat()ed, and
    resolve_read counts newlines in binary chunks that are dropped at once.
  * Never raises. Every public function catches everything and returns [] / None.
  * Anything it cannot understand confidently yields nothing: command
    substitution, heredocs, stdout redirected to a file, unknown variables.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import stat

__all__ = ["parse", "resolve_read", "hits"]

MAX_READ_BYTES = 20 * 1024 * 1024
_CHUNK = 1 << 20
_MAX_COMMAND = 200_000
_MAX_ACTIONS = 1000
_MAX_GLOB = 1000
_MAX_LOOP_VALUES = 200
_MAX_STDOUT = 5_000_000
_MAX_LINES = 50_000
_MAX_DEPTH = 3

# Placeholder for anything the shell would compute at run time ($(...), `...`,
# process substitution). A segment containing it produces no reads/searches.
_SUB = "\x00"
# Marker words for stdin that comes from the command text: "\x01H<n>" is a heredoc
# with n body lines ("\x01H" when unknown), "\x01S" a here-string (next word).
_HEREDOC = "\x01"

_OPCHARS = set("();<>|&")
_OPS = sorted(["&>>", "<<<", ">>", "&&", "||", ";;", "|&", ">&", "<&", "&>", ">|", "<>", "<<",
               ";", "|", "&", "(", ")", "<", ">"], key=len, reverse=True)
_SEPS = {";", ";;", "&&", "||", "|", "|&", "&"}
_OUT_REDIRS = {">", ">>", ">|", "&>", "&>>", ">&"}
_IN_REDIRS = {"<", "<&", "<>"}

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-=]([^}$]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_FD_RE = re.compile(r"(\d+)(>>|>\||>|<)(&(?:\d+|-))?")
_WORD_START = " \t\n;&|()"

_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "done", "fi",
             "esac", "nohup", "exec", "builtin", "noglob"}
_ALIASES = {
    "ggrep": "grep", "egrep": "grep", "fgrep": "grep", "gegrep": "grep", "gfgrep": "grep",
    "gsed": "sed", "ghead": "head", "gtail": "tail", "gcat": "cat", "gfind": "find", "gls": "ls",
    "gawk": "awk", "mawk": "awk", "nawk": "awk", "batcat": "bat", "fdfind": "fd", "gnl": "nl",
    "gcp": "cp", "gmv": "mv", "gtouch": "touch", "gtee": "tee",
}
_WHOLE_READERS = {"cat", "nl", "bat", "less", "more"}
_SEARCHERS = {"grep", "rg", "ag"}
_HIDERS = {"wc", "xargs", "md5", "md5sum", "shasum", "sha1sum", "sha256sum", "sha512sum", "cksum",
           "true", "false", "sum", "b2sum"}

_GREP_LONG = {"--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context",
              "--include", "--exclude", "--exclude-dir", "--exclude-from", "--label", "--devices",
              "--directories", "--binary-files", "--group-separator"}
_RG_LONG = {"--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--max-count",
            "--after-context", "--before-context", "--context", "--max-columns", "--threads",
            "--encoding", "--replace", "--max-depth", "--type-add", "--type-clear", "--sort", "--sortr",
            "--color", "--colors", "--pre", "--pre-glob", "--path-separator", "--context-separator",
            "--field-context-separator", "--field-match-separator", "--ignore-file", "--max-filesize",
            "--dfa-size-limit", "--regex-size-limit", "--engine", "--hyperlink-format", "--generate"}
_AG_LONG = {"--file-search-regex", "--max-count", "--after", "--before", "--context", "--depth",
            "--ignore", "--ignore-dir", "--path-to-ignore", "--pager", "--workers"}
_GITGREP_LONG = {"--max-depth", "--threads", "--max-count", "--after-context", "--before-context",
                 "--context", "--regexp", "--file"}
_FD_LONG = {"--extension", "--type", "--exclude", "--max-depth", "--min-depth", "--exact-depth",
            "--size", "--threads", "--color", "--changed-within", "--changed-before",
            "--change-newer-than", "--change-older-than", "--owner", "--base-directory",
            "--search-path", "--max-results", "--path-separator", "--batch-size", "--ignore-file",
            "--format", "--and"}
_LS_LONG = {"--ignore", "--hide", "--width", "--tabsize", "--block-size", "--format", "--sort",
            "--time", "--time-style", "--quoting-style", "--indicator-style"}
_TREE_LONG = {"--filelimit", "--charset", "--sort", "--timefmt", "--gitfile", "--infile"}
_BAT_LONG = {"--line-range", "--language", "--highlight-line", "--style", "--theme", "--paging",
             "--color", "--map-syntax", "--tabs", "--wrap", "--terminal-width", "--decorations",
             "--italic-text", "--pager", "--file-name", "--diff-context", "--squeeze-limit"}


# ======================================================================== public API

def parse(command: str, cwd: str) -> list[dict]:
    """Actions inferred from a command string, in order. Paths absolute (normalised
    with os.path.normpath, not realpath).
      {"op": "read", "cmd": "cat", "path": abs, "start": int|None, "end": int|None, "from_end": int|None}
          start/end 1-based inclusive; both None = whole file; from_end=N means the last N lines
          (`head -c N` adds "bytes": N, resolved to a line range by resolve_read)
      {"op": "search", "cmd": "grep"|"rg"|"ag"|"git grep", "pattern": str, "scope": [abs,...],
       "files_only": bool, "single_file": bool, "cwd": abs}
      {"op": "list", "cmd": "find"|"fd"|"ls"|"rg --files"|"tree", "scope": [abs,...], "cwd": abs}
      {"op": "write", "cmd": "cat"|"printf"|"echo"|"tee"|"sed"|"cp"|"mv"|"touch"|"perl"|...,
       "path": abs, "mode": "append"|"overwrite"|"in-place"|"copy"|"move"|"touch", "lines": int|None}
          lines = lines written when the command text alone says so (heredoc body, echo, printf)
    """
    try:
        if not isinstance(command, str) or not command.strip() or len(command) > _MAX_COMMAND:
            return []
        start = None
        if isinstance(cwd, str) and os.path.isabs(cwd):
            start = os.path.normpath(cwd)
        st = _State(start)
        return _run(command, st, 0)[:_MAX_ACTIONS]
    except Exception:
        return []


def resolve_read(action: dict) -> dict | None:
    """For a read action: return None if the path is not an existing regular file; otherwise a copy
    with concrete "start"/"end" and "total_lines" filled in. total_lines uses Claude Code's convention:
    text.count("\\n") + 1 (a trailing newline opens a final empty line). Newlines are counted in binary
    chunks; content is never kept. end is clamped to total_lines. Files over 20 MB return None, as does
    a range that starts past the end of the file."""
    try:
        if not isinstance(action, dict) or action.get("op", "read") != "read":
            return None
        path = action.get("path")
        if not isinstance(path, str) or not path:
            return None
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_READ_BYTES:
            return None
        byte_limit = action.get("bytes")
        byte_limit = int(byte_limit) if byte_limit is not None else None
        newlines = 0
        last = b""
        prefix_newlines = None
        prefix_ends_nl = False
        seen = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                if byte_limit is not None and prefix_newlines is None and seen + len(chunk) >= byte_limit:
                    cut = max(0, byte_limit - seen)
                    prefix_newlines = newlines + chunk.count(b"\n", 0, cut)
                    prefix_ends_nl = cut > 0 and chunk[cut - 1:cut] == b"\n"
                newlines += chunk.count(b"\n")
                last = chunk[-1:]
                seen += len(chunk)
                del chunk
        total = newlines + 1
        last_real = total - 1 if last == b"\n" else total
        if byte_limit is not None:
            if byte_limit <= 0:
                return None
            if prefix_newlines is None:  # limit covers the whole file
                start, end = 1, total
            else:
                start = 1
                end = prefix_newlines if prefix_ends_nl else prefix_newlines + 1
        elif action.get("from_end") is not None:
            n = int(action["from_end"])
            if n <= 0:
                return None
            end = max(1, last_real)
            start = max(1, end - n + 1)
        else:
            start = action.get("start")
            end = action.get("end")
            start = int(start) if start is not None else 1
            end = int(end) if end is not None else total
        start = max(1, start)
        if start > total:
            return None
        end = min(end, total)
        if end < start:
            return None
        out = dict(action)
        out["start"] = start
        out["end"] = end
        out["total_lines"] = total
        return out
    except Exception:
        return None


def hits(action: dict, stdout: str, cwd: str, limit: int = 1000) -> list[str]:
    """For search/list actions: absolute paths of existing regular files named in stdout,
    deduped, in order, capped at `limit`."""
    try:
        if not isinstance(action, dict) or not isinstance(stdout, str) or not stdout.strip():
            return []
        limit = int(limit)
        if limit <= 0:
            return []
        op = action.get("op")
        base = action.get("cwd") or cwd
        if not isinstance(base, str) or not os.path.isabs(base):
            base = None
        return _Hits(action, base, limit).run(op, stdout)
    except Exception:
        return []


# ======================================================================== tokenising

def _skip_subst(s: str, i: int) -> int:
    """s[i] is just after an opening '('; return the index just after the matching ')'."""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
            continue
        if c == "`":
            j = s.find("`", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_word(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] not in " \t\n;&|()<>":
        if s[i] in "'\"":
            j = s.find(s[i], i + 1)
            i = n if j < 0 else j + 1
        elif s[i] == "\\":
            i += 2
        else:
            i += 1
    return i


def _skip_heredocs(s: str, i: int, pending: list):
    """Skip heredoc bodies starting at s[i]; returns (new index, body line count per heredoc)."""
    n = len(s)
    counts = []
    for delim, strip_tabs, _slot in pending:
        count = 0
        while i < n:
            j = s.find("\n", i)
            line = s[i:] if j < 0 else s[i:j]
            i = n if j < 0 else j + 1
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                break
            count += 1
        counts.append(count)
    return i, counts


def _prepare(s: str) -> str:
    """Quote-aware pre-pass before shlex: drops comments, line continuations, stderr redirects and
    heredoc bodies (never parsed as commands); turns unquoted newlines into ';'; replaces $(...),
    `...` and <(...) with the _SUB marker and heredocs / here-strings with _HEREDOC marker words;
    protects escaped operators (find's \\; and \\( )."""
    s = s.replace("\r\n", "\n")
    out: list[str] = []
    pending: list = []
    i, n = 0, len(s)

    def last_char() -> str:
        return out[-1][-1] if out and out[-1] else ""

    while i < n:
        c = s[i]
        if c == "\\":
            nxt = s[i + 1:i + 2]
            if nxt == "\n":
                i += 2
                continue
            if nxt and nxt in "();<>|&!":
                out.append("'\\" + nxt + "'")
            else:
                out.append(s[i:i + 2])
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ValueError("unbalanced single quote")
            out.append(s[i:j + 1])
            i = j + 1
            continue
        if c == '"':
            buf = ['"']
            i += 1
            closed = False
            while i < n:
                d = s[i]
                if d == "\\":
                    buf.append(s[i:i + 2])
                    i += 2
                elif d == '"':
                    buf.append('"')
                    i += 1
                    closed = True
                    break
                elif d == "$" and s.startswith("$(", i):
                    buf.append(_SUB)
                    i = _skip_subst(s, i + 2)
                elif d == "`":
                    j = s.find("`", i + 1)
                    buf.append(_SUB)
                    i = n if j < 0 else j + 1
                else:
                    buf.append(d)
                    i += 1
            if not closed:
                raise ValueError("unbalanced double quote")
            out.append("".join(buf))
            continue
        if c == "#" and (not out or last_char() in _WORD_START):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "\n":
            i += 1
            if pending:
                i, counts = _skip_heredocs(s, i, pending)
                for (_d, _t, slot), count in zip(pending, counts):
                    out[slot] = " %sH%d " % (_HEREDOC, count)
                pending = []
            tail = "".join(out[-40:]).rstrip()
            if not tail or tail.endswith(("|", "&&")) or tail.endswith(";"):
                out.append(" ")
            else:
                out.append(" ; ")
            continue
        if c == "$" and s.startswith("$(", i):
            out.append(_SUB)
            i = _skip_subst(s, i + 2)
            continue
        if c == "`":
            j = s.find("`", i + 1)
            out.append(_SUB)
            i = n if j < 0 else j + 1
            continue
        if c in "<>" and s.startswith("(", i + 1):
            out.append(" " + _SUB + " ")
            i = _skip_subst(s, i + 2)
            continue
        if s.startswith("<<", i):
            if s.startswith("<<<", i):
                out.append(" %sS " % _HEREDOC)
                i += 3
                continue
            i += 2
            strip_tabs = s.startswith("-", i)
            if strip_tabs:
                i += 1
            while i < n and s[i] in " \t":
                i += 1
            j = i
            word = []
            while j < n and s[j] not in " \t\n;&|()<>":
                if s[j] in "'\"":
                    k = s.find(s[j], j + 1)
                    k = n if k < 0 else k
                    word.append(s[j + 1:k])
                    j = k + 1
                elif s[j] == "\\":
                    word.append(s[j + 1:j + 2])
                    j += 2
                else:
                    word.append(s[j])
                    j += 1
            pending.append(("".join(word), strip_tabs, len(out)))
            out.append(" %sH " % _HEREDOC)
            i = j
            continue
        if c.isdigit() and (not out or last_char() in _WORD_START):
            m = _FD_RE.match(s, i)
            if m:
                fd, op, dup = m.group(1), m.group(2), m.group(3) or ""
                if fd == "1" and op != "<":
                    out.append(" " + op + dup + " ")
                elif fd == "0" and op == "<":
                    out.append(" < ")
                else:  # stderr or another fd: irrelevant to what the agent sees on stdout
                    i = m.end()
                    if not dup:
                        while i < n and s[i] in " \t":
                            i += 1
                        i = _skip_word(s, i)
                    continue
                i = m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_ops(tok: str) -> list[str]:
    ops, i = [], 0
    while i < len(tok):
        for op in _OPS:
            if tok.startswith(op, i):
                ops.append(op)
                i += len(op)
                break
        else:
            i += 1
    return ops


def _segments(command: str) -> list[dict]:
    lex = shlex.shlex(_prepare(command), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    items = []
    for tok in lex:
        if tok and all(ch in _OPCHARS for ch in tok):
            items.extend(("op", o) for o in _split_ops(tok))
        else:
            items.append(("w", tok))

    segs: list[dict] = []

    def fresh():
        return {"kind": "cmd", "words": [], "redir": [], "herein": False, "herein_lines": None, "sep": None}

    cur = fresh()

    def flush(sep):
        nonlocal cur
        if cur["words"] or cur["redir"]:
            cur["sep"] = sep
            segs.append(cur)
        cur = fresh()

    in_test = False  # inside [[ ... ]], where < and > compare strings
    i = 0
    while i < len(items):
        kind, val = items[i]
        if kind == "w":
            if val.startswith(_HEREDOC + "H"):
                cur["herein"] = True
                cur["herein_lines"] = int(val[2:]) if val[2:].isdigit() else None
            elif val == _HEREDOC + "S":
                cur["herein"] = True
                if i + 1 < len(items) and items[i + 1][0] == "w":
                    cur["herein_lines"] = items[i + 1][1].count("\n") + 1
                    i += 1
            else:
                cur["words"].append(val)
                if val == "[[":
                    in_test = True
                elif val == "]]":
                    in_test = False
        elif in_test and val in ("<", ">"):
            cur["words"].append(val)
        elif val in _OUT_REDIRS or val in _IN_REDIRS:
            target = ""
            if i + 1 < len(items) and items[i + 1][0] == "w":
                target = items[i + 1][1]
                i += 1
            cur["redir"].append((val, target))
        elif val in _SEPS:
            in_test = False
            flush(val)
        elif val == "(":
            if i + 1 < len(items) and items[i + 1] == ("op", "("):  # (( arithmetic )): no redirects
                j = i + 2
                while j + 1 < len(items) and not (items[j] == ("op", ")") and items[j + 1] == ("op", ")")):
                    j += 1
                i = j + 2
                continue
            flush(";")
            segs.append({"kind": "open"})
        elif val == ")":
            flush(";")
            segs.append({"kind": "close"})
        i += 1
    flush(None)
    return segs


# ======================================================================== walking

class _State:
    def __init__(self, cwd):
        self.cwd = cwd
        self.oldpwd = None
        self.vars: dict[str, str] = {}
        self.stack: list = []
        self.dirstack: list = []


def _run(command: str, st: _State, depth: int) -> list[dict]:
    segs = _segments(command)
    out: list[dict] = []
    _walk(segs, 0, len(segs), st, depth, out)
    return out


def _first_word(words: list[str]) -> tuple[int, str]:
    k = 0
    while k < len(words) and words[k] in ("do", "then", "else", "{", "!"):
        k += 1
    return k, (words[k] if k < len(words) else "")


def _walk(segs, start, end, st: _State, depth: int, out: list) -> None:
    i = start
    while i < end and len(out) < _MAX_ACTIONS:
        seg = segs[i]
        if seg["kind"] == "open":
            st.stack.append(st.cwd)
            i += 1
            continue
        if seg["kind"] == "close":
            if st.stack:
                st.cwd = st.stack.pop()
            i += 1
            continue
        k, first = _first_word(seg["words"])
        if first == "for":
            done = _loop_end(segs, i + 1, end)
            if done is not None:
                _for_loop(seg["words"][k:], segs, i + 1, done, st, depth, out)
                out.extend(_command(segs[done], st)[1])  # `done > file`
                i = done + 1
                continue
        j = i
        while (j + 1 < end and segs[j]["kind"] == "cmd" and segs[j]["sep"] in ("|", "|&")
               and segs[j + 1]["kind"] == "cmd"):
            j += 1
        out.extend(_pipeline(segs[i:j + 1], st, depth))
        i = j + 1


def _loop_end(segs, start, end):
    level = 0
    for idx in range(start, end):
        seg = segs[idx]
        if seg["kind"] != "cmd" or not seg["words"]:
            continue
        w = seg["words"][0]
        if w == "do":
            level += 1
        elif w == "done":
            level -= 1
            if level <= 0:
                return idx
    return None


def _for_loop(header, segs, body_start, body_end, st, depth, out):
    values = None
    if len(header) >= 3 and header[2] == "in" and re.match(r"^[A-Za-z_]\w*$", header[1]):
        var = header[1]
        values = []
        for w in header[3:]:
            w = _subst(w, st)
            if _SUB in w or "$" in w:
                values = None
                break
            values.extend(_glob_rel(w, st.cwd))
    if not values:
        _walk(segs, body_start, body_end, st, depth, out)
        return
    saved = st.vars.get(var)
    for v in values[:_MAX_LOOP_VALUES]:
        st.vars[var] = v
        _walk(segs, body_start, body_end, st, depth, out)
        if len(out) >= _MAX_ACTIONS:
            break
    if saved is None:
        st.vars.pop(var, None)
    else:
        st.vars[var] = saved


def _subst(word: str, st: _State) -> str:
    if "$" not in word:
        return word

    def rep(m):
        # Shell-local assignments and loop variables first; then the hook's own environment, which
        # Claude Code shares with the Bash tool's shell (e.g. $HOME, or a storage dir set at launch).
        name = m.group(1) or m.group(3)
        if name in st.vars:
            return st.vars[name]
        if name == "PWD":
            return st.cwd or m.group(0)
        if name == "OLDPWD":
            return st.oldpwd or m.group(0)
        val = os.environ.get(name)
        if val:
            return val
        if m.group(1) and m.group(2) is not None:  # ${NAME:-default}
            return m.group(2)
        return m.group(0)

    return _VAR_RE.sub(rep, word)


def _glob_rel(word: str, cwd) -> list[str]:
    """Expand an unquoted glob the way the shell would, keeping relative words relative."""
    if not any(ch in word for ch in "*?[") or not word:
        return [word]
    w = os.path.expanduser(word) if word.startswith("~") else word
    if os.path.isabs(w):
        if os.path.lexists(w):
            return [word]
        found = sorted(glob.glob(w))[:_MAX_GLOB]
        return found or [word]
    if not cwd:
        return [word]
    if os.path.lexists(os.path.join(cwd, w)):
        return [word]
    prefix = cwd.rstrip(os.sep) + os.sep
    found = sorted(glob.glob(os.path.join(glob.escape(cwd), w)))[:_MAX_GLOB]
    rel = [f[len(prefix):] if f.startswith(prefix) else f for f in found]
    return rel or [word]


def _abspath(word: str, cwd):
    if not word or _SUB in word or "$" in word:
        return None
    if word.startswith("~"):
        word = os.path.expanduser(word)
    if os.path.isabs(word):
        return os.path.normpath(word)
    if not cwd:
        return None
    return os.path.normpath(os.path.join(cwd, word))


def _paths(words, cwd, expand=True) -> list[str]:
    out = []
    for w in words:
        if not w or w == "-" or _SUB in w or "$" in w:
            continue
        for x in (_glob_rel(w, cwd) if expand else [w]):
            p = _abspath(x, cwd)
            if p and p not in out:
                out.append(p)
    return out


def _isfile(p) -> bool:
    try:
        return stat.S_ISREG(os.stat(p).st_mode)
    except Exception:
        return False


def _canon(name: str) -> str:
    if name.startswith("\\"):
        name = name[1:]
    if "/" in name:
        name = os.path.basename(name)
    return _ALIASES.get(name, name)


def _skip_prefixes(words: list[str]):
    k, n = 0, len(words)
    while k < n:
        w = words[k]
        if _ASSIGN_RE.match(w) or w in _KEYWORDS:
            k += 1
        elif w == "env":
            k += 1
            while k < n and (words[k].startswith("-") or _ASSIGN_RE.match(words[k])):
                k += 2 if words[k] in ("-u", "-C", "-S", "--unset", "--chdir") else 1
        elif w == "command":
            if k + 1 < n and words[k + 1] in ("-v", "-V"):
                return None
            k += 1
            while k < n and words[k] == "-p":
                k += 1
        elif w == "time":
            k += 1
            while k < n and words[k] == "-p":
                k += 1
        elif w == "nice":
            k += 1
            if k < n and words[k] == "-n":
                k += 2
            elif k < n and re.match(r"^-\d+$", words[k]):
                k += 1
        elif w == "timeout":
            k += 1
            while k < n and words[k].startswith("-"):
                k += 2 if words[k] in ("-s", "-k", "--signal", "--kill-after") else 1
            k += 1  # duration
        elif w == "sudo":
            k += 1
            while k < n and words[k].startswith("-"):
                k += 2 if words[k] in ("-u", "-g", "-C", "-h", "-p") else 1
        else:
            break
    return k


def _set_vars(words: list[str], st: _State) -> None:
    for w in words:
        m = _ASSIGN_RE.match(w)
        if not m:
            continue
        name = w.split("=", 1)[0].rstrip("+")
        val = w.split("=", 1)[1]
        if _SUB in val or "$" in val or "[" in name:
            st.vars.pop(name, None)
        else:
            st.vars[name] = val


def _cd(name: str, args: list[str], st: _State, unknown: bool) -> None:
    if name == "popd":
        if st.dirstack:
            st.cwd = st.dirstack.pop()
        return
    args = [a for a in args if a not in ("-L", "-P", "-e", "-@", "--")]
    if name == "pushd":
        if not args:
            return
        st.dirstack.append(st.cwd)
    if unknown:
        new = None
    elif not args:
        new = os.path.expanduser("~")
    elif args[0] == "-":
        new = st.oldpwd
    else:
        new = _abspath(args[0], st.cwd)
    st.oldpwd = st.cwd
    st.cwd = new


def _command(seg: dict, st: _State):
    """(command dict or None, write actions for its redirects) for one simple command."""
    words = [_subst(w, st) for w in seg["words"]]
    redir = [(op, _subst(t, st)) for op, t in seg["redir"]]
    skip = any(_SUB in w for w in words) or any(_SUB in t for _, t in redir)
    herein, herein_lines = seg.get("herein", False), seg.get("herein_lines")
    assign_only = bool(words) and all(_ASSIGN_RE.match(w) for w in words)
    k = None if assign_only else _skip_prefixes(words)
    name = _canon(words[k]) if k is not None and k < len(words) else ""
    args = words[k + 1:] if name else []
    writes = _redirect_writes(name, args, redir, st.cwd, herein_lines)
    if assign_only:
        _set_vars(words, st)
        return None, writes
    if not name:
        return None, writes
    if name in ("export", "local", "declare", "readonly", "typeset"):
        _set_vars(args, st)
        return None, writes
    if name in ("cd", "pushd", "popd"):
        _cd(name, args, st, unknown=skip or any("$" in a for a in args[:1]))
        return None, writes
    stdin = None
    for op, target in redir:
        if op == "<":
            stdin = _abspath(target, st.cwd)
    return {
        "name": name,
        "args": args,
        "skip": skip,
        "redirected": any(op in _OUT_REDIRS for op, _ in redir),
        "stdin": stdin,
        "herein": herein,
        "herein_lines": herein_lines,
        "cwd": st.cwd,
    }, writes


def _pipeline(pipe: list[dict], st: _State, depth: int) -> list[dict]:
    parsed = [_command(seg, st) for seg in pipe]
    cmds = [c for c, _ in parsed]
    last_redirect = max((k for k, c in enumerate(cmds) if c and c["redirected"]), default=-1)
    out: list[dict] = []
    upstream_scope = None
    for k, (c, redirect_writes) in enumerate(parsed):
        prev_scope, upstream_scope = upstream_scope, None
        if c is not None and not c["skip"] and k > last_redirect:
            acts = _actions(c, k > 0, prev_scope, st, depth)
            lists = [a for a in acts if a["op"] == "list"]
            if lists:
                upstream_scope = lists[0]["scope"]
            if k + 1 < len(cmds) and _is_xargs_search(cmds[k + 1]):
                acts = [a for a in acts if a["op"] != "list"]
            if acts and all(a["op"] == "read" for a in acts) and k + 1 < len(cmds):
                acts = _apply_filters(acts, cmds[k + 1:], c["cwd"])
            out.extend(acts)
        if c is not None:
            out.extend(_command_writes(c, cmds[k - 1] if k > 0 else None))
        out.extend(redirect_writes)
    return out


# ======================================================================== writes

def _write(cmd, path, mode, lines=None):
    return {"op": "write", "cmd": cmd or "sh", "path": path, "mode": mode, "lines": lines}


def _write_paths(words, cwd, expand=True) -> list[str]:
    return [p for p in _paths(words, cwd, expand) if p != "/dev" and not p.startswith(("/dev/", "/proc/"))]


def _redirect_writes(name, args, redir, cwd, herein_lines) -> list[dict]:
    targets = []
    for op, target in redir:
        if op in (">>", "&>>"):
            mode = "append"
        elif op in (">", ">|", "&>") or (op == ">&" and target and not re.match(r"^(\d+|-)$", target)):
            mode = "overwrite"
        else:
            continue
        for p in _write_paths([target], cwd, expand=False):
            targets.append((p, mode))
    if not targets:
        return []
    lines = _output_lines(name, args, herein_lines)
    return [_write(name, p, mode, lines) for p, mode in targets]


def _output_lines(name, args, herein_lines):
    """Lines a command prints, when the command text alone says so; else None."""
    try:
        if name == "cat":
            return herein_lines if not [a for a in args if not a.startswith("-")] else None
        if name == "echo":
            return _echo_lines(args)
        if name == "printf":
            return _printf_lines(args)
    except Exception:
        pass
    return None


def _echo_lines(args):
    flags, k = "", 0
    while k < len(args) and re.match(r"^-[neE]+$", args[k]):
        flags += args[k][1:]
        k += 1
    text = " ".join(args[k:])
    if _SUB in text or "$" in text:
        return None
    if "\\" in text:
        if "e" in flags:
            text = text.replace("\\n", "\n")
        elif "E" not in flags:
            return None  # shells disagree on escapes (zsh's echo expands them, bash's does not)
    if "n" in flags:
        return text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return text.count("\n") + 1


def _printf_lines(args):
    if args and args[0] == "--":
        args = args[1:]
    if not args or args[0].startswith("-"):
        return None
    fmt, rest = args[0], args[1:]
    if any(_SUB in a or "$" in a for a in args) or any("\n" in a or "\\" in a for a in rest):
        return None
    convs = len(re.findall(r"%[-+ #0-9.]*[a-zA-Z]", fmt.replace("%%", "")))
    reps = -(-len(rest) // convs) if convs and rest else 1
    text = fmt.replace("\\n", "\n")
    if not text:
        return 0
    return reps * text.count("\n") + (0 if text.endswith("\n") else 1)


def _sed_inplace_files(args) -> list[str]:
    """File operands of `sed -i` / `sed -i ''` / `sed -i.bak` / `sed --in-place`; [] if not in-place."""
    inplace, scripts, pos = False, 0, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--"):
            if a.startswith("--in-place"):
                inplace = True
            elif a in ("--expression", "--file", "--line-length"):
                scripts += a != "--line-length"
                i += 1
            elif a.startswith(("--expression=", "--file=")):
                scripts += 1
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                ch = a[j]
                if ch == "i":
                    inplace = True
                    # BSD takes the backup suffix as the next word: -i '' or -i .bak
                    if j + 1 == len(a) and i + 1 < len(args) and re.match(r"^(|\.[\w.~-]*)$", args[i + 1]):
                        i += 1
                    break
                if ch in "efl":
                    scripts += ch != "l"
                    if j + 1 == len(a):
                        i += 1
                    break
                j += 1
        else:
            pos.append(a)
        i += 1
    if not inplace:
        return []
    return pos if scripts else pos[1:]


def _perl_inplace_files(args) -> list[str]:
    inplace, code, pos = False, False, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if not a.startswith("-") or len(a) < 2:
            pos.extend(args[i:])  # perl stops option parsing at the first operand
            break
        j = 1
        while j < len(a):
            ch = a[j]
            if ch == "i":
                inplace = True
                break
            if ch in "eE":
                code = True
                if j + 1 == len(a):
                    i += 1
                break
            if ch in "l0":
                j += 1
                while j < len(a) and a[j].isdigit():
                    j += 1
                continue
            if ch in "MmIxdDCFV":
                break
            j += 1
        i += 1
    if not inplace:
        return []
    return pos if code else pos[1:]


def _copy_move_writes(name, args, cwd) -> list[dict]:
    opts, pos = _parse_opts(args, "tS", {"--target-directory", "--suffix"})
    mode = "copy" if name == "cp" else "move"
    target = next((v for f, v in opts if f in ("-t", "--target-directory") and v), None)
    if target:
        dst, srcs, into_dir = target, pos, True
    else:
        if len(pos) < 2:
            return []
        dst, srcs = pos[-1], pos[:-1]
        into_dir = None
    found = _write_paths([dst], cwd, expand=False)
    if not found:
        return []
    dst_abs = found[0]
    if into_dir is None:
        no_target_dir = any(f in ("-T", "--no-target-directory") for f, _ in opts)
        into_dir = not no_target_dir and os.path.isdir(dst_abs)
    if not into_dir:
        return [_write(name, dst_abs, mode)]
    out, seen = [], set()
    for src in _paths(srcs, cwd):
        p = os.path.join(dst_abs, os.path.basename(src))
        if p not in seen:
            seen.add(p)
            out.append(_write(name, p, mode))
    return out


def _command_writes(c, prev) -> list[dict]:
    """Writes performed by the command itself (not by a shell redirect)."""
    name, args, cwd = c["name"], c["args"], c["cwd"]
    try:
        if name == "tee":
            opts, pos = _parse_opts(args, "", ())
            mode = "append" if any(f in ("-a", "--append") for f, _ in opts) else "overwrite"
            if c["herein"]:
                lines = c["herein_lines"]
            elif prev is not None:
                lines = _output_lines(prev["name"], prev["args"], prev["herein_lines"])
            else:
                lines = None
            return [_write("tee", p, mode, lines) for p in _write_paths(pos, cwd)]
        if name == "sed":
            return [_write("sed", p, "in-place") for p in _write_paths(_sed_inplace_files(args), cwd)]
        if name == "perl":
            return [_write("perl", p, "in-place") for p in _write_paths(_perl_inplace_files(args), cwd)]
        if name in ("cp", "mv"):
            return _copy_move_writes(name, args, cwd)
        if name == "touch":
            _, pos = _parse_opts(args, "dtrA", {"--date", "--reference", "--time"})
            return [_write("touch", p, "touch") for p in _write_paths(pos, cwd)]
    except Exception:
        return []
    return []


# ======================================================================== option parsing

def _parse_opts(args, short_val="", long_val=()):
    """Generic getopt-ish split: returns ([(flag, value)], positionals)."""
    opts, pos = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--") and len(a) > 2:
            if "=" in a:
                name, val = a.split("=", 1)
                opts.append((name, val))
            elif a in long_val and i + 1 < len(args):
                opts.append((a, args[i + 1]))
                i += 1
            else:
                opts.append((a, None))
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                ch = a[j]
                if ch in short_val:
                    val = a[j + 1:]
                    if not val and i + 1 < len(args):
                        val = args[i + 1]
                        i += 1
                    opts.append(("-" + ch, val))
                    break
                opts.append(("-" + ch, None))
                j += 1
        else:
            pos.append(a)
        i += 1
    return opts, pos


def _int(v):
    try:
        v = str(v).strip()
        m = re.match(r"^([+-]?\d+)([kKmMbB]?)$", v)
        if not m:
            return None
        return int(m.group(1)) * {"": 1, "b": 512, "k": 1024, "m": 1048576}[m.group(2).lower()]
    except Exception:
        return None


def _head_spec(args):
    n, nbytes, files = 10, None, []
    i = 0
    while i < len(args):
        a = args[i]
        val = None
        if a == "--":
            files.extend(args[i + 1:])
            break
        if re.match(r"^-\d+$", a):
            n = int(a[1:])
        elif a in ("-n", "--lines"):
            val, i = (args[i + 1] if i + 1 < len(args) else ""), i + 1
            n = _int(val)
        elif a.startswith("--lines="):
            n = _int(a.split("=", 1)[1])
        elif a.startswith("-n"):
            n = _int(a[2:])
        elif a in ("-c", "--bytes"):
            val, i = (args[i + 1] if i + 1 < len(args) else ""), i + 1
            nbytes = _int(val)
        elif a.startswith("--bytes="):
            nbytes = _int(a.split("=", 1)[1])
        elif a.startswith("-c"):
            nbytes = _int(a[2:])
        elif a.startswith("-") and a != "-":
            pass
        else:
            files.append(a)
        i += 1
    if n is not None and n < 0:
        n = None  # GNU "all but the last N": treat as the whole file
    return {"n": n, "bytes": nbytes, "files": files}


def _tail_spec(args):
    from_end, start, files = 10, None, []
    i = 0
    while i < len(args):
        a = args[i]
        val = None
        if a == "--":
            files.extend(args[i + 1:])
            break
        if re.match(r"^-\d+$", a):
            val = a[1:]
        elif re.match(r"^\+\d+$", a) and not files:
            val = a
        elif a in ("-n", "--lines"):
            val, i = (args[i + 1] if i + 1 < len(args) else ""), i + 1
        elif a.startswith("--lines="):
            val = a.split("=", 1)[1]
        elif a.startswith("-n"):
            val = a[2:]
        elif a in ("-c", "--bytes") or a.startswith(("-c", "--bytes=")):
            return None
        elif a.startswith("-") and a != "-":
            pass
        else:
            files.append(a)
        if val is not None:
            v = val.strip()
            if v.startswith("+") and _int(v[1:]) is not None:
                start, from_end = max(1, _int(v[1:])), None
            elif _int(v) is not None:
                from_end, start = abs(_int(v)), None
        i += 1
    return {"from_end": from_end, "start": start, "files": files}


_SED_RANGE = re.compile(r"^(\d+|\$)\s*(?:,\s*(\d+|\$|\+\d+))?\s*p$")


def _sed_spec(args):
    """None unless this is `sed -n` with only line-number print commands."""
    opts, pos = _parse_opts(args, "efl", {"--expression", "--file", "--line-length"})
    flags = {f for f, _ in opts}
    if flags & {"-i", "--in-place", "-f", "--file"}:
        return None
    if not flags & {"-n", "--quiet", "--silent"}:
        return None
    scripts = [v for f, v in opts if f in ("-e", "--expression") and v is not None]
    if not scripts:
        if not pos:
            return None
        scripts = [pos.pop(0)]
    ranges = []
    for script in scripts:
        for part in re.split(r"[;\n]", script):
            part = part.strip()
            if not part or re.match(r"^\d*\s*q$", part):
                continue
            m = _SED_RANGE.match(part)
            if not m:
                return None
            a, b = m.group(1), m.group(2)
            if a == "$":
                ranges.append((None, None, 1))
            elif b is None:
                ranges.append((int(a), int(a), None))
            elif b == "$":
                ranges.append((int(a), None, None))
            elif b.startswith("+"):
                ranges.append((int(a), int(a) + int(b[1:]), None))
            elif int(b) >= int(a):
                ranges.append((int(a), int(b), None))
            else:
                ranges.append((int(a), int(a), None))
    if not ranges:
        return None
    return {"ranges": ranges, "files": pos}


_AWK_COND = r"F?NR\s*(==|>=|<=|>|<)\s*(\d+)"
_AWK_RE = re.compile(
    r"^\s*\(?\s*" + _AWK_COND + r"(?:\s*&&\s*" + _AWK_COND + r")?\s*\)?\s*"
    r"(?:\{[^{}]*\})?\s*;?\s*(?:F?NR\s*(?:>=|>|==)\s*\d+\s*\{\s*exit\s*;?\s*\}\s*;?\s*)?$")
_AWK_PAIR_RE = re.compile(r"^\s*F?NR\s*==\s*(\d+)\s*,\s*F?NR\s*==\s*(\d+)\s*(?:\{[^{}]*\})?\s*$")


def _awk_spec(args):
    opts, pos = _parse_opts(args, "Fvf", {"--field-separator", "--assign", "--file"})
    if any(f in ("-f", "--file") for f, _ in opts) or not pos:
        return None
    prog = pos.pop(0)
    files = [p for p in pos if not _ASSIGN_RE.match(p)]
    m = _AWK_PAIR_RE.match(prog)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return {"ranges": [(a, max(a, b), None)], "files": files}
    m = _AWK_RE.match(prog)
    if not m:
        return None
    start, end = 1, None
    for op, val in ((m.group(1), m.group(2)), (m.group(3), m.group(4))):
        if not op:
            continue
        v = int(val)
        if op == "==":
            start, end = v, v
        elif op == ">=":
            start = max(start, v)
        elif op == ">":
            start = max(start, v + 1)
        elif op == "<=":
            end = v if end is None else min(end, v)
        elif op == "<":
            end = v - 1 if end is None else min(end, v - 1)
    if end is not None and end < start:
        return None
    return {"ranges": [(start, end, None)], "files": files}


def _whole_spec(name, args):
    """(files, ranges) for cat/nl/bat/less/more."""
    ranges = [(None, None, None)]
    if name == "cat":
        files, rest = [], False
        for a in args:
            if rest or a == "-" or not a.startswith("-"):
                files.append(a)
            elif a == "--":
                rest = True
        return files, ranges
    if name in ("less", "more"):
        return [a for a in args if not a.startswith(("-", "+")) or a == "-"], ranges
    if name == "nl":
        _, pos = _parse_opts(args, "bdfhilnsvw", {"--body-numbering", "--section-delimiter",
                                                  "--footer-numbering", "--header-numbering",
                                                  "--line-increment", "--join-blank-lines",
                                                  "--number-format", "--number-separator",
                                                  "--starting-line-number", "--number-width"})
        return pos, ranges
    opts, pos = _parse_opts(args, "rlHm", _BAT_LONG)
    rng = []
    for f, v in opts:
        if f in ("-r", "--line-range") and v:
            m = re.match(r"^(\d*):(\+?\d*)$", v)
            if m:
                a = int(m.group(1)) if m.group(1) else 1
                b = m.group(2)
                if not b:
                    rng.append((a, None, None))
                elif b.startswith("+"):
                    rng.append((a, a + int(b[1:]), None))
                else:
                    rng.append((a, int(b), None))
    return pos, rng or ranges


def _search_spec(name, args, cwd):
    """Parse grep/rg/ag (or `git` with a grep subcommand). None when not a search."""
    if name == "grep":
        opts, pos = _parse_opts(args, "efmABCdD", _GREP_LONG)
        flags = {f for f, _ in opts}
        if flags & {"-V", "--version", "--help"}:
            return None
        return _finish_spec("grep", opts, pos, {"-e", "--regexp"}, {"-f", "--file"},
                            {"-l", "-L", "-c", "--files-with-matches", "--files-without-match", "--count"},
                            recursive=bool(flags & {"-r", "-R", "--recursive", "--dereference-recursive"}))
    if name == "rg":
        opts, pos = _parse_opts(args, "efgtTmABCMjErd", _RG_LONG)
        flags = {f for f, _ in opts}
        if flags & {"--type-list", "-V", "--version", "-h", "--help"}:
            return None
        if "--files" in flags:
            return {"kind": "list", "cmd": "rg --files", "paths": pos}
        return _finish_spec("rg", opts, pos, {"-e", "--regexp"}, {"-f", "--file"},
                            {"-l", "-c", "--files-with-matches", "--files-without-match", "--count",
                             "--count-matches"}, recursive=True)
    if name == "ag":
        opts, pos = _parse_opts(args, "GgmABCp", _AG_LONG)
        glist = [v for f, v in opts if f == "-g" and v is not None]
        if glist:
            return {"kind": "search", "cmd": "ag", "pattern": glist[0], "paths": pos,
                    "files_only": True, "recursive": True}
        return _finish_spec("ag", opts, pos, set(), set(),
                            {"-l", "-L", "-c", "--files-with-matches", "--files-without-matches", "--count"},
                            recursive=True)
    if name == "git":
        return _git_grep_spec(args, cwd)
    return None


def _finish_spec(cmd, opts, pos, pat_flags, file_flags, fo_flags, recursive):
    flags = {f for f, _ in opts}
    patterns = [v for f, v in opts if f in pat_flags and v is not None]
    if patterns:
        pattern = "|".join(patterns)
    elif flags & file_flags:
        pattern = ""
    elif pos:
        pattern = pos.pop(0)
    else:
        return None
    return {"kind": "search", "cmd": cmd, "pattern": pattern, "paths": pos,
            "files_only": bool(flags & fo_flags), "recursive": recursive}


def _git_grep_spec(args, cwd):
    i, gcwd = 0, cwd
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            gcwd = _abspath(args[i + 1], gcwd)
            i += 2
        elif a == "-c" and i + 1 < len(args):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    if i >= len(args) or args[i] != "grep":
        return None
    rest = args[i + 1:]
    after = []
    if "--" in rest:
        idx = rest.index("--")
        rest, after = rest[:idx], rest[idx + 1:]
    opts, pos = _parse_opts(rest, "efmABC", _GITGREP_LONG)
    spec = _finish_spec("git grep", opts, pos, {"-e", "--regexp"}, {"-f", "--file"},
                        {"-l", "-L", "-c", "--name-only", "--files-with-matches",
                         "--files-without-match", "--count"}, recursive=True)
    if spec is None:
        return None
    scope = []
    for w in spec["paths"]:  # revisions or paths: keep only what exists on disk
        p = _abspath(w, gcwd)
        if p and os.path.exists(p):
            scope.append(p)
    for w in after:
        if not any(ch in w for ch in "*?[:"):
            p = _abspath(w, gcwd)
            if p:
                scope.append(p)
    spec["paths"] = []
    spec["abs_scope"] = scope
    spec["cwd"] = gcwd
    return spec


# ======================================================================== actions

def _read(cmd, path, start=None, end=None, from_end=None):
    return {"op": "read", "cmd": cmd, "path": path, "start": start, "end": end, "from_end": from_end}


def _search_action(spec, cwd, piped_in, stdin, inherited=None):
    cwd = spec.get("cwd", cwd)
    scope = spec.get("abs_scope") or _paths(spec["paths"], cwd)
    single_ok = True
    if not scope:
        if inherited is not None:
            scope, single_ok = list(inherited), False
        elif stdin:
            scope = [stdin]
        elif piped_in:
            return None
        elif spec["cmd"] == "grep" and not spec["recursive"]:
            return None
        elif cwd:
            scope = [cwd]
        else:
            return None
    return {"op": "search", "cmd": spec["cmd"], "pattern": spec["pattern"], "scope": scope,
            "files_only": bool(spec["files_only"]),
            "single_file": bool(single_ok and len(scope) == 1 and _isfile(scope[0])),
            "cwd": cwd}


def _list_action(cmd, scope, cwd):
    if not scope:
        if not cwd:
            return []
        scope = [cwd]
    return [{"op": "list", "cmd": cmd, "scope": scope, "cwd": cwd}]


def _xargs_inner(args):
    i, repl = 0, None
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            if "=" not in a and a in ("--max-args", "--max-procs", "--delimiter", "--arg-file",
                                      "--max-chars", "--max-lines"):
                i += 1
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            ch = a[1]
            if ch in "IJLnPsdEaR":
                val = a[2:]
                if not val and i + 1 < len(args):
                    val = args[i + 1]
                    i += 1
                if ch in "IJ":
                    repl = val
            elif ch == "i":
                repl = a[2:] or "{}"
            i += 1
            continue
        break
    inner = args[i:]
    if repl:
        inner = [w for w in inner if w != repl]
    return [w for w in inner if w != "{}"]


def _is_xargs_search(c) -> bool:
    if not c or c["skip"] or c["name"] != "xargs":
        return False
    inner = _xargs_inner(c["args"])
    if not inner:
        return False
    k = _skip_prefixes(inner)
    return k is not None and k < len(inner) and _canon(inner[k]) in _SEARCHERS | {"git"}


def _actions(c, piped_in, upstream_scope, st, depth) -> list[dict]:
    name, args, cwd = c["name"], c["args"], c["cwd"]
    stdin = c["stdin"]
    pipe_in = bool(piped_in or c.get("herein")) and not stdin

    def files_or_stdin(words):
        paths = _paths(words, cwd)
        if not paths and stdin and not [w for w in words if w != "-"]:
            paths = [stdin]
        return paths

    if name in _WHOLE_READERS:
        files, ranges = _whole_spec(name, args)
        return [_read(name, p, s, e, fe) for p in files_or_stdin(files) for s, e, fe in ranges]

    if name == "head":
        spec = _head_spec(args)
        if spec["n"] == 0 and spec["bytes"] is None:
            return []
        acts = []
        for p in files_or_stdin(spec["files"]):
            if spec["bytes"] is not None:
                if spec["bytes"] > 0:
                    a = _read("head", p, 1, None)
                    a["bytes"] = spec["bytes"]
                    acts.append(a)
            elif spec["n"] is None:
                acts.append(_read("head", p))
            else:
                acts.append(_read("head", p, 1, spec["n"]))
        return acts

    if name == "tail":
        spec = _tail_spec(args)
        if spec is None or spec["from_end"] == 0:
            return []
        return [_read("tail", p, spec["start"], None, spec["from_end"]) for p in files_or_stdin(spec["files"])]

    if name in ("sed", "awk"):
        spec = _sed_spec(args) if name == "sed" else _awk_spec(args)
        if spec is None:
            return []
        return [_read(name, p, s, e, fe) for p in files_or_stdin(spec["files"]) for s, e, fe in spec["ranges"]]

    if name in _SEARCHERS or name == "git":
        spec = _search_spec(name, args, cwd)
        if spec is None:
            return []
        if spec["kind"] == "list":
            return _list_action(spec["cmd"], _paths(spec["paths"], cwd), cwd)
        act = _search_action(spec, cwd, pipe_in, stdin)
        return [act] if act else []

    if name == "xargs":
        if not piped_in and not stdin and not c.get("herein"):
            return []
        inner = _xargs_inner(args)
        k = _skip_prefixes(inner) if inner else None
        if k is None or k >= len(inner):
            return []
        iname = _canon(inner[k])
        if iname not in _SEARCHERS and iname != "git":
            return []
        spec = _search_spec(iname, inner[k + 1:], cwd)
        if not spec or spec["kind"] != "search":
            return []
        act = _search_action(spec, cwd, False, None, inherited=upstream_scope or ([cwd] if cwd else None))
        return [act] if act else []

    if name == "find":
        return _find_actions(args, cwd)

    if name == "fd":
        cut = len(args)
        for idx, a in enumerate(args):
            if a in ("-x", "--exec", "-X", "--exec-batch"):
                cut = idx
                break
        opts, pos = _parse_opts(args[:cut], "etEdSjco", _FD_LONG)
        extra = [v for f, v in opts if f == "--search-path" and v]
        return _list_action("fd", _paths(pos[1:] + extra, cwd), cwd)

    if name == "ls":
        _, pos = _parse_opts(args, "", _LS_LONG)
        return _list_action("ls", _paths(pos, cwd), cwd)

    if name == "tree":
        _, pos = _parse_opts(args, "LPIoHT", _TREE_LONG)
        return _list_action("tree", _paths(pos, cwd), cwd)

    if name in ("bash", "sh", "zsh", "dash") and depth < _MAX_DEPTH:
        script = None
        for idx, a in enumerate(args):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                script = next((w for w in args[idx + 1:] if not w.startswith("-")), None)
                break
            if not a.startswith("-"):
                break
        if script:
            saved = (st.cwd, dict(st.vars))
            try:
                return _run(script, st, depth + 1)
            except Exception:
                return []
            finally:
                st.cwd, st.vars = saved
    return []


def _find_actions(args, cwd):
    i, scope_words = 0, []
    while i < len(args):
        a = args[i]
        if a in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x") or re.match(r"^-O\d$", a):
            i += 1
        elif a == "-D":
            i += 2
        elif a == "-f" and i + 1 < len(args):
            scope_words.append(args[i + 1])
            i += 2
        else:
            break
    while i < len(args):
        a = args[i]
        if a.startswith("-") or a in ("(", ")", "!", "\\(", "\\)", "\\!", ","):
            break
        scope_words.append(a)
        i += 1
    scope = _paths(scope_words, cwd) or ([cwd] if cwd else [])
    if not scope:
        return []
    expr = args[i:]
    for idx, a in enumerate(expr):
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            inner = []
            for b in expr[idx + 1:]:
                if b in (";", "\\;", "+"):
                    break
                inner.append(b)
            inner = [w for w in inner if w != "{}"]
            if inner and _canon(inner[0]) in _SEARCHERS:
                spec = _search_spec(_canon(inner[0]), inner[1:], cwd)
                if spec and spec["kind"] == "search":
                    act = _search_action(spec, cwd, False, None, inherited=scope)
                    if act:
                        return [act]
    return _list_action("find", scope, cwd)


# ======================================================================== pipelines

def _filter_kind(c):
    """What a downstream pipeline stage does to the stream it receives."""
    if c is None:
        return ("pass",)
    if c["skip"]:
        return ("hide",)
    name, args = c["name"], c["args"]
    if c["stdin"] or c.get("herein"):
        return ("hide",)  # `< file` or a heredoc replaces the pipe
    if name == "head":
        spec = _head_spec(args)
        if spec["files"]:
            return ("hide",)
        if spec["bytes"] is not None:
            return ("bytes", spec["bytes"])
        return ("head", spec["n"])
    if name == "tail":
        spec = _tail_spec(args)
        if spec is None:
            return ("pass",)
        if spec["files"]:
            return ("hide",)
        if spec["start"] is not None:
            return ("tail+", spec["start"])
        return ("tail", spec["from_end"])
    if name in ("sed", "awk"):
        spec = _sed_spec(args) if name == "sed" else _awk_spec(args)
        if spec is None:
            return ("pass",)
        if spec["files"]:
            return ("hide",)
        rs = spec["ranges"]
        if any(fe is not None for _, _, fe in rs):
            return ("tail", 1) if len(rs) == 1 else ("pass",)
        lo = min(s for s, _, _ in rs)
        hi = None if any(e is None for _, e, _ in rs) else max(e for _, e, _ in rs)
        return ("range", lo, hi)
    if name in _SEARCHERS:
        spec = _search_spec(name, args, c["cwd"])
        if spec is None or spec["kind"] != "search":
            return ("pass",)
        if spec["paths"]:
            return ("hide",)
        return ("search", spec)
    if name in _HIDERS or name in _WHOLE_READERS and [a for a in args if not a.startswith("-")]:
        return ("hide",)
    return ("pass",)


def _apply_filters(reads: list[dict], downstream: list, cwd) -> list[dict]:
    reads = [dict(r) for r in reads]
    for c in downstream:
        kind = _filter_kind(c)
        tag = kind[0]
        if tag == "pass":
            continue
        if tag == "hide":
            return []
        if tag == "search":
            spec = kind[1]
            scope = []
            for r in reads:
                if r["path"] not in scope:
                    scope.append(r["path"])
            return [{"op": "search", "cmd": spec["cmd"], "pattern": spec["pattern"], "scope": scope,
                     "files_only": bool(spec["files_only"]),
                     "single_file": len(scope) == 1 and _isfile(scope[0]), "cwd": cwd}]
        if tag in ("head", "bytes"):
            if kind[1] is None:
                continue
            if kind[1] <= 0:
                return []
            reads = reads[:1]
            r = reads[0]
            if tag == "bytes":
                if r["start"] is None and r["end"] is None and r["from_end"] is None:
                    r["start"], r["bytes"] = 1, kind[1]
                continue
            if r["from_end"] is None:
                s = r["start"] or 1
                e = s + kind[1] - 1
                r["start"], r["end"] = s, (min(r["end"], e) if r["end"] is not None else e)
        elif tag == "tail":
            if not kind[1]:
                return []
            reads = reads[-1:]
            r = reads[0]
            if r["from_end"] is not None:
                r["from_end"] = min(r["from_end"], kind[1])
            elif r["end"] is not None:
                r["start"] = max(r["start"] or 1, r["end"] - kind[1] + 1)
            else:
                r["start"], r["end"], r["from_end"] = None, None, kind[1]
        elif tag == "tail+":
            for r in reads:
                if r["from_end"] is None:
                    r["start"] = (r["start"] or 1) + kind[1] - 1
            reads = [r for r in reads if r["end"] is None or r["start"] <= r["end"]]
        elif tag == "range":
            lo, hi = kind[1], kind[2]
            for r in reads:
                if r["from_end"] is not None:
                    continue
                s = r["start"] or 1
                ns = s + lo - 1
                ne = s + hi - 1 if hi is not None else r["end"]
                if r["end"] is not None and ne is not None:
                    ne = min(ne, r["end"])
                r["start"], r["end"] = ns, ne
            reads = [r for r in reads if r["end"] is None or r["start"] <= r["end"]]
        if not reads:
            return []
    return reads


# ======================================================================== hits

_LS_LONG_RE = re.compile(r"^[-bcdlpsDw?][-rwxsStTlL]{9}[@+.]?\s")
_TREE_RE = re.compile(r"^((?:(?:[│|]|\s)[\s\xa0]{3})*)(?:├──|└──|\|--|`--)[\s\xa0](.*)$")
_TREE_SUMMARY = re.compile(r"^\d+ director(?:y|ies)(?:, \d+ files?)?$")


class _Hits:
    def __init__(self, action, base, limit):
        self.action = action
        self.base = base
        self.limit = limit
        self.out: list[str] = []
        self.seen: set = set()
        self.cache: dict = {}

    def isfile(self, p):
        r = self.cache.get(p)
        if r is None:
            r = self.cache[p] = _isfile(p)
        return r

    def resolve(self, rel, base=None):
        rel = rel.strip() if rel else rel
        if not rel or "\x00" in rel:
            return None
        base = base or self.base
        if os.path.isabs(rel):
            p = rel
        elif base:
            p = os.path.join(base, rel)
        else:
            return None
        p = os.path.normpath(p)
        return p if self.isfile(p) else None

    def add(self, p):
        if p and p not in self.seen:
            self.seen.add(p)
            self.out.append(p)
        return len(self.out) >= self.limit

    def lines(self, stdout):
        text = stdout[:_MAX_STDOUT]
        return [ln.rstrip("\r") for ln in re.split(r"[\n\x00]", text)][:_MAX_LINES]

    def run(self, op, stdout):
        if op == "search":
            self.search(stdout)
        elif op == "list":
            cmd = self.action.get("cmd")
            if cmd == "ls":
                self.ls(stdout)
            elif cmd == "tree":
                self.tree(stdout)
            else:
                self.plain(stdout)
        return self.out[:self.limit]

    def search(self, stdout):
        a = self.action
        scope = a.get("scope") or []
        files_only = bool(a.get("files_only"))
        if a.get("single_file") and scope:
            text = stdout.strip()
            if text and not (files_only and text == "0") and self.isfile(scope[0]):
                self.add(scope[0])
            return
        for line in self.lines(stdout):
            ln = line.strip()
            if not ln or ln == "--":
                continue
            p = self.resolve(ln)
            if not p and files_only:
                m = re.match(r"^(.*):(\d+)$", ln)
                if m and m.group(2) != "0":
                    p = self.resolve(m.group(1))
            if not p and not files_only:
                p = self.content_prefix(ln)
            if p and self.add(p):
                return

    def content_prefix(self, ln):
        m = re.match(r"^Binary file (.+) matches$", ln)
        if m:
            return self.resolve(m.group(1))
        first = ln.find(":")
        if first > 0:
            p = self.resolve(ln[:first])
            if p:
                return p
            second = ln.find(":", first + 1)
            if second > 0:
                p = self.resolve(ln[:second])
                if p:
                    return p
        for m in re.finditer(r"-(\d+)-", ln):
            if m.start() > 0:
                p = self.resolve(ln[:m.start()])
                if p:
                    return p
        return None

    def plain(self, stdout):
        for line in self.lines(stdout):
            ln = line.strip()
            if not ln:
                continue
            p = self.resolve(ln)
            if not p and " " in ln:
                p = self.resolve(ln.split()[-1])
            if p and self.add(p):
                return

    def ls(self, stdout):
        scope = self.action.get("scope") or ([self.base] if self.base else [])
        cur = self.base
        if len(scope) == 1 and os.path.isdir(scope[0]):
            cur = scope[0]
        for line in self.lines(stdout):
            ln = line.rstrip()
            if not ln.strip() or ln.startswith("total "):
                continue
            if ln.endswith(":"):
                d = _abspath(ln[:-1], self.base)
                if d and os.path.isdir(d):
                    cur = d
                    continue
            names = [ln]
            if _LS_LONG_RE.match(ln):
                names = []
                for k in (8, 7):
                    parts = ln.split(None, k)
                    if len(parts) == k + 1:
                        names.append(parts[k].split(" -> ")[0])
                names.append(ln.split()[-1])
            p = None
            for name in names:
                for cand in (name, name.rstrip("*@=|")):
                    p = self.resolve(cand, cur) or self.resolve(cand, self.base)
                    if p:
                        break
                if p:
                    break
            if p and self.add(p):
                return

    def tree(self, stdout):
        scope = self.action.get("scope") or []
        root = scope[0] if scope else self.base
        stack: list[str] = []
        for line in self.lines(stdout):
            ln = line.rstrip()
            if not ln.strip() or _TREE_SUMMARY.match(ln.strip()):
                continue
            m = _TREE_RE.match(ln)
            if not m:
                p = self.resolve(ln.strip())
                if p and self.add(p):
                    return
                continue
            depth = len(m.group(1)) // 4
            name = m.group(2).split(" -> ")[0].strip()
            stack = stack[:depth] + [name]
            p = self.resolve(name) if "/" in name else None
            if not p and root:
                p = self.resolve(os.path.join(*stack), root) or self.resolve(
                    os.path.join(*(stack[:-1] + [name.rstrip("*@=|/")])), root)
            if p and self.add(p):
                return
