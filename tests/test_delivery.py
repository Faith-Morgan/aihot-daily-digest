"""Delivery regressions; no real network requests or state changes."""
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import aihot_daily as d


class RetryTests(unittest.TestCase):
    def test_get_exhaustion_has_no_final_sleep(self):
        with patch.object(d.urllib.request, "urlopen", side_effect=TimeoutError()) as get, \
                patch.object(d.time, "sleep") as sleep:
            with self.assertRaises(d.TransientFetchError):
                d.http_get_json("https://example.invalid")
        self.assertEqual(get.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 3])

    def test_get_http_400_is_not_retried(self):
        error = urllib.error.HTTPError("https://example.invalid", 400, "bad", {}, None)
        with patch.object(d.urllib.request, "urlopen", side_effect=error) as get, \
                patch.object(d.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                d.http_get_json("https://example.invalid")
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_post_dns_failure_has_bounded_safe_retry(self):
        error = urllib.error.URLError(d.socket.gaierror("simulated DNS failure"))
        with patch.object(d, "_post_json", side_effect=[error, {"errcode": 0, "errmsg": "ok"}]) as post, \
                patch.object(d.time, "sleep") as sleep:
            d.send_wecom("test-hook", "content")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_post_5xx_remains_uncertain(self):
        error = urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, None)
        with patch.object(d, "_post_json", side_effect=error) as post:
            with self.assertRaises(d.DeliveryUncertain):
                d.send_wecom("test-hook", "content")
        post.assert_called_once()

    def test_http_429_obeys_retry_after(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limit", {"Retry-After": "7"}, None)
        with patch.object(d, "_post_json", side_effect=[error, {"errcode": 0, "errmsg": "ok"}]) as post, \
                patch.object(d.time, "sleep") as sleep:
            d.send_wecom("test-hook", "content")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(7)

    def test_long_retry_after_is_not_shortened(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limit", {"Retry-After": "120"}, None)
        with patch.object(d, "_post_json", side_effect=error) as post, \
                patch.object(d.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "60"):
                d.send_wecom("test-hook", "content")
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_get_recovers_after_two_timeouts(self):
        response = io.BytesIO(b'{"items": [1]}')
        with patch.object(d.urllib.request, "urlopen", side_effect=[
            TimeoutError(), TimeoutError(), response,
        ]) as get, patch.object(d.time, "sleep") as sleep:
            self.assertEqual(d.http_get_json("https://example.invalid"), {"items": [1]})
        self.assertEqual(get.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 3])

    def test_post_http_400_is_not_retried(self):
        error = urllib.error.HTTPError("https://example.invalid", 400, "bad", {}, None)
        with patch.object(d, "_post_json", side_effect=error) as post, \
                patch.object(d.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                d.send_wecom("test-hook", "content")
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_post_timeout_is_not_blindly_resent(self):
        with patch.object(d, "_post_json", side_effect=TimeoutError()) as post, \
                patch.object(d.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                d.send_wecom("test-hook", "content")
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in [("STATE_FILE", root / "pushed_ids.json"),
                            ("DELIVERY_FILE", root / "delivery.json")]:
            p = patch.object(d, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        self.env = patch.dict("os.environ", {"WECOM_WEBHOOK": "test-hook", "AIHOT_PERSIST_GIT": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def plan(self, chunks=None):
        return d.new_delivery("header", chunks or ["first", "second"], ["one"])

    def test_timeout_survives_restart_and_blocks_automatic_resend(self):
        plan = self.plan()
        d.save_delivery(plan)
        with patch.object(d, "_post_json", side_effect=[{"errcode": 0, "errmsg": "ok"}, TimeoutError()]) as post, \
                patch.object(d.time, "sleep"):
            with self.assertRaises(d.DeliveryUncertain):
                d.dispatch(plan["chunks"], plan["header"], plan)
            persisted = d.load_delivery()
            self.assertEqual(persisted["confirmed"], 1)
            self.assertEqual(persisted["inflight"], 1)
            with self.assertRaises(d.DeliveryUncertain):
                d.dispatch(persisted["chunks"], persisted["header"], persisted)
        self.assertEqual(post.call_count, 2)

    def test_checkpoint_failure_prevents_post(self):
        plan = self.plan()
        with patch.object(d, "persist_git_state", side_effect=RuntimeError("checkpoint failed")), \
                patch.object(d, "_post_json") as post:
            with self.assertRaises(RuntimeError):
                d.dispatch(plan["chunks"], "header", plan)
        post.assert_not_called()

    def test_kill_after_post_before_confirmation_leaves_unknown_checkpoint(self):
        plan = self.plan()
        original = d.save_delivery
        def save(data):
            if data["confirmed"]:
                raise OSError("simulated disk failure after POST")
            original(data)
        with patch.object(d, "save_delivery", side_effect=save), \
                patch.object(d, "_post_json", return_value={"errcode": 0, "errmsg": "ok"}) as post:
            with self.assertRaises(OSError):
                d.dispatch(plan["chunks"], "header", plan)
        persisted = d.load_delivery()
        self.assertEqual(persisted["inflight"], 0)
        self.assertEqual(persisted["confirmed"], 0)
        post.assert_called_once()

    def test_corrupt_progress_fails_closed(self):
        d.DELIVERY_FILE.write_text('{"broken": true}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "损坏"):
            d.load_delivery()

    def test_changed_frozen_content_is_rejected(self):
        plan = self.plan()
        d.save_delivery(plan)
        plan["chunks"][0] = "tampered"
        d.write_json(d.DELIVERY_FILE, plan)
        with self.assertRaisesRegex(RuntimeError, "损坏"):
            d.load_delivery()

    def test_corrupt_dedup_state_fails_closed(self):
        d.STATE_FILE.write_text('"not a list"', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "损坏"):
            d.load_pushed_ids()

    def test_id_retention_keeps_chronological_tail(self):
        d.STATE_FILE.write_text('["oldest", "middle", "newest"]', encoding="utf-8")
        with patch.object(d, "MAX_STATE_IDS", 3):
            d.save_pushed_ids(d.load_pushed_ids(), ["added"])
        self.assertEqual(json.loads(d.STATE_FILE.read_text()), ["middle", "newest", "added"])

    def test_manual_resolution_only_updates_state_without_sending(self):
        plan = self.plan()
        plan["inflight"] = 0
        d.save_delivery(plan)
        with patch("sys.argv", ["aihot_daily.py", "--resolve-inflight", "sent"]), \
                patch.object(d, "_post_json") as post, patch.object(d, "fetch_items") as get:
            self.assertEqual(d.main(), 0)
        self.assertEqual(d.load_delivery()["confirmed"], 1)
        self.assertIsNone(d.load_delivery()["inflight"])
        post.assert_not_called()
        get.assert_not_called()

    def test_window_check_accepts_only_todays_on_time_completion(self):
        now = datetime(2026, 10, 20, 8, 36, tzinfo=d.CST)
        plan = self.plan(["one"])
        plan.update(date="2026-10-20", confirmed=1, completed_at="2026-10-20T00:34:59+00:00")
        d.save_delivery(plan)
        d.check_delivery(now)
        plan["completed_at"] = "2026-10-20T08:35:01+08:00"
        d.save_delivery(plan)
        with self.assertRaisesRegex(RuntimeError, "窗口"):
            d.check_delivery(now)
        plan.update(date="2026-10-19", completed_at="2026-10-19T08:30:00+08:00")
        d.save_delivery(plan)
        with self.assertRaisesRegex(RuntimeError, "尚未完成"):
            d.check_delivery(now)

    def test_dry_run_writes_no_progress_or_ids(self):
        with patch("sys.argv", ["aihot_daily.py", "--dry-run", "--no-dedup"]), \
                patch.object(d, "fetch_items", return_value=[{"id": "one"}]), \
                patch.object(d, "fetch_hot_topics", return_value=[]), \
                patch.object(d, "build_blocks", return_value=("header", ["block"])), \
                patch.object(d, "_post_json") as post, patch("sys.stdout", io.StringIO()):
            self.assertEqual(d.main(), 0)
        self.assertFalse(d.DELIVERY_FILE.exists())
        self.assertFalse(d.STATE_FILE.exists())
        post.assert_not_called()

    def test_strict_runner_refuses_to_start_late_post(self):
        plan = self.plan()
        with patch.dict("os.environ", {"AIHOT_ENFORCE_WINDOW": "1"}), \
                patch.object(d, "datetime") as clock, patch.object(d, "_post_json") as post:
            clock.now.return_value = datetime(2026, 10, 20, 13, 0, tzinfo=d.CST)
            with self.assertRaisesRegex(RuntimeError, "窗口"):
                d.dispatch(plan["chunks"], "header", plan)
        self.assertFalse(d.DELIVERY_FILE.exists())
        post.assert_not_called()

    def test_permanent_failure_does_not_expose_hook_in_error(self):
        error = urllib.error.HTTPError("https://example.invalid?key=private", 400, "secret", {}, None)
        with patch.object(d, "_post_json", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                d.send_wecom("test-hook", "content")
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_restart_sends_only_unconfirmed_frozen_chunks(self):
        def post(_hook, payload):
            content = payload["markdown"]["content"]
            if content == "second" and not getattr(post, "failed", False):
                post.failed = True
                return {"errcode": 400, "errmsg": "simulated rejection"}
            return {"errcode": 0, "errmsg": "ok"}

        with patch("sys.argv", ["aihot_daily.py"]), \
                patch.object(d, "fetch_items", return_value=[{"id": "one"}]) as fetch, \
                patch.object(d, "fetch_hot_topics", return_value=[]), \
                patch.object(d, "build_blocks", return_value=("header", ["block"])), \
                patch.object(d, "pack_chunks", side_effect=[["first", "second"], ["changed"]]), \
                patch.object(d, "_post_json", side_effect=post) as send, \
                patch.object(d.time, "sleep"):
            with self.assertRaises(RuntimeError):
                d.main()
            self.assertEqual(d.main(), 0)
        self.assertEqual([c.args[1] for c in send.call_args_list], [
            {"msgtype": "markdown", "markdown": {"content": "first"}},
            {"msgtype": "markdown", "markdown": {"content": "second"}},
            {"msgtype": "markdown", "markdown": {"content": "second"}},
        ])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(json.loads(d.STATE_FILE.read_text()), ["one"])

    def test_same_day_completed_run_does_not_fetch_or_send_again(self):
        with patch("sys.argv", ["aihot_daily.py"]), \
                patch.object(d, "fetch_items", return_value=[{"id": "one"}]) as fetch, \
                patch.object(d, "fetch_hot_topics", return_value=[]), \
                patch.object(d, "build_blocks", return_value=("header", ["block"])), \
                patch.object(d, "_post_json", return_value={"errcode": 0, "errmsg": "ok"}) as send:
            self.assertEqual(d.main(), 0)
            self.assertEqual(d.main(), 0)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
