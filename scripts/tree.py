#!/usr/bin/env python3
"""read-write-monitor: static file listing for the Tree and Project views.

`list_tree(root)` returns every file under `root` (names only; file contents are
never read, apart from `.gitignore` rules in walk mode) so the dashboard can
draw the whole tree an agent runs in and compute "never touched" files.

Two sources, tried in order:

git   `git` is on PATH and `root` is inside a work tree. The listing is
      `git ls-files` for tracked files plus untracked files that are not
      excluded (.gitignore, .git/info/exclude, core.excludesFile), minus tracked
      files deleted from the working tree. Submodule entries (gitlinks) and
      untracked nested repositories are skipped because they are directories.
      Git's view is trusted for tracked files; DEFAULT_IGNORES only filters
      *untracked* files (so an un-ignored node_modules/ or .DS_Store does not
      flood the tree, but a committed build/ directory stays visible).
      Any failure or a ~20 s timeout falls back to walk mode.

walk  Iterative breadth-first os.scandir. Symlinked directories are never
      followed (and not listed); symlinks to files, and broken symlinks, are
      listed like git lists them. Unreadable directories are skipped. Stops
      after max(max_entries * 20, 100_000) files or ~3 s (complete=False).

Ignore rules (gitignore syntax), lowest to highest precedence:
  1. DEFAULT_IGNORES (walk mode; untracked files only in git mode)
  2. walk mode: `.gitignore` files, the root one first, deeper ones override
  3. extra ignores: the `extra_ignores` argument, then env RWM_IGNORE
     (comma-separated). These apply in both modes and can re-include with `!`.
Within one list the last matching pattern wins. A pattern with no match falls
through to the next lower list; no match anywhere means the file is kept.

Supported gitignore subset:
  - blank lines and `#` comments; `\\#` / `\\!` for a literal leading # or !
  - trailing unescaped spaces are stripped
  - `!pattern` negates (re-includes)
  - trailing `/` matches directories only
  - a leading `/` or a `/` in the middle anchors the pattern to the directory
    of its .gitignore (the root for default/extra ignores); otherwise the
    pattern matches the name at any depth below that directory
  - `*` and `?` (never match `/`), `[abc]`, `[a-z]`, `[!a]` / `[^a]`, `\\x`
  - `**`: leading `**/`, trailing `/**`, and `/**/` in the middle
  - an excluded directory is not descended into, so negating a path inside
    it has no effect (same as git)
Not supported: `[[:class:]]` POSIX classes, core.ignoreCase (matching is
case-sensitive), .git/info/exclude and global excludes in walk mode, .gitignore
files above `root`, commas inside RWM_IGNORE patterns.

Env: RWM_TREE_MAX (default 5000), RWM_IGNORE.
CLI: python3 scripts/tree.py <root> [--max N]   prints a JSON summary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_MAX_ENTRIES = 5000

# Gitignore-style patterns for directories and files that are never interesting
# to show as part of a project tree: VCS metadata, dependency and virtualenv
# directories, caches, build outputs, IDE state and OS litter. `.claude/` is
# deliberately absent: agent instructions and skills live there.
DEFAULT_IGNORES = (
    # version control metadata (no trailing slash: `.git` is a file in worktrees)
    ".git",
    ".hg/",
    ".svn/",
    # dependencies and virtual environments
    "node_modules/",
    "bower_components/",
    ".venv/",
    "venv/",
    # Python tooling caches and packaging leftovers
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".eggs/",
    "*.egg-info/",
    # build tools, bundlers and their outputs
    ".gradle/",
    "dist/",
    "build/",
    "target/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    ".turbo/",
    ".parcel-cache/",
    ".cache/",
    "coverage/",
    ".terraform/",
    # IDE state
    ".idea/",
    # OS litter
    ".DS_Store",
    "Thumbs.db",
    # compiled artifacts
    "*.pyc",
    "*.pyo",
    "*.class",
)

_WALK_FILE_FLOOR = 100_000  # walk hard limit is max(max_entries * 20, this)
_WALK_TIME_BUDGET = 3.0  # seconds
_GIT_TIMEOUT = 20.0  # seconds, across all git calls
_GITIGNORE_MAX_BYTES = 1 << 20

# Variables that would point git at some other repository than `root`'s.
_GIT_ENV_DROP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


# ---------------------------------------------------------------------------
# gitignore patterns


class _Rule:
    __slots__ = ("negate", "dir_only", "anchored", "source", "regex")

    def __init__(self, negate: bool, dir_only: bool, anchored: bool, source: str):
        self.negate = negate
        self.dir_only = dir_only
        self.anchored = anchored
        self.source = source
        self.regex = re.compile(source, re.DOTALL)


def _bracket(pat: str, i: int) -> Optional[Tuple[str, int]]:
    """Translate the `[...]` starting at pat[i]; None if it is not closed."""
    n = len(pat)
    j = i + 1
    negate = False
    if j < n and pat[j] in "!^":
        negate = True
        j += 1
    start = j
    if j < n and pat[j] == "]":
        j += 1
    while j < n and pat[j] != "]":
        if pat[j] == "\\":
            j += 1
        j += 1
    if j >= n:
        return None
    body = pat[start:j]
    items: List[str] = []
    k = 0
    while k < len(body):
        ch = body[k]
        if ch == "\\" and k + 1 < len(body):
            items.append(re.escape(body[k + 1]))
            k += 2
        elif ch == "-" and items and k + 1 < len(body):
            items.append("-")
            k += 1
        else:
            items.append(re.escape(ch))
            k += 1
    inner = "".join(items)
    if negate:
        return "[^" + inner + "/]", j + 1
    return "(?:(?!/)[" + inner + "])", j + 1


def _translate(pat: str) -> str:
    """Translate a gitignore glob (no leading/trailing slash) to a regex."""
    out: List[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            j = i
            while j < n and pat[j] == "*":
                j += 1
            if j - i >= 2 and (i == 0 or pat[i - 1] == "/"):
                if j == n:  # trailing `/**` (or a bare `**`): everything inside
                    out.append(".*")
                    i = j
                    continue
                if pat[j] == "/":  # leading `**/` or middle `/**/`: zero or more dirs
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
            out.append("[^/]*")
            i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            parsed = _bracket(pat, i)
            if parsed is None:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(parsed[0])
                i = parsed[1]
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _parse_pattern(line: str) -> Optional[_Rule]:
    line = line.rstrip("\r\n")
    while line.endswith(" ") and not line.endswith("\\ "):
        line = line[:-1]
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    if negate:
        line = line[1:]
    dir_only = line.endswith("/")
    if dir_only:
        line = line.rstrip("/")
    anchored = "/" in line
    if line.startswith("/"):
        line = line[1:]
    if not line:
        return None
    try:
        return _Rule(negate, dir_only, anchored, _translate(line))
    except re.error:
        return None


def _parse_patterns(lines: Sequence[str]) -> List[_Rule]:
    rules = []
    for line in lines:
        rule = _parse_pattern(line)
        if rule is not None:
            rules.append(rule)
    return rules


def _combine(rules: List[_Rule]):
    if not rules:
        return None
    return re.compile("|".join("(?:%s)" % r.source for r in rules), re.DOTALL)


class _RuleSet:
    """The patterns of one source (one .gitignore, the defaults, the extras).

    `base` is the directory (relative to root, "" for root) that anchored
    patterns are relative to and that the rules are scoped to.
    """

    def __init__(self, base: str, rules: List[_Rule]):
        self.cut = len(base) + 1 if base else 0
        self.rules = rules
        self.has_negation = any(r.negate for r in rules)
        if not self.has_negation:
            # Without negations any match means "ignored": one regex per case.
            self.name_file = _combine([r for r in rules if not r.anchored and not r.dir_only])
            self.name_dir = _combine([r for r in rules if not r.anchored])
            self.path_file = _combine([r for r in rules if r.anchored and not r.dir_only])
            self.path_dir = _combine([r for r in rules if r.anchored])

    def __bool__(self) -> bool:
        return bool(self.rules)

    def match(self, rel: str, name: str, is_dir: bool) -> Optional[bool]:
        """True = ignored, False = re-included, None = no rule matched."""
        sub = rel[self.cut:] if self.cut else rel
        if not self.has_negation:
            if is_dir:
                by_name, by_path = self.name_dir, self.path_dir
            else:
                by_name, by_path = self.name_file, self.path_file
            if (by_name is not None and by_name.fullmatch(name)) or (
                by_path is not None and by_path.fullmatch(sub)
            ):
                return True
            return None
        for rule in reversed(self.rules):
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.fullmatch(sub if rule.anchored else name):
                return not rule.negate
        return None


def _decide(rulesets: Sequence[_RuleSet], rel: str, name: str, is_dir: bool) -> bool:
    """`rulesets` is ordered highest precedence first."""
    for rs in rulesets:
        verdict = rs.match(rel, name, is_dir)
        if verdict is not None:
            return verdict
    return False


def _filter_paths(paths: Sequence[str], rulesets: Sequence[_RuleSet]) -> List[str]:
    """Apply rules to a flat path list, honouring excluded parent directories."""
    rulesets = [rs for rs in rulesets if rs]
    if not rulesets:
        return list(paths)
    dir_cache: Dict[str, bool] = {}
    kept = []
    for path in paths:
        parts = path.split("/")
        prefix = ""
        excluded = False
        for comp in parts[:-1]:
            prefix = prefix + "/" + comp if prefix else comp
            verdict = dir_cache.get(prefix)
            if verdict is None:
                verdict = _decide(rulesets, prefix, comp, True)
                dir_cache[prefix] = verdict
            if verdict:
                excluded = True
                break
        if not excluded and not _decide(rulesets, path, parts[-1], False):
            kept.append(path)
    return kept


_DEFAULT_RULESET = _RuleSet("", _parse_patterns(DEFAULT_IGNORES))


# ---------------------------------------------------------------------------
# git mode


def _git_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"  # never take index.lock just to list
    return env


def _run_git(git: str, root: str, args: List[str], deadline: float) -> Optional[List[str]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        proc = subprocess.run(
            [git, "-C", root] + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return [os.fsdecode(rec) for rec in proc.stdout.split(b"\0") if rec]


def _git_list(root: str) -> Optional[Tuple[List[str], List[str]]]:
    """(tracked files present on disk, untracked non-excluded files) or None."""
    git = shutil.which("git")
    if not git:
        return None
    deadline = time.monotonic() + _GIT_TIMEOUT
    staged = _run_git(git, root, ["ls-files", "-z", "--stage"], deadline)
    if staged is None:
        return None
    others = _run_git(git, root, ["ls-files", "-z", "--others", "--exclude-standard"], deadline)
    if others is None:
        return None
    deleted = _run_git(git, root, ["ls-files", "-z", "--deleted"], deadline)
    if deleted is None:
        return None
    gone = set(deleted)
    tracked = set()
    for rec in staged:
        meta, sep, path = rec.partition("\t")
        if not sep or meta.startswith("160000 ") or path in gone:
            continue  # gitlink (submodule) or deleted from the working tree
        tracked.add(path)
    untracked = {p for p in others if not p.endswith("/") and p not in tracked}
    return sorted(tracked), sorted(untracked)


# ---------------------------------------------------------------------------
# walk mode


def _read_gitignore(directory: str) -> List[_Rule]:
    try:
        with open(os.path.join(directory, ".gitignore"), "rb") as fh:
            data = fh.read(_GITIGNORE_MAX_BYTES)
    except OSError:
        return []
    return _parse_patterns(data.decode("utf-8-sig", errors="replace").splitlines())


def _walk(root: str, max_entries: int, extras: _RuleSet) -> Tuple[List[str], bool]:
    limit = max(max_entries * 20, _WALK_FILE_FLOOR)
    started = time.monotonic()
    budget = _WALK_TIME_BUDGET
    files: List[str] = []
    # (dir relative to root, .gitignore rulesets in effect, shallowest first)
    queue = deque([("", ())])  # type: deque
    seen = 0
    while queue:
        if time.monotonic() - started > budget:
            return files, False
        rel_dir, chain = queue.popleft()
        abs_dir = os.path.join(root, rel_dir) if rel_dir else root
        own = _read_gitignore(abs_dir)
        if own:
            chain = chain + (_RuleSet(rel_dir, own),)
        rulesets = [rs for rs in (extras,) + tuple(reversed(chain)) + (_DEFAULT_RULESET,) if rs]
        try:
            it = os.scandir(abs_dir)
        except OSError:
            continue
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    break
                seen += 1
                if seen % 512 == 0 and time.monotonic() - started > budget:
                    return files, False
                name = entry.name
                rel = rel_dir + "/" + name if rel_dir else name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not _decide(rulesets, rel, name, True):
                            queue.append((rel, chain))
                        continue
                    if entry.is_symlink():
                        if entry.is_dir():  # symlinked directory: not followed, not listed
                            continue
                    elif not entry.is_file(follow_symlinks=False):
                        continue  # sockets, fifos, devices
                except OSError:
                    continue
                if _decide(rulesets, rel, name, False):
                    continue
                if len(files) >= limit:
                    return files, False
                files.append(rel)
    return files, True


# ---------------------------------------------------------------------------
# public API


def _env_max_entries() -> int:
    raw = os.environ.get("RWM_TREE_MAX", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_ENTRIES


def _extra_patterns(extra_ignores: Optional[List[str]]) -> List[str]:
    patterns = [p.strip() for p in (extra_ignores or [])]
    patterns += [p.strip() for p in os.environ.get("RWM_IGNORE", "").split(",")]
    return [p for p in patterns if p]


def _cap(files: List[str], max_entries: int) -> Tuple[List[str], Dict[str, int]]:
    if len(files) <= max_entries:
        return sorted(files), {}
    by_depth = sorted(files, key=lambda p: (p.count("/"), p))
    omitted: Dict[str, int] = {}
    for path in by_depth[max_entries:]:
        cut = path.rfind("/")
        parent = path[:cut] if cut >= 0 else "."
        omitted[parent] = omitted.get(parent, 0) + 1
    return sorted(by_depth[:max_entries]), dict(sorted(omitted.items()))


def list_tree(root: str, max_entries: int | None = None, extra_ignores: list[str] | None = None) -> dict:
    """List the files under `root` (names only, never contents).

    Returns {
      "root": root (as given, absolute, no trailing slash),
      "source": "git" | "walk",
      "files": [sorted POSIX paths relative to root],   # at most max_entries
      "total": int,           # files found before the cap (a lower bound when complete is False)
      "complete": bool,       # False when the walk hit its hard file limit or time budget
      "truncated": bool,      # len(files) < total
      "omitted": {"<rel dir or '.'>": count}  # files dropped by the cap, keyed by their direct parent dir
    }
    A root that is not a readable directory yields an empty walk listing with
    complete False.
    """
    root = os.path.abspath(root)
    if max_entries is None:
        max_entries = _env_max_entries()
    max_entries = max(0, int(max_entries))
    extras = _RuleSet("", _parse_patterns(_extra_patterns(extra_ignores)))

    source = "walk"
    complete = False
    files: List[str] = []
    if os.path.isdir(root):
        listed = _git_list(root)
        if listed is not None:
            tracked, untracked = listed
            source = "git"
            complete = True
            files = _filter_paths(tracked, [extras]) + _filter_paths(untracked, [extras, _DEFAULT_RULESET])
        else:
            files, complete = _walk(root, max_entries, extras)

    total = len(files)
    kept, omitted = _cap(files, max_entries)
    return {
        "root": root,
        "source": source,
        "files": kept,
        "total": total,
        "complete": complete,
        "truncated": len(kept) < total,
        "omitted": omitted,
    }


def _main(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Print a JSON summary of list_tree(root).")
    parser.add_argument("root")
    parser.add_argument("--max", type=int, default=None, help="max entries (default: RWM_TREE_MAX or 5000)")
    args = parser.parse_args(argv)
    started = time.monotonic()
    result = list_tree(args.root, args.max)
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    summary = {
        "root": result["root"],
        "source": result["source"],
        "total": result["total"],
        "listed": len(result["files"]),
        "truncated": result["truncated"],
        "complete": result["complete"],
        "elapsed_ms": elapsed_ms,
        "first_files": result["files"][:20],
        "omitted": result["omitted"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
