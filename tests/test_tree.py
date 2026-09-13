"""Tests for scripts/tree.py. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import tree  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def touch(base: str, *rels: str) -> None:
    for rel in rels:
        path = os.path.join(base, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x")


def write(base: str, rel: str, text: str) -> None:
    path = os.path.join(base, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="rwm-tree-"))
        self.addCleanup(self._cleanup)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("RWM_IGNORE", "RWM_TREE_MAX", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            os.environ.pop(key, None)
        # Keep the user's git config (global excludes, signing) out of the tests and
        # stop git discovery from finding a repository above the temp dir.
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(self.tmp)

    def _cleanup(self) -> None:
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), stat.S_IRWXU)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)


class _WalkCase(_TempDirCase):
    """Forces walk mode regardless of git availability."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(tree, "_git_list", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def files(self, **kwargs):
        res = tree.list_tree(self.tmp, **kwargs)
        self.assertEqual(res["source"], "walk")
        return res["files"]


@unittest.skipUnless(HAVE_GIT, "git is not available")
class GitModeTests(_TempDirCase):
    def git(self, *args: str, cwd: str | None = None) -> None:
        env = dict(os.environ)
        env.update(
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]
            + list(args),
            cwd=cwd or self.tmp,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def make_repo(self) -> None:
        self.git("init", "-q")
        write(self.tmp, ".gitignore", "*.log\nsecret/\n")
        touch(self.tmp, "a.txt", "src/main.py", "src/util/helper.py", "gone.txt", "build/gen.txt")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        os.remove(os.path.join(self.tmp, "gone.txt"))
        touch(
            self.tmp,
            "notes.md",
            "src/new.py",
            "debug.log",
            "secret/key.txt",
            ".claude/skills/demo/SKILL.md",
            ".DS_Store",
            "node_modules/pkg/index.js",
        )

    def test_tracked_untracked_ignored_deleted(self):
        self.make_repo()
        res = tree.list_tree(self.tmp)
        self.assertEqual(res["source"], "git")
        self.assertEqual(
            res["files"],
            [
                ".claude/skills/demo/SKILL.md",
                ".gitignore",
                "a.txt",
                "build/gen.txt",  # tracked: default ignores do not hide it
                "notes.md",
                "src/main.py",
                "src/new.py",
                "src/util/helper.py",
            ],
        )
        self.assertEqual(res["total"], 8)
        self.assertTrue(res["complete"])
        self.assertFalse(res["truncated"])
        self.assertEqual(res["omitted"], {})
        self.assertEqual(res["root"], self.tmp)

    def test_root_is_subdirectory_of_repo(self):
        self.make_repo()
        res = tree.list_tree(os.path.join(self.tmp, "src") + "/")
        self.assertEqual(res["source"], "git")
        self.assertEqual(res["root"], os.path.join(self.tmp, "src"))
        self.assertEqual(res["files"], ["main.py", "new.py", "util/helper.py"])

    def test_extra_ignores_and_env(self):
        self.make_repo()
        os.environ["RWM_IGNORE"] = " *.md , src/util/ "
        res = tree.list_tree(self.tmp, extra_ignores=["*.txt", "!a.txt"])
        self.assertEqual(res["source"], "git")
        self.assertEqual(res["files"], [".gitignore", "a.txt", "src/main.py", "src/new.py"])

    def test_nested_untracked_repo_is_skipped(self):
        self.make_repo()
        nested = os.path.join(self.tmp, "vendor", "lib")
        os.makedirs(nested)
        self.git("init", "-q", cwd=nested)
        touch(nested, "inner.txt")
        res = tree.list_tree(self.tmp)
        self.assertFalse(any(p.startswith("vendor") for p in res["files"]))

    def test_falls_back_to_walk_without_git(self):
        self.make_repo()
        with mock.patch.object(tree.shutil, "which", return_value=None):
            res = tree.list_tree(self.tmp)
        self.assertEqual(res["source"], "walk")
        self.assertNotIn("debug.log", res["files"])  # root .gitignore still honoured
        self.assertNotIn("gone.txt", res["files"])
        self.assertNotIn("build/gen.txt", res["files"])  # default ignores apply in walk mode
        self.assertFalse(any(p.startswith(".git/") for p in res["files"]))


class PlainDirectoryTests(_TempDirCase):
    def test_directory_outside_git_uses_walk(self):
        touch(self.tmp, "a.txt")
        res = tree.list_tree(self.tmp)
        self.assertEqual(res["source"], "walk")
        self.assertEqual(res["files"], ["a.txt"])


class WalkModeTests(_WalkCase):
    def test_default_ignores(self):
        touch(
            self.tmp,
            "README.md",
            ".claude/skills/x/SKILL.md",
            ".claude/settings.json",
            "node_modules/pkg/index.js",
            ".venv/bin/python",
            "bin/__pycache__/tool.cpython-313.pyc",
            "bin/tool.py",
            "bin/stale.pyc",
            ".DS_Store",
            "sub/.DS_Store",
            ".git/HEAD",
            "web/dist/app.js",
            "Main.class",
        )
        self.assertEqual(
            self.files(),
            [".claude/settings.json", ".claude/skills/x/SKILL.md", "README.md", "bin/tool.py"],
        )

    def test_gitignore_semantics(self):
        write(
            self.tmp,
            ".gitignore",
            "# comment\n"
            "\n"
            "*.log\n"
            "!keep.log\n"
            "cache/\n"
            "/anchored.txt\n"
            "docs/private\n"
            "logs/\n"
            "!logs/wanted.txt\n"
            "trailing.txt   \n"
            "\\#hash.txt\n",
        )
        touch(
            self.tmp,
            "app.log",
            "keep.log",
            "deep/er/app.log",
            "cache/x.txt",
            "sub/cache",  # a file named cache: dir-only rule does not apply
            "anchored.txt",
            "sub/anchored.txt",
            "docs/private",
            "sub/docs/private",
            "logs/wanted.txt",  # negation inside an excluded dir has no effect
            "trailing.txt",
            "#hash.txt",
            "plain.txt",
        )
        write(self.tmp, "sub/.gitignore", "*.tmp\n!important.log\n/local.txt\n")
        touch(self.tmp, "sub/a.tmp", "sub/important.log", "sub/local.txt", "sub/deeper/local.txt", "b.tmp")
        self.assertEqual(
            self.files(),
            [
                ".gitignore",
                "b.tmp",
                "keep.log",
                "plain.txt",
                "sub/.gitignore",
                "sub/anchored.txt",
                "sub/cache",
                "sub/deeper/local.txt",
                "sub/docs/private",
                "sub/important.log",
            ],
        )

    def test_double_star_and_classes(self):
        write(
            self.tmp,
            ".gitignore",
            "**/tmpdir\n"
            "a/**/z.txt\n"
            "out/**\n"
            "!out/keep.txt\n"
            "file[0-9].txt\n"
            "?.dat\n"
            "[!a]x.bin\n",
        )
        touch(
            self.tmp,
            "tmpdir/1.txt",
            "p/q/tmpdir/2.txt",
            "a/z.txt",
            "a/b/c/z.txt",
            "a/y.txt",
            "out/drop.txt",
            "out/deep/drop.txt",
            "out/keep.txt",
            "file1.txt",
            "fileX.txt",
            "k.dat",
            "kk.dat",
            "ax.bin",
            "bx.bin",
        )
        self.assertEqual(
            self.files(),
            [".gitignore", "a/y.txt", "ax.bin", "fileX.txt", "kk.dat", "out/keep.txt"],
        )

    def test_extra_ignores_and_env(self):
        write(self.tmp, ".gitignore", "!never-matters\n")
        touch(self.tmp, "a.txt", "b.md", "gen/out.js", "dist/bundle.js", "sub/c.txt")
        os.environ["RWM_IGNORE"] = " gen/ ,, *.md "
        self.assertEqual(
            self.files(extra_ignores=["sub/", "!dist/"]),
            [".gitignore", "a.txt", "dist/bundle.js"],  # extras can re-include a default ignore
        )

    def test_cap_is_breadth_first(self):
        touch(self.tmp, "a.txt", "b.txt", "d1/x1", "d1/x2", "d1/x3")
        touch(self.tmp, *["d1/d2/y%d" % i for i in range(5)])
        res = tree.list_tree(self.tmp, max_entries=4)
        self.assertEqual(res["files"], ["a.txt", "b.txt", "d1/x1", "d1/x2"])
        self.assertEqual(res["total"], 10)
        self.assertTrue(res["truncated"])
        self.assertTrue(res["complete"])
        self.assertEqual(res["omitted"], {"d1": 1, "d1/d2": 5})

        res = tree.list_tree(self.tmp, max_entries=1)
        self.assertEqual(res["files"], ["a.txt"])
        self.assertEqual(res["omitted"], {".": 1, "d1": 3, "d1/d2": 5})

    def test_env_max_entries(self):
        touch(self.tmp, "a", "b", "c")
        os.environ["RWM_TREE_MAX"] = "2"
        res = tree.list_tree(self.tmp)
        self.assertEqual((res["files"], res["total"], res["truncated"]), (["a", "b"], 3, True))
        os.environ["RWM_TREE_MAX"] = "junk"
        self.assertEqual(tree.list_tree(self.tmp)["files"], ["a", "b", "c"])

    def test_hard_file_limit(self):
        touch(self.tmp, *["f%02d" % i for i in range(30)])
        with mock.patch.object(tree, "_WALK_FILE_FLOOR", 0):
            res = tree.list_tree(self.tmp, max_entries=1)
        self.assertFalse(res["complete"])
        self.assertEqual(res["total"], 20)
        self.assertEqual(len(res["files"]), 1)

    def test_time_budget(self):
        touch(self.tmp, "a", "b/c")
        with mock.patch.object(tree, "_WALK_TIME_BUDGET", -1):
            res = tree.list_tree(self.tmp)
        self.assertFalse(res["complete"])

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks")
    def test_symlink_loop_does_not_hang(self):
        touch(self.tmp, "real.txt", "d/inner.txt")
        os.symlink(self.tmp, os.path.join(self.tmp, "loop"))
        os.symlink("..", os.path.join(self.tmp, "d", "up"))
        os.symlink("self", os.path.join(self.tmp, "self"))
        os.symlink("real.txt", os.path.join(self.tmp, "link.txt"))
        box = {}
        worker = threading.Thread(target=lambda: box.update(res=tree.list_tree(self.tmp)), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "list_tree hung on a symlink loop")
        files = box["res"]["files"]
        self.assertIn("real.txt", files)
        self.assertIn("link.txt", files)
        self.assertIn("d/inner.txt", files)
        self.assertNotIn("loop", files)
        self.assertNotIn("d/up", files)
        self.assertTrue(box["res"]["complete"])

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_permission_error_is_survived(self):
        touch(self.tmp, "ok.txt", "locked/hidden.txt")
        os.chmod(os.path.join(self.tmp, "locked"), 0)
        res = tree.list_tree(self.tmp)
        self.assertEqual(res["files"], ["ok.txt"])

    def test_missing_root(self):
        res = tree.list_tree(os.path.join(self.tmp, "nope") + "/")
        self.assertEqual(res["root"], os.path.join(self.tmp, "nope"))
        self.assertEqual((res["files"], res["total"], res["complete"]), ([], 0, False))


if __name__ == "__main__":
    unittest.main()
