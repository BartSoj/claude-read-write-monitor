"""Static replay export: paths made relative, dot-folders hidden, nothing private left in the file."""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import export_replay  # noqa: E402
import serve  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_paths_become_relative_and_outside_paths_keep_only_their_name(self):
        events = [
            {"kind": "session", "cwd": "/home/u/wiki", "model": "m", "ts": 1},
            {"kind": "read", "path": "/home/u/wiki/needs/a.md", "ts": 2},
            {"kind": "search", "scope": "/home/u/wiki", "files": ["/home/u/wiki/b.md", "/tmp/x/out.txt"], "ts": 3},
            {"kind": "instructions", "path": "/home/u/wiki/CLAUDE.md", "parent_file_path": "/elsewhere/p.md", "ts": 4},
        ]
        out = export_replay.sanitize(events, "/home/u/wiki", "atlas")
        self.assertNotIn("cwd", out[0])
        self.assertNotIn("model", out[0])
        self.assertEqual(out[1]["path"], "atlas/needs/a.md")
        self.assertEqual(out[2]["scope"], "atlas")
        self.assertEqual(out[2]["files"], ["atlas/b.md", "outside/out.txt"])
        self.assertEqual(out[3]["parent_file_path"], "outside/p.md")
        self.assertNotIn("/home/u", json.dumps(out))

    def test_steps_skip_duplicate_instruction_loads(self):
        events = [{"kind": "instructions", "path": "C", "ts": 1000}, {"kind": "instructions", "path": "C", "ts": 1500},
                  {"kind": "pending", "ts": 1600}, {"kind": "read", "path": "a", "ts": 2000},
                  {"kind": "search", "ts": 3000}, {"kind": "edit", "path": "a", "ts": 4000}]
        self.assertEqual(export_replay.count_steps(events), 4)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.project = tempfile.mkdtemp()
        self._old = os.environ.get("RWM_DATA_DIR")
        os.environ["RWM_DATA_DIR"] = self.data
        serve._meta_cache.clear()
        for rel in ("CLAUDE.md", "needs/a.md", ".claude/skills/x/SKILL.md"):
            os.makedirs(os.path.dirname(os.path.join(self.project, rel)), exist_ok=True)
            open(os.path.join(self.project, rel), "w").write("x\n")
        sd = os.path.join(self.data, "sessions", "sess-1")
        os.makedirs(sd)
        events = [{"v": 1, "seq": 1, "kind": "session", "phase": "start", "cwd": self.project, "ts": 1},
                  {"v": 1, "seq": 2, "kind": "prompt", "ts": 2},
                  {"v": 1, "seq": 3, "kind": "read", "path": self.project + "/needs/a.md", "start": 1, "end": 1, "ts": 3},
                  {"v": 1, "seq": 4, "kind": "read", "path": self.project + "/.claude/skills/x/SKILL.md", "start": 1, "end": 1, "ts": 4}]
        open(os.path.join(sd, "events.jsonl"), "w").write("".join(json.dumps(e) + "\n" for e in events))
        json.dump({"cwd": self.project, "project": self.project, "label": "a task",
                   "transcript_path": self.project + "/t.jsonl"}, open(os.path.join(sd, "meta.json"), "w"))
        serve.save_expectations({"name": "set-1", "root": self.project, "reads": ["needs/a.md"], "writes": ["log.md"]})

    def tearDown(self):
        if self._old is None:
            os.environ.pop("RWM_DATA_DIR", None)
        else:
            os.environ["RWM_DATA_DIR"] = self._old
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.project, ignore_errors=True)

    def static(self, html):
        return json.loads(re.search(r"window\.RWM_STATIC = (\{.*?\});</script>", html, re.S).group(1).replace("<\\/", "</"))

    def test_the_export_embeds_everything_and_no_host_path(self):
        html, info = export_replay.build("sess-1", name="wiki", expect="set-1")
        s = self.static(html)
        self.assertEqual(s["listing"]["files"], ["CLAUDE.md", "needs/a.md"])   # .claude hidden
        self.assertTrue(s["hideDot"])
        self.assertEqual(s["expectations"][0]["reads"], ["needs/a.md"])
        self.assertIsNone(s["expectations"][0]["root"])
        self.assertIn("expect=set-1", s["query"])
        self.assertIn("embed=1", s["query"])
        self.assertNotIn(self.project, html)
        self.assertNotIn("transcript_path", json.dumps(s["meta"]))
        self.assertEqual(info["steps"], 2)
        self.assertEqual(export_replay.check(html, ["forbidden-word"]), [])

    def test_speed_is_baked_into_the_query_only_when_given(self):
        html, _ = export_replay.build("sess-1", name="wiki", speed=0.75)
        self.assertIn("speed=0.75", self.static(html)["query"])
        html, _ = export_replay.build("sess-1", name="wiki")
        self.assertNotIn("speed=", self.static(html)["query"])

    def test_check_refuses_a_forbidden_word(self):
        html, _ = export_replay.build("sess-1", name="wiki")
        self.assertTrue(export_replay.check(html + "Secret-Word", ["secret-word"]))

    def test_keep_dotfiles_lists_them(self):
        html, _ = export_replay.build("sess-1", name="wiki", keep_dotfiles=True)
        self.assertIn(".claude/skills/x/SKILL.md", self.static(html)["listing"]["files"])


if __name__ == "__main__":
    unittest.main()
