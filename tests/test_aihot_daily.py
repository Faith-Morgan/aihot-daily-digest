"""离线分片回归测试，不请求 API、不发送消息、不改去重状态。"""

import unittest
from unittest.mock import patch

from scripts import aihot_daily as digest


class PackChunksTests(unittest.TestCase):
    header = "# AI 热点日报 2026-09-30"

    def test_uneven_blocks_fit_without_exceeding_bytes(self):
        # 旧均衡算法将前两块塞进一片，产生 4241 字节的消息。
        blocks = [f"{i}" + "中" * 699 + "xx" for i in range(4)]
        chunks = digest.pack_chunks(self.header, blocks)
        self.assertEqual(len(chunks), 4)
        self.assertTrue(all(digest.bytelen(c) <= digest.CHUNK_LIMIT for c in chunks))
        for i, block in enumerate(blocks, 1):
            self.assertEqual(chunks[i - 1], f"{self.header}（{i}/4）\n\n{block}")

    def test_full_message_at_exact_byte_boundary(self):
        prefix = f"{self.header}（5/5）\n\n"
        budget = digest.CHUNK_LIMIT - digest.bytelen(prefix)
        block = "中" * (budget // 3) + "x" * (budget % 3)
        chunks = digest.pack_chunks(self.header, [block] * 5)
        self.assertEqual(len(chunks), 5)
        self.assertEqual([digest.bytelen(c) for c in chunks], [3500] * 5)

    def test_more_than_five_chunks_preserve_all_content(self):
        # 合成 37 条数据；旧算法的第一片恰好为日志中的 4496 字节。
        # 历史 API 响应没有存档，这不是当天原始内容的重放。
        blocks = ["中" * 221] + ["中" * 180] * 36
        with patch.dict("os.environ", {"MAX_CHUNKS": "5"}):
            chunks = digest.pack_chunks(self.header, blocks)
        self.assertEqual(len(chunks), 7)
        self.assertTrue(all(digest.bytelen(c) <= digest.CHUNK_LIMIT for c in chunks))
        bodies = [chunk.split("\n\n", 1)[1] for chunk in chunks]
        self.assertEqual("\n\n".join(bodies), "\n\n".join(blocks))

    def test_two_digit_page_numbers_fit_byte_budget(self):
        count = 12
        prefix = f"{self.header}（{count}/{count}）\n\n"
        budget = digest.CHUNK_LIMIT - digest.bytelen(prefix)
        blocks = ["🙂" * (budget // 4) + "x" * (budget % 4)] * count
        chunks = digest.pack_chunks(self.header, blocks)
        self.assertEqual(len(chunks), count)
        self.assertTrue(all(digest.bytelen(c) <= digest.CHUNK_LIMIT for c in chunks))
        self.assertEqual(digest.bytelen(chunks[-1]), digest.CHUNK_LIMIT)

    def test_single_oversized_block_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "第 1 个内容块.*单片正文预算"):
            digest.pack_chunks(self.header, ["中" * 1400])

    def test_empty_and_single_chunk(self):
        self.assertEqual(digest.pack_chunks(self.header, []), [])
        self.assertEqual(digest.pack_chunks(self.header, ["短消息"]),
                         [f"{self.header}\n\n短消息"])

    def test_capacity_failure_does_not_send_or_mark_ids(self):
        items = [{"id": "oversized"}]
        blocks = ["中" * 1400]
        with patch("sys.argv", ["aihot_daily.py"]), \
                patch.object(digest, "fetch_items", return_value=items), \
                patch.object(digest, "load_pushed_ids", return_value=set()), \
                patch.object(digest, "fetch_hot_topics", return_value=[]), \
                patch.object(digest, "build_blocks", return_value=(self.header, blocks)), \
                patch.object(digest, "dispatch") as send, \
                patch.object(digest, "save_pushed_ids") as save:
            with self.assertRaisesRegex(RuntimeError, "单片正文预算"):
                digest.main()
        send.assert_not_called()
        save.assert_not_called()

    def test_large_digest_sends_all_chunks_and_records_ids(self):
        items = [{"id": str(i)} for i in range(37)]
        blocks = ["中" * 221] + ["中" * 180] * 36
        with patch("sys.argv", ["aihot_daily.py"]), \
                patch.object(digest, "fetch_items", return_value=items), \
                patch.object(digest, "load_pushed_ids", return_value=set()), \
                patch.object(digest, "fetch_hot_topics", return_value=[]), \
                patch.object(digest, "build_blocks", return_value=(self.header, blocks)), \
                patch.object(digest, "dispatch") as send, \
                patch.object(digest, "save_pushed_ids") as save:
            self.assertEqual(digest.main(), 0)
        self.assertEqual(len(send.call_args.args[0]), 7)
        save.assert_called_once_with(set(), [item["id"] for item in items])

    def test_dispatch_paces_more_than_twenty_chunks(self):
        chunks = [str(i) for i in range(21)]
        with patch.dict("os.environ", {"WECOM_WEBHOOK": "test-hook"}), \
                patch.object(digest, "send_wecom") as send, \
                patch.object(digest.time, "sleep") as sleep, \
                patch.object(digest, "log"):
            digest.dispatch(chunks, self.header)
        self.assertEqual([call.args[1] for call in send.call_args_list], chunks)
        self.assertEqual(len(sleep.call_args_list), 20)
        self.assertTrue(all(call.args[0] >= 3 for call in sleep.call_args_list))


if __name__ == "__main__":
    unittest.main()
