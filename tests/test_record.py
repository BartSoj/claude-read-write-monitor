"""Writes a shell command made without the parser seeing them, found by what changed on disk."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import record  # noqa: E402


class ChangedOnDiskTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()
        self.data = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("RWM_DATA_DIR", "RWM_BASH_WRITE_SCAN")}
        os.environ["RWM_DATA_DIR"] = self.data
        os.environ.pop("RWM_BASH_WRITE_SCAN", None)
        old = time.time() - 3600
        for name in ("old.md", "new.md", "log.md"):
            path = os.path.join(self.project, name)
            with open(path, "w") as f:
                f.write("a\nb\n")
            os.utime(path, (old, old))

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.project, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def touch(self, name, text="a\nb\nc\n"):
        with open(os.path.join(self.project, name), "w") as f:
            f.write(text)

    def payload(self, command, sid="s1", tool_use_id="t1"):
        return {"session_id": sid, "cwd": self.project, "tool_name": "Bash", "tool_use_id": tool_use_id,
                "tool_input": {"command": command}, "tool_response": {"stdout": ""}, "duration_ms": 300}

    def records(self, d):
        return record.on_bash(d, record.base_record(d, "read"))

    def test_changed_since_takes_only_the_window(self):
        self.touch("new.md")
        now = int(time.time() * 1000)
        got = record.changed_since(self.project, ["old.md", "new.md", "gone.md"], now - 5000, now + 1000)
        self.assertEqual(got, [os.path.join(self.project, "new.md")])

    def test_a_python_heredoc_write_is_found(self):
        self.touch("log.md")
        recs = self.records(self.payload("python3 - <<'EOF'\nopen('log.md', 'a').write('x')\nEOF"))
        writes = [r for r in recs if r["kind"] in ("edit", "write")]
        self.assertEqual(len(writes), 1)
        w = writes[0]
        self.assertEqual(w["path"], os.path.join(self.project, "log.md"))
        self.assertEqual((w["precision"], w["mode"], w["source"]), ("mtime", "changed", "bash"))
        self.assertEqual(w["total_lines_after"], 4)

    def test_a_write_the_parser_already_saw_is_not_counted_twice(self):
        self.touch("log.md")
        recs = self.records(self.payload("cat >> log.md <<'EOF'\nc\nEOF"))
        writes = [r for r in recs if r["kind"] in ("edit", "write")]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["precision"], "shell")

    def test_a_file_just_written_by_another_tool_is_skipped(self):
        self.touch("new.md")
        record.append("s1", [{"v": 1, "kind": "edit", "ts": int(time.time() * 1000) - 400,
                              "path": os.path.join(self.project, "new.md"), "tool_use_id": "earlier"}])
        recs = self.records(self.payload("python3 do_something.py"))
        self.assertEqual([r for r in recs if r["kind"] in ("edit", "write")], [])

    def test_the_scan_can_be_turned_off(self):
        self.touch("log.md")
        os.environ["RWM_BASH_WRITE_SCAN"] = "0"
        recs = self.records(self.payload("python3 - <<'EOF'\npass\nEOF"))
        self.assertEqual([r for r in recs if r["kind"] in ("edit", "write")], [])

    def test_no_duration_means_no_scan(self):
        self.touch("log.md")
        d = self.payload("python3 x.py")
        d.pop("duration_ms")
        self.assertEqual([r for r in self.records(d) if r["kind"] in ("edit", "write")], [])


if __name__ == "__main__":
    unittest.main()
