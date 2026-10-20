"""Network-free tests for per-item AI summaries."""

import json
import os
import unittest
from unittest.mock import patch


with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
    from trendradar.ai.item_summary import (
        ItemSummaryError,
        enrich_item_summaries,
    )


def _response(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


class ItemSummaryTests(unittest.TestCase):
    def setUp(self):
        self.client_patcher = patch("trendradar.ai.item_summary.AIClient")
        self.client_class = self.client_patcher.start()
        self.addCleanup(self.client_patcher.stop)
        self.client = self.client_class.return_value
        self.client.chat.return_value = _response(
            {"id": "news_000001", "summary": "这条新闻概述了已提供的事实。"},
            {"id": "news_000002", "summary": "标题说明了公告的主要内容。"},
        )

    def test_rss_summary_and_title_only_get_distinct_basis_labels(self):
        stats = [{
            "word": "科技",
            "count": 2,
            "titles": [
                {
                    "title": "公司发布季度业绩",
                    "url": "https://example.invalid/report",
                    "summary": "公司公布了本季度收入数据。",
                },
                {
                    "title": "城市启动新交通线路",
                    "url": "https://example.invalid/transit",
                },
            ],
        }]

        enrich_item_summaries(stats, None, ai_config={})

        rss_news, title_news = stats[0]["titles"]
        self.assertEqual(rss_news["ai_summary"], "这条新闻概述了已提供的事实。")
        self.assertEqual(rss_news["summary_basis"], "rss_summary")
        self.assertEqual(title_news["ai_summary"], "标题简述：标题说明了公告的主要内容。")
        self.assertEqual(title_news["summary_basis"], "title")
        self.assertEqual(self.client.chat.call_count, 1)

    def test_same_url_across_groups_is_requested_once_and_summary_is_shared(self):
        hotlist_item = {
            "title": "公司发布季度业绩",
            "url": "https://example.invalid/report",
        }
        rss_item = {
            "title": "公司季度业绩公告",
            "url": "https://example.invalid/report",
            "summary": "公司公布了本季度收入数据。",
        }
        stats = [{"word": "业绩", "titles": [hotlist_item]}]
        rss_stats = [{"word": "财经", "titles": [rss_item]}]
        self.client.chat.return_value = _response(
            {"id": "news_000001", "summary": "公司公布了本季度收入数据。"}
        )

        enrich_item_summaries(stats, rss_stats, ai_config={})

        request_content = self.client.chat.call_args.args[0][1]["content"]
        request_data = json.loads(request_content.split("：\n", 1)[1])
        self.assertEqual(len(request_data["items"]), 1)
        self.assertEqual(
            request_data["items"][0]["rss_summary"],
            "公司公布了本季度收入数据。",
        )
        self.assertEqual(hotlist_item["ai_summary"], rss_item["ai_summary"])
        self.assertEqual(hotlist_item["summary_basis"], "rss_summary")
        self.assertEqual(rss_item["summary_basis"], "rss_summary")

    def test_missing_duplicate_and_empty_response_entries_raise_without_partial_write(self):
        cases = (
            (
                "missing ID",
                _response({"id": "news_000001", "summary": "有效摘要。"}),
                "遗漏了请求 ID",
            ),
            (
                "duplicate ID",
                _response(
                    {"id": "news_000001", "summary": "第一条摘要。"},
                    {"id": "news_000001", "summary": "重复条目。"},
                ),
                "重复请求 ID",
            ),
            (
                "empty summary",
                _response({"id": "news_000001", "summary": "  "}),
                "空或无效摘要",
            ),
        )
        for case_name, response, expected_error in cases:
            with self.subTest(case=case_name):
                self.client.chat.return_value = response
                stats = [{
                    "titles": [
                        {"title": "新闻甲", "url": "https://example.invalid/a"},
                        {"title": "新闻乙", "url": "https://example.invalid/b"},
                    ]
                }]

                with self.assertRaisesRegex(ItemSummaryError, expected_error):
                    enrich_item_summaries(stats, None, ai_config={})
                self.assertNotIn("ai_summary", stats[0]["titles"][0])
                self.assertNotIn("summary_basis", stats[0]["titles"][1])

    def test_missing_id_field_is_reported_clearly(self):
        self.client.chat.return_value = _response({"summary": "没有请求 ID。"})
        stats = [{"titles": [{"title": "一条新闻"}]}]

        with self.assertRaisesRegex(ItemSummaryError, "缺少 ID"):
            enrich_item_summaries(stats, None, ai_config={})

    def test_empty_inputs_do_not_construct_or_call_ai_client(self):
        with patch("trendradar.ai.item_summary.AIClient") as client_class:
            self.assertIsNone(enrich_item_summaries([], None, ai_config={}))
            self.assertIsNone(enrich_item_summaries(None, [], ai_config={}))
            client_class.assert_not_called()

    def test_api_exception_does_not_expose_url_from_error(self):
        private_url = "https://private.example.invalid/key"
        self.client.chat.side_effect = RuntimeError(f"request failed: {private_url}")
        stats = [{"titles": [{"title": "一条新闻", "url": private_url}]}]

        with self.assertRaises(ItemSummaryError) as raised:
            enrich_item_summaries(stats, None, ai_config={})

        self.assertNotIn(private_url, str(raised.exception))
        self.assertNotIn("RuntimeError", str(raised.exception))
        self.assertNotIn("ai_summary", stats[0]["titles"][0])


if __name__ == "__main__":
    unittest.main()
