"""Replay files and expectation sets in scripts/serve.py."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import serve  # noqa: E402


class DataDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("RWM_DATA_DIR")
        os.environ["RWM_DATA_DIR"] = self.tmp
        serve._meta_cache.clear()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("RWM_DATA_DIR", None)
        else:
            os.environ["RWM_DATA_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_session(self, sid, events, meta):
        sd = os.path.join(self.tmp, "sessions", sid)
        os.makedirs(sd)
        with open(os.path.join(sd, "events.jsonl"), "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events))
        with open(os.path.join(sd, "meta.json"), "w") as f:
            json.dump(meta, f)


class ReplayFileTests(DataDirCase):
    EVENTS = [{"v": 1, "seq": 1, "kind": "session", "phase": "start", "ts": 1},
              {"v": 1, "seq": 2, "kind": "read", "path": "/p/a.md", "start": 1, "end": 4, "ts": 2}]

    def test_export_then_import_round_trips(self):
        self.write_session("abc-123", self.EVENTS, {"cwd": "/p", "label": "hello"})
        bundle = serve.export_bundle("abc-123")
        self.assertEqual(bundle["format"], "rwm-session")
        self.assertEqual(bundle["events"], self.EVENTS)
        shutil.rmtree(os.path.join(self.tmp, "sessions", "abc-123"))
        self.assertEqual(serve.import_bundle(json.loads(json.dumps(bundle))), "abc-123")
        serve._meta_cache.clear()
        again = serve.export_bundle("abc-123")
        self.assertEqual(again["events"], self.EVENTS)
        self.assertTrue(again["meta"]["closed"])
        self.assertEqual(again["meta"]["label"], "hello")
        with open(os.path.join(self.tmp, "sessions", "abc-123", "seq")) as f:
            self.assertEqual(f.read(), "2")

    def test_import_refuses_an_existing_session_unless_forced(self):
        self.write_session("abc-123", self.EVENTS, {})
        bundle = serve.export_bundle("abc-123")
        with self.assertRaises(FileExistsError):
            serve.import_bundle(bundle)
        self.assertEqual(serve.import_bundle(bundle, force=True), "abc-123")

    def test_rejects_foreign_files_and_unsafe_ids(self):
        with self.assertRaises(ValueError):
            serve.import_bundle({"format": "other", "id": "x", "events": []})
        with self.assertRaises(ValueError):
            serve.import_bundle({"format": "rwm-session", "id": "../escape", "events": []})
        with self.assertRaises(ValueError):
            serve.import_bundle({"format": "rwm-session", "id": "ok", "events": "nope"})
        self.assertIsNone(serve.export_bundle("../etc"))
        self.assertIsNone(serve.export_bundle("missing"))


class ExpectationTests(DataDirCase):
    def test_parse_reads_writes_root_note_and_default_read(self):
        s = serve.parse_expectations(
            "# expectation set: demo\n# what a guide needs\nroot: /p\n\nneeds/a.md\nread methods/*.md\n"
            "write guides/*.md\nWRITE log.md\nread needs/a.md\n", "demo")
        self.assertEqual(s["root"], "/p")
        self.assertEqual(s["reads"], ["needs/a.md", "methods/*.md"])
        self.assertEqual(s["writes"], ["guides/*.md", "log.md"])
        self.assertEqual(s["note"], "what a guide needs")

    def test_parse_drops_paths_outside_the_project(self):
        s = serve.parse_expectations("read /etc/passwd\nread ../x.md\nread ~/y.md\nread ok.md\n", "x")
        self.assertEqual(s["reads"], ["ok.md"])

    def test_save_then_list_filters_by_root(self):
        serve.save_expectations({"name": "one", "root": self.tmp, "reads": ["a.md"], "writes": ["b.md"]})
        serve.save_expectations({"name": "two", "root": "/somewhere/else", "reads": ["c.md"]})
        serve.save_expectations({"name": "any", "reads": ["d.md"]})
        names = [s["name"] for s in serve.list_expectations(self.tmp)]
        self.assertEqual(names, ["any", "one"])
        one = [s for s in serve.list_expectations(self.tmp) if s["name"] == "one"][0]
        self.assertEqual((one["reads"], one["writes"]), (["a.md"], ["b.md"]))
        self.assertEqual(len(serve.list_expectations()), 3)

    def test_save_validates_name_and_paths(self):
        for bad in ("", "../x", "has space", "x" * 65):
            with self.assertRaises(ValueError):
                serve.save_expectations({"name": bad, "reads": ["a.md"]})
        with self.assertRaises(ValueError):
            serve.save_expectations({"name": "ok", "reads": ["/abs/path.md"]})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "expectations", "ok.txt")))

    def test_format_round_trips(self):
        s = {"name": "rt", "root": "/p", "note": "n", "reads": ["a.md", "b/*.md"], "writes": ["c.md"]}
        self.assertEqual(serve.parse_expectations(serve.format_expectations(s), "rt"), s)


if __name__ == "__main__":
    unittest.main()
