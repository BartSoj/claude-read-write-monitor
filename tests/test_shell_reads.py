"""Tests for scripts/shell_reads.py. Run: python3 -m unittest discover -s tests"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import shell_reads as sr  # noqa: E402


def lines(n):
    return "".join("line %d\n" % i for i in range(1, n + 1))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="rwm-shell-"))
        self.files = {
            "CLAUDE.md": "# project\n",
            "notes/a.md": "alpha\nbeta\nalpha again\n",
            "notes/b.md": "gamma\nalpha\n",
            "sub/x.md": lines(3),
            "big.txt": lines(50),
            "src/app.py": "print('hi')\n",
        }
        for rel, text in self.files.items():
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(text)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def p(self, rel):
        return os.path.join(self.root, rel)

    def parse(self, command, cwd=None):
        return sr.parse(command, cwd or self.root)

    def one(self, command, cwd=None):
        acts = self.parse(command, cwd)
        self.assertEqual(len(acts), 1, acts)
        return acts[0]

    def rng(self, act):
        return act["start"], act["end"], act["from_end"]


class ReadTests(Fixture):
    def test_cat_multiple_files(self):
        acts = self.parse("cat notes/a.md notes/b.md")
        self.assertEqual([a["path"] for a in acts], [self.p("notes/a.md"), self.p("notes/b.md")])
        for a in acts:
            self.assertEqual((a["op"], a["cmd"]), ("read", "cat"))
            self.assertEqual(self.rng(a), (None, None, None))

    def test_cd_then_cat(self):
        a = self.one("cd sub && cat x.md")
        self.assertEqual(a["path"], self.p("sub/x.md"))

    def test_cd_absolute_and_relative_chain(self):
        acts = self.parse("cd %s; cd notes\ncat a.md" % self.root, cwd="/")
        self.assertEqual([a["path"] for a in acts], [self.p("notes/a.md")])

    def test_head_forms(self):
        self.assertEqual(self.rng(self.one("head -n 5 big.txt")), (1, 5, None))
        self.assertEqual(self.rng(self.one("head -5 big.txt")), (1, 5, None))
        self.assertEqual(self.rng(self.one("head big.txt")), (1, 10, None))
        self.assertEqual(self.rng(self.one("head --lines=7 big.txt")), (1, 7, None))

    def test_tail_forms(self):
        self.assertEqual(self.rng(self.one("tail -n 7 big.txt")), (None, None, 7))
        self.assertEqual(self.rng(self.one("tail -30 big.txt")), (None, None, 30))
        self.assertEqual(self.rng(self.one("tail big.txt")), (None, None, 10))
        self.assertEqual(self.rng(self.one("tail -n +40 big.txt")), (40, None, None))

    def test_sed_ranges(self):
        a = self.one("sed -n '10,20p' big.txt")
        self.assertEqual((a["cmd"], self.rng(a)), ("sed", (10, 20, None)))
        self.assertEqual(self.rng(self.one("sed -n 7p big.txt")), (7, 7, None))
        acts = self.parse("sed -n '1,5p;30,32p' big.txt")
        self.assertEqual([self.rng(a) for a in acts], [(1, 5, None), (30, 32, None)])
        acts = self.parse("sed -n -e '1,2p' -e '45,$p' big.txt")
        self.assertEqual([self.rng(a) for a in acts], [(1, 2, None), (45, None, None)])

    def test_sed_not_a_read(self):
        self.assertEqual(self.parse("sed 's/a/b/' big.txt"), [])
        acts = self.parse("sed -i '' -n '1,5p' big.txt")  # an in-place edit, never a read
        self.assertEqual([(a["op"], a["mode"]) for a in acts], [("write", "in-place")])
        self.assertEqual(self.parse("sed -n '/start/,/end/p' big.txt"), [])

    def test_awk_ranges(self):
        self.assertEqual(self.rng(self.one("awk 'NR>=3 && NR<=9' big.txt")), (3, 9, None))
        self.assertEqual(self.rng(self.one("awk 'NR==4' big.txt")), (4, 4, None))
        self.assertEqual(self.parse("awk '{print $2}' big.txt"), [])

    def test_cat_piped_into_head(self):
        a = self.one("cat big.txt | head -n 5")
        self.assertEqual((a["op"], a["cmd"], a["path"]), ("read", "cat", self.p("big.txt")))
        self.assertEqual(self.rng(a), (1, 5, None))

    def test_pipeline_ranges_compose(self):
        self.assertEqual(self.rng(self.one("head -20 big.txt | tail -5")), (16, 20, None))
        self.assertEqual(self.rng(self.one("tail -n +10 big.txt | head -3")), (10, 12, None))
        self.assertEqual(self.rng(self.one("sed -n '10,40p' big.txt | head -5")), (10, 14, None))

    def test_cat_piped_into_grep_is_a_search(self):
        a = self.one("cat notes/a.md | grep alpha")
        self.assertEqual(a["op"], "search")
        self.assertEqual((a["cmd"], a["pattern"]), ("grep", "alpha"))
        self.assertEqual(a["scope"], [self.p("notes/a.md")])
        self.assertTrue(a["single_file"])
        self.assertEqual(sr.hits(a, "alpha\nalpha again", self.root), [self.p("notes/a.md")])

    def test_read_into_wc_or_stdin_reader_yields_nothing(self):
        self.assertEqual(self.parse("cat big.txt | wc -l"), [])
        self.assertEqual(self.parse("python3 bin/x.py | head -5"), [])
        self.assertEqual(self.parse("git log | tail -3"), [])

    def test_passthrough_filter_keeps_read(self):
        a = self.one("cat -n big.txt | cut -c1-80")
        self.assertEqual(self.rng(a), (None, None, None))

    def test_unquoted_glob_expands(self):
        acts = self.parse("cat notes/*.md")
        self.assertEqual([a["path"] for a in acts], [self.p("notes/a.md"), self.p("notes/b.md")])

    def test_variables_and_for_loops(self):
        a = self.one("P=%s; sed -n '1,3p' $P" % self.p("big.txt"))
        self.assertEqual((a["path"], self.rng(a)), (self.p("big.txt"), (1, 3, None)))
        acts = self.parse('for f in notes/a.md notes/b.md; do echo "== $f"; head -2 "$f"; done')
        self.assertEqual([a["path"] for a in acts], [self.p("notes/a.md"), self.p("notes/b.md")])
        self.assertEqual(self.parse('for f in $(ls); do cat "$f"; done'), [])

    def test_environment_variables(self):
        os.environ["RWM_TEST_STAGE"] = self.root
        try:
            a = self.one('cd "$RWM_TEST_STAGE/sub"; sed -n \'1,2p\' x.md', cwd="/")
            self.assertEqual((a["path"], self.rng(a)), (self.p("sub/x.md"), (1, 2, None)))
        finally:
            del os.environ["RWM_TEST_STAGE"]
        a = self.one('cat "${RWM_TEST_UNSET_VAR:-%s}/big.txt"' % self.root, cwd="/")
        self.assertEqual(a["path"], self.p("big.txt"))
        # an unknown directory makes relative paths unknowable, so nothing is guessed
        self.assertEqual(self.parse('cd "$RWM_TEST_UNSET_VAR/x" && cat big.txt'), [])

    def test_prefixes_skipped(self):
        a = self.one("LC_ALL=C time nice -n 5 cat big.txt")
        self.assertEqual(a["path"], self.p("big.txt"))
        self.assertEqual(self.one("env FOO=1 head -3 big.txt")["end"], 3)

    def test_head_bytes(self):
        a = self.one("head -c 20 big.txt")
        r = sr.resolve_read(a)
        self.assertEqual((r["start"], r["end"]), (1, 3))  # "line 1\nline 2\nline 3\n" is 21 bytes


class SkipTests(Fixture):
    def test_stdout_redirect_is_a_write_not_a_read(self):
        def ops(cmd):
            return [(a["op"], a["cmd"], a.get("path"), a.get("mode")) for a in self.parse(cmd)]
        self.assertEqual(ops("cat notes/a.md > out.txt"), [("write", "cat", self.p("out.txt"), "overwrite")])
        self.assertEqual(ops("cat notes/a.md >> out.txt"), [("write", "cat", self.p("out.txt"), "append")])
        self.assertEqual(ops("grep -rl alpha . > hits.txt"), [("write", "grep", self.p("hits.txt"), "overwrite")])
        self.assertEqual(ops("cat big.txt | head -3 > out.txt"), [("write", "head", self.p("out.txt"), "overwrite")])
        self.assertEqual(self.parse("cat big.txt &>/dev/null"), [])

    def test_substitution_and_heredoc_skipped(self):
        self.assertEqual(self.parse("cat $(echo notes/a.md)"), [])
        self.assertEqual(self.parse('head -3 "$(ls | head -1)"'), [])
        self.assertEqual(self.parse("cat `echo notes/a.md`"), [])
        self.assertEqual(self.parse("n=$(wc -l < big.txt)"), [])
        cmd = "cat > new.md <<'EOF'\ncat notes/a.md\nEOF\ntail -2 big.txt"
        acts = self.parse(cmd)
        self.assertEqual([(a["op"], a["path"]) for a in acts], [("write", self.p("new.md")), ("read", self.p("big.txt"))])
        self.assertEqual((acts[0]["lines"], acts[1]["from_end"]), (1, 2))

    def test_stderr_redirects_kept(self):
        self.assertEqual(self.one("cat notes/a.md 2>/dev/null")["path"], self.p("notes/a.md"))
        a = self.one("grep -rl alpha . 2>&1 | head")
        self.assertEqual(a["op"], "search")

    def test_other_commands_yield_nothing(self):
        for cmd in ("python3 x.py", "git status", "wc -l big.txt", "echo cat big.txt",
                    "mkdir -p a/b", "rm -f big.txt", "command -v cat", "# cat big.txt"):
            self.assertEqual(self.parse(cmd), [], cmd)

    def test_comments_stripped(self):
        acts = self.parse("# look at it\ncat big.txt # trailing")
        self.assertEqual([a["path"] for a in acts], [self.p("big.txt")])


class SearchTests(Fixture):
    def test_grep_rl_with_stdout(self):
        a = self.one('grep -r "alpha" . --files-with-matches 2>/dev/null')
        self.assertEqual((a["op"], a["cmd"], a["pattern"]), ("search", "grep", "alpha"))
        self.assertEqual(a["scope"], [self.root])
        self.assertTrue(a["files_only"])
        self.assertFalse(a["single_file"])
        got = sr.hits(a, "notes/b.md\nnotes/a.md", self.root)
        self.assertEqual(got, [self.p("notes/b.md"), self.p("notes/a.md")])
        a2 = self.one("grep -rl alpha .")
        self.assertTrue(a2["files_only"])

    def test_grep_rn_content_output(self):
        a = self.one('grep -rn -e alpha -A1 notes')
        self.assertEqual((a["pattern"], a["files_only"]), ("alpha", False))
        self.assertEqual(a["scope"], [self.p("notes")])
        out = "notes/a.md:1:alpha\nnotes/a.md-2-beta\n--\nnotes/b.md:2:alpha\nnot/a/file.md:3:x"
        self.assertEqual(sr.hits(a, out, self.root), [self.p("notes/a.md"), self.p("notes/b.md")])

    def test_grep_count_lines(self):
        a = self.one("grep -rc alpha notes")
        self.assertTrue(a["files_only"])
        out = "notes/a.md:2\nnotes/b.md:1\nsub/x.md:0"
        self.assertEqual(sr.hits(a, out, self.root), [self.p("notes/a.md"), self.p("notes/b.md")])

    def test_grep_single_file(self):
        a = self.one('grep -n "alpha" notes/a.md')
        self.assertTrue(a["single_file"])
        self.assertEqual(a["scope"], [self.p("notes/a.md")])
        self.assertEqual(sr.hits(a, "1:alpha\n3:alpha again", self.root), [self.p("notes/a.md")])
        self.assertEqual(sr.hits(a, "", self.root), [])

    def test_grep_without_path_on_stdin_is_a_filter(self):
        acts = self.parse("ls notes | grep a")
        self.assertEqual([a["op"] for a in acts], ["list"])

    def test_grep_glob_scope(self):
        a = self.one("grep -l alpha notes/*.md")
        self.assertEqual(a["scope"], [self.p("notes/a.md"), self.p("notes/b.md")])

    def test_agent_style_grep_pipeline(self):
        wiki = self.root
        a = self.one('cd %s && grep -rn "berrypicking" --include="*.md" . | head -20' % wiki, cwd="/")
        self.assertEqual((a["op"], a["pattern"], a["scope"]), ("search", "berrypicking", [wiki]))
        self.assertFalse(a["files_only"])
        out = "./notes/b.md:2:the berrypicking model\n./CLAUDE.md:1:berrypicking"
        self.assertEqual(sr.hits(a, out, "/"), [self.p("notes/b.md"), self.p("CLAUDE.md")])

    def test_rg_files_and_rg_l(self):
        a = self.one("rg --files src notes")
        self.assertEqual((a["op"], a["cmd"]), ("list", "rg --files"))
        self.assertEqual(a["scope"], [self.p("src"), self.p("notes")])
        self.assertEqual(sr.hits(a, "src/app.py\nnotes/a.md\n", self.root), [self.p("src/app.py"), self.p("notes/a.md")])
        b = self.one("rg -l -g '*.md' alpha")
        self.assertEqual((b["op"], b["cmd"], b["pattern"], b["files_only"]), ("search", "rg", "alpha", True))
        self.assertEqual(b["scope"], [self.root])

    def test_rg_heading_output(self):
        a = self.one("rg --heading -n alpha notes")
        out = "notes/a.md\n1:alpha\n3:alpha again\n\nnotes/b.md\n2:alpha\n"
        self.assertEqual(sr.hits(a, out, self.root), [self.p("notes/a.md"), self.p("notes/b.md")])

    def test_git_grep(self):
        a = self.one("git --no-pager grep -n alpha -- notes")
        self.assertEqual((a["cmd"], a["pattern"], a["scope"]), ("git grep", "alpha", [self.p("notes")]))

    def test_xargs_and_find_exec(self):
        a = self.one("find notes -name '*.md' | xargs grep -l alpha")
        self.assertEqual((a["op"], a["cmd"], a["scope"], a["files_only"]), ("search", "grep", [self.p("notes")], True))
        b = self.one("find . -name '*.md' -exec grep -l alpha {} \\;")
        self.assertEqual((b["op"], b["scope"]), ("search", [self.root]))


class ListTests(Fixture):
    def test_find_with_scope(self):
        a = self.one('find notes -name "*.md" -type f 2>/dev/null')
        self.assertEqual((a["op"], a["cmd"], a["scope"]), ("list", "find", [self.p("notes")]))
        self.assertEqual(sr.hits(a, "notes/b.md\nnotes/a.md\nnotes", self.root), [self.p("notes/b.md"), self.p("notes/a.md")])

    def test_find_without_scope(self):
        a = self.one('find -name "*.md"')
        self.assertEqual(a["scope"], [self.root])
        a = self.one('find . -name "*.md" -type f 2>/dev/null')
        self.assertEqual(a["scope"], [self.root])
        out = "./CLAUDE.md\n./notes/b.md\n./notes/a.md"
        self.assertEqual(sr.hits(a, out, self.root), [self.p("CLAUDE.md"), self.p("notes/b.md"), self.p("notes/a.md")])

    def test_ls_directory(self):
        a = self.one("ls notes")
        self.assertEqual((a["op"], a["cmd"], a["scope"]), ("list", "ls", [self.p("notes")]))
        self.assertEqual(sr.hits(a, "a.md\nb.md\n", self.root), [self.p("notes/a.md"), self.p("notes/b.md")])
        long_out = ("total 16\ndrwxr-xr-x  4 user  staff  128 Aug 11 16:10 .\n"
                    "-rw-r--r--@ 1 user  staff   24 Aug 11 16:10 a.md\n"
                    "-rw-r--r--  1 user  staff   12 Aug 11 16:10 b.md\n")
        b = self.one("ls -la notes")
        self.assertEqual(sr.hits(b, long_out, self.root), [self.p("notes/a.md"), self.p("notes/b.md")])

    def test_ls_default_and_multiple_dirs(self):
        a = self.one("ls")
        self.assertEqual(a["scope"], [self.root])
        self.assertEqual(sr.hits(a, "CLAUDE.md\nbig.txt\nnotes\n", self.root), [self.p("CLAUDE.md"), self.p("big.txt")])
        b = self.one("ls notes sub")
        out = "notes:\na.md\nb.md\n\nsub:\nx.md\n"
        self.assertEqual(sr.hits(b, out, self.root), [self.p("notes/a.md"), self.p("notes/b.md"), self.p("sub/x.md")])

    def test_tree(self):
        a = self.one("tree -L 2")
        out = ".\n├── CLAUDE.md\n├── notes\n│   ├── a.md\n│   └── b.md\n└── sub\n    └── x.md\n\n3 directories, 4 files\n"
        self.assertEqual(sr.hits(a, out, self.root),
                         [self.p("CLAUDE.md"), self.p("notes/a.md"), self.p("notes/b.md"), self.p("sub/x.md")])

    def test_hits_limit(self):
        a = self.one("find .")
        out = "\n".join(["./CLAUDE.md", "./big.txt", "./notes/a.md"])
        self.assertEqual(len(sr.hits(a, out, self.root, limit=2)), 2)


class ResolveReadTests(Fixture):
    def write(self, rel, data):
        p = self.p(rel)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_trailing_newline_convention(self):
        p = self.write("t.txt", b"a\nb\nc\n")
        r = sr.resolve_read({"op": "read", "cmd": "cat", "path": p, "start": None, "end": None, "from_end": None})
        self.assertEqual((r["start"], r["end"], r["total_lines"]), (1, 4, 4))
        q = self.write("u.txt", b"a\nb")
        r = sr.resolve_read({"op": "read", "cmd": "cat", "path": q, "start": None, "end": None, "from_end": None})
        self.assertEqual((r["start"], r["end"], r["total_lines"]), (1, 2, 2))

    def test_clamping_and_tail(self):
        p = self.write("t.txt", b"a\nb\nc\n")
        r = sr.resolve_read({"op": "read", "cmd": "head", "path": p, "start": 1, "end": 10, "from_end": None})
        self.assertEqual((r["start"], r["end"]), (1, 4))
        r = sr.resolve_read({"op": "read", "cmd": "tail", "path": p, "start": None, "end": None, "from_end": 2})
        self.assertEqual((r["start"], r["end"]), (2, 3))
        r = sr.resolve_read({"op": "read", "cmd": "tail", "path": p, "start": 2, "end": None, "from_end": None})
        self.assertEqual((r["start"], r["end"]), (2, 4))
        self.assertIsNone(sr.resolve_read({"op": "read", "path": p, "start": 9, "end": 12, "from_end": None}))

    def test_copy_not_mutated(self):
        a = self.one("sed -n '3,5p' big.txt")
        r = sr.resolve_read(a)
        self.assertEqual((r["start"], r["end"], r["total_lines"]), (3, 5, 51))
        self.assertNotIn("total_lines", a)

    def test_missing_or_not_regular(self):
        self.assertIsNone(sr.resolve_read({"op": "read", "path": self.p("nope.md")}))
        self.assertIsNone(sr.resolve_read({"op": "read", "path": self.p("notes")}))
        self.assertIsNone(sr.resolve_read({"op": "read", "path": None}))

    def test_large_file_skipped(self):
        p = self.p("huge.bin")
        with open(p, "wb") as fh:
            fh.truncate(sr.MAX_READ_BYTES + 1)
        self.assertIsNone(sr.resolve_read({"op": "read", "path": p}))


class WriteTests(Fixture):
    def w(self, act):
        return (act["op"], act["cmd"], act["path"], act["mode"], act["lines"])

    def test_heredoc_append_counts_lines_and_body_is_not_parsed(self):
        cmd = "cat >> log.md <<'EOF'\n\n## [2026-09-13] query | cat notes/a.md\n- grep -rl alpha .\nEOF"
        acts = self.parse(cmd)
        self.assertEqual([self.w(a) for a in acts], [("write", "cat", self.p("log.md"), "append", 3)])
        a = self.one("cat > new.md <<-EOF\n\tone\n\ttwo\n\tEOF\n")
        self.assertEqual((a["mode"], a["lines"]), ("overwrite", 2))

    def test_real_session_sed_inplace_then_heredoc_append(self):
        cmd = ("sed -i '' 's/^rows: 3$/rows: 4/' _meta/gaps.md && cat >> _meta/gaps.md <<'EOF'\n"
               "| g-071 | process | row |\nEOF")
        acts = self.parse(cmd)
        self.assertEqual([self.w(a) for a in acts], [
            ("write", "sed", self.p("_meta/gaps.md"), "in-place", None),
            ("write", "cat", self.p("_meta/gaps.md"), "append", 1),
        ])

    def test_devices_and_fd_redirects_are_not_writes(self):
        self.assertEqual(self.parse("python3 x.py > /dev/null 2>&1"), [])
        self.assertEqual(self.parse("echo oops >&2"), [])
        self.assertEqual(self.parse("echo oops > /dev/stderr"), [])
        self.assertEqual(self.parse("make 2> err.log"), [])
        self.assertEqual(self.parse("tee /dev/null < big.txt"), [])
        acts = self.parse("grep -rl alpha . 2>&1 | head")
        self.assertEqual([a["op"] for a in acts], ["search"])

    def test_tee(self):
        a = self.one('echo "| row |" | tee -a notes/a.md')
        self.assertEqual(self.w(a), ("write", "tee", self.p("notes/a.md"), "append", 1))
        a = self.one("cat <<'EOF' | tee out.md >/dev/null\none\ntwo\nEOF")
        self.assertEqual(self.w(a), ("write", "tee", self.p("out.md"), "overwrite", 2))
        acts = self.parse("ls notes | tee listing.txt")
        self.assertEqual([a["op"] for a in acts], ["list", "write"])

    def test_cp_and_mv(self):
        acts = self.parse("cp notes/a.md notes/b.md sub")
        self.assertEqual([self.w(a) for a in acts], [
            ("write", "cp", self.p("sub/a.md"), "copy", None), ("write", "cp", self.p("sub/b.md"), "copy", None)])
        self.assertEqual(self.w(self.one("cp -p big.txt new.txt")), ("write", "cp", self.p("new.txt"), "copy", None))
        self.assertEqual(self.w(self.one("mv big.txt sub/")), ("write", "mv", self.p("sub/big.txt"), "move", None))
        self.assertEqual(self.one("cp -t sub notes/*.md" if False else "cp -t sub big.txt")["path"], self.p("sub/big.txt"))
        self.assertEqual(self.parse("cp big.txt /dev/null"), [])

    def test_printf_and_echo(self):
        a = self.one("printf '| a |\\n| b |\\n' > t.md")
        self.assertEqual(self.w(a), ("write", "printf", self.p("t.md"), "overwrite", 2))
        self.assertEqual(self.one("printf '%s\\n' a b c >> t.md")["lines"], 3)
        self.assertEqual(self.one("echo hello >> t.md")["lines"], 1)
        self.assertIsNone(self.one('echo "$(date)" >> t.md')["lines"])
        self.assertIsNone(self.one("cat notes/a.md > t.md")["lines"])

    def test_sed_and_perl_in_place(self):
        acts = self.parse("sed --in-place -e 's/a/b/' notes/*.md")
        self.assertEqual([(a["path"], a["mode"]) for a in acts],
                         [(self.p("notes/a.md"), "in-place"), (self.p("notes/b.md"), "in-place")])
        self.assertEqual(self.one("sed -i.bak 's/a/b/' big.txt")["path"], self.p("big.txt"))
        self.assertEqual(self.one("sed -i -e 's/a/b/' -e 's/c/d/' big.txt")["path"], self.p("big.txt"))
        acts = self.parse("perl -pi -e 's/a/b/' notes/a.md notes/b.md")
        self.assertEqual([self.w(a) for a in acts], [("write", "perl", self.p("notes/a.md"), "in-place", None),
                                                     ("write", "perl", self.p("notes/b.md"), "in-place", None)])
        self.assertEqual(self.parse("perl -ne 'print if /a/' big.txt"), [])
        self.assertEqual(self.one("sed -n '1,3p' big.txt")["op"], "read")

    def test_touch_mkdir_rm(self):
        acts = self.parse("touch new.md sub/other.md")
        self.assertEqual([self.w(a) for a in acts], [("write", "touch", self.p("new.md"), "touch", None),
                                                     ("write", "touch", self.p("sub/other.md"), "touch", None)])
        self.assertEqual(self.parse("mkdir -p out && rm -rf out"), [])

    def test_paths_resolve_like_reads(self):
        self.assertEqual(self.one("cd sub && echo hi >> x.md")["path"], self.p("sub/x.md"))
        self.assertEqual(self.one("echo hi > ~/rwm-never-created.md")["path"],
                         os.path.join(os.path.expanduser("~"), "rwm-never-created.md"))
        self.assertEqual(self.parse('echo x > "$RWM_TEST_UNSET_VAR/f.md"'), [])
        self.assertEqual(self.parse('cd "$RWM_TEST_UNSET_VAR" && touch f.md'), [])
        acts = self.parse("for f in notes/a.md notes/b.md; do sed -i '' 's/a/b/' \"$f\"; done > loop.log")
        self.assertEqual([a["path"] for a in acts], [self.p("notes/a.md"), self.p("notes/b.md"), self.p("loop.log")])

    def test_comparisons_are_not_redirects(self):
        self.assertEqual(self.parse('if [[ "$a" > "b" ]]; then echo yes; fi'), [])
        self.assertEqual(self.parse("(( n > 3 )) && echo big"), [])

    def test_order_and_heredoc_stdin(self):
        acts = self.parse("cat big.txt; echo x >> log.md; tail -2 log.md")
        self.assertEqual([a["op"] for a in acts], ["read", "write", "read"])
        acts = self.parse("python3 - <<'PY' > out.json\nprint(open('big.txt').read())\nPY")
        self.assertEqual([self.w(a) for a in acts], [("write", "python3", self.p("out.json"), "overwrite", None)])
        self.assertEqual(self.parse("rg alpha <<'EOF'\nalpha\nEOF"), [])
        self.assertEqual(self.parse("cat <<'EOF'\ncat big.txt\nEOF"), [])


class GarbageTests(Fixture):
    def test_never_raises(self):
        junk = ['cat "unterminated', "sed -n '1,5p big.txt", "((((", ")))", "|||", ";;;", "&&", "<<", "<<EOF",
                "\\", "`", "$(", "cat \x00 big.txt", "for", "for f in; do", "done done", "cd", "cd -",
                "grep", "grep -e", "find (", "tail -n", "head -n", "sed -n", "awk", "xargs", "git grep",
                "ls -l --sort", "bash -c 'cat \"x'", "\n\n\n", "   ", "cat " + "a" * 5000, "🙂 cat 🙂"]
        for cmd in junk:
            got = sr.parse(cmd, self.root)
            self.assertIsInstance(got, list, cmd)
        self.assertEqual(sr.parse('cat "unterminated', self.root), [])
        self.assertEqual(sr.parse("sed -n '1,5p big.txt", self.root), [])
        for bad in (None, 42, b"cat x", ["cat"]):
            self.assertEqual(sr.parse(bad, self.root), [])
        self.assertEqual(sr.parse("cat big.txt", None), [])
        self.assertEqual(sr.parse("cat %s" % self.p("big.txt"), None)[0]["path"], self.p("big.txt"))

    def test_hits_and_resolve_garbage(self):
        for action in (None, {}, {"op": "search"}, {"op": "list", "cmd": "ls", "scope": 5}, {"op": "search", "scope": [None], "single_file": True}):
            self.assertEqual(sr.hits(action, "a\nb", self.root), [])
        self.assertEqual(sr.hits({"op": "list", "cmd": "find", "scope": [self.root]}, None, self.root), [])
        self.assertEqual(sr.hits({"op": "list", "cmd": "find"}, "big.txt", None), [])
        for action in (None, {}, {"path": 5}, {"op": "read", "path": self.p("big.txt"), "start": "x"}):
            self.assertIsNone(sr.resolve_read(action))


if __name__ == "__main__":
    unittest.main()
