"""Real local Git remote recovery; network calls to WeCom remain mocked."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import aihot_daily as d


@unittest.skipUnless(shutil.which("git"), "Git is required for checkpoint integration")
class GitCheckpointTests(unittest.TestCase):
    def test_fresh_checkout_recovers_confirmed_progress_from_remote(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, first, second = root / "remote.git", root / "first", root / "second"
            def git(cwd, *args):
                subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
            git(root, "init", "--bare", str(remote))
            git(root, "init", "-b", "main", str(first))
            git(first, "config", "user.name", "Checkpoint Test")
            git(first, "config", "user.email", "checkpoint@example.invalid")
            (first / "state").mkdir()
            (first / "state" / "pushed_ids.json").write_text("[]", encoding="utf-8")
            git(first, "add", "state")
            git(first, "commit", "-m", "initial")
            git(first, "remote", "add", "origin", str(remote))
            git(first, "push", "origin", "main")
            (first / "unrelated.txt").write_text("existing staged work", encoding="utf-8")
            git(first, "add", "unrelated.txt")

            def paths(checkout):
                return patch.multiple(d, REPO_ROOT=checkout, STATE_DIR=checkout / "state",
                                      STATE_FILE=checkout / "state/pushed_ids.json",
                                      DELIVERY_FILE=checkout / "state/delivery.json")

            env = {"AIHOT_PERSIST_GIT": "1", "AIHOT_GIT_BRANCH": "main",
                   "AIHOT_ENFORCE_WINDOW": "", "WECOM_WEBHOOK": "test-hook"}
            with patch.dict("os.environ", env), paths(first), \
                    patch.object(d, "_post_json", side_effect=[
                        {"errcode": 0, "errmsg": "ok"},
                        {"errcode": 400, "errmsg": "known rejection"},
                    ]), patch.object(d.time, "sleep"):
                plan = d.new_delivery("header", ["first chunk", "second chunk"], ["one"])
                d.save_delivery(plan)
                with self.assertRaises(RuntimeError):
                    d.dispatch(plan["chunks"], plan["header"], plan)

            git(root, "clone", "--branch", "main", str(remote), str(second))
            self.assertFalse((second / "unrelated.txt").exists())
            self.assertEqual(subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"], cwd=first, text=True,
            ).strip(), "unrelated.txt")
            git(second, "config", "user.name", "Checkpoint Test")
            git(second, "config", "user.email", "checkpoint@example.invalid")
            with patch.dict("os.environ", env), paths(second), \
                    patch.object(d, "_post_json", return_value={"errcode": 0, "errmsg": "ok"}) as post:
                recovered = d.load_delivery()
                self.assertEqual(recovered["confirmed"], 1)
                self.assertIsNone(recovered["inflight"])
                d.dispatch(recovered["chunks"], recovered["header"], recovered)
                d.finish_delivery(recovered)
            post.assert_called_once_with("test-hook", {
                "msgtype": "markdown", "markdown": {"content": "second chunk"},
            })
            self.assertTrue(json.loads((second / "state/delivery.json").read_text())["completed_at"])
            # Verify the completion receipt reached the remote, not only the working directory.
            receipt = subprocess.check_output([
                "git", "--git-dir", str(remote), "show", "main:state/delivery.json",
            ])
            self.assertEqual(json.loads(receipt)["confirmed"], 2)
