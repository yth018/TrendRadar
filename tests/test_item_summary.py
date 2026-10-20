"""Network-free tests for content-grounded per-item analysis."""
import json
import os
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
    from trendradar.ai.item_summary import ItemSummaryError, enrich_item_summaries

BODY = ("公司公布本季度收入同比增长百分之二十，主要来自新增客户和订阅业务。"
        "同时研发费用增加，利润增幅低于收入增幅。公司说明下季度将继续投入研发，"
        "但尚未公布具体利润指引。原文的数据体现收入增长与成本压力并存。")
EVIDENCE = "公司公布本季度收入同比增长百分之二十，主要来自新增客户和订阅业务。"
ANALYSIS = "收入同比增长20%，新增客户与订阅业务是主要来源。研发费用增加压低利润增幅，后续应关注收入增长能否覆盖投入。"


def response(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def entry(item_id="news_000001", **changes):
    return {"id": item_id, "usable": True, "summary": ANALYSIS, "evidence": EVIDENCE, **changes}


class ItemSummaryTests(unittest.TestCase):
    def setUp(self):
        self.client_patcher = patch("trendradar.ai.item_summary.AIClient")
        self.client_class = self.client_patcher.start()
        self.addCleanup(self.client_patcher.stop)
        self.client = self.client_class.return_value
        self.client.chat.return_value = response(entry())
        self.reader_patcher = patch("trendradar.ai.item_summary.fetch_article_contents")
        self.reader = self.reader_patcher.start()
        self.addCleanup(self.reader_patcher.stop)
        self.reader.side_effect = lambda items: [
            {"content": BODY, "url": item["url"], "status": "ok"} for item in items
        ]

    def test_body_not_title_is_sent_and_evidence_is_preserved(self):
        item = {"title": "公司发布季度业绩", "url": "https://example.invalid/report"}
        enrich_item_summaries([{"titles": [item]}], None, ai_config={})
        request = json.loads(self.client.chat.call_args.args[0][1]["content"].split("：\n", 1)[1])
        self.assertEqual(request["items"][0]["content"], BODY)
        self.assertEqual(request["items"][0]["source_basis"], "article")
        self.assertEqual(item["ai_summary"], "内容分析：" + ANALYSIS)
        self.assertEqual(item["summary_evidence"], EVIDENCE)
        self.assertEqual(item["summary_basis"], "article")
        self.assertNotIn("标题简述", item["ai_summary"])

    def test_body_preferred_over_rss_and_same_url_read_and_analyzed_once(self):
        hot = {"title": "季度业绩", "url": "https://example.invalid/a"}
        rss = {"title": "季度业绩公告", "url": hot["url"], "summary": "源摘要" * 80}
        enrich_item_summaries([{"titles": [hot]}], [{"titles": [rss]}], ai_config={})
        self.assertEqual(len(self.reader.call_args.args[0]), 1)
        self.assertEqual(self.client.chat.call_count, 1)
        self.assertEqual(hot["ai_summary"], rss["ai_summary"])
        self.assertEqual(rss["summary_basis"], "article")
        self.assertIn(BODY, self.client.chat.call_args.args[0][1]["content"])

    def test_long_rss_fallback_is_explicitly_labelled(self):
        self.reader.side_effect = None
        self.reader.return_value = [{"content": "", "url": "", "status": "unavailable"}]
        item = {"title": "季度业绩", "summary": BODY}
        enrich_item_summaries([], [{"titles": [item]}], ai_config={})
        self.assertEqual(item["summary_basis"], "rss_summary")
        self.assertEqual(item["ai_summary"], "来源摘要分析：" + ANALYSIS)

    def test_unreadable_and_title_only_do_not_call_ai(self):
        self.reader.side_effect = lambda items: [{"content": "", "status": "unavailable"} for _ in items]
        items = [{"title": "仅有标题"}, {"title": "公告发布", "summary": "公告发布了。"}]
        enrich_item_summaries([{"titles": items}], None, ai_config={})
        self.client_class.assert_not_called()
        for item in items:
            self.assertEqual(item["summary_basis"], "unavailable")
            self.assertEqual(item["ai_summary"], "正文暂不可读，未生成内容分析。")

    def test_model_rejects_unrelated_page_without_title_based_analysis(self):
        self.client.chat.return_value = response(entry(usable=False, summary="", evidence=""))
        item = {"title": "请求标题"}
        enrich_item_summaries([{"titles": [item]}], None, ai_config={})
        self.assertEqual(item["summary_basis"], "unavailable")

    def test_invented_or_too_short_evidence_is_not_published(self):
        for evidence in ("这是一段输入正文中从未出现过的虚构数据和推断结论。", "公司公布"):
            with self.subTest(evidence=evidence):
                self.client.chat.return_value = response(entry(evidence=evidence))
                item = {"title": "季度业绩"}
                enrich_item_summaries([{"titles": [item]}], None, ai_config={})
                self.assertEqual(item["summary_basis"], "unavailable")
                self.assertNotIn(ANALYSIS, item["ai_summary"])

    def test_response_shape_errors_do_not_partially_write(self):
        cases = [
            (response(entry()), "遗漏了请求 ID"),
            (response(entry(), entry()), "重复请求 ID"),
            (response(entry(summary="")), "空或无效摘要"),
            (response({"summary": "没有ID"}), "缺少 ID"),
            (response(entry(usable="true")), "可用性判断"),
        ]
        for raw, message in cases:
            with self.subTest(message=message):
                self.client.chat.return_value = raw
                items = [{"title": "新闻甲"}, {"title": "新闻乙"}]
                with self.assertRaisesRegex(ItemSummaryError, message):
                    enrich_item_summaries([{"titles": items}], None, ai_config={})
                self.assertTrue(all("ai_summary" not in item for item in items))

    def test_later_batch_failure_leaves_all_items_untouched(self):
        self.client.chat.side_effect = [response(entry()), RuntimeError("secret-url")]
        items = [{"title": "新闻甲"}, {"title": "新闻乙"}]
        with self.assertRaises(ItemSummaryError) as exc:
            enrich_item_summaries([{"titles": items}], None, ai_config={}, batch_size=1)
        self.assertNotIn("secret-url", str(exc.exception))
        self.assertTrue(all("ai_summary" not in item for item in items))

    def test_resolved_article_url_is_recorded(self):
        self.reader.side_effect = None
        self.reader.return_value = [{"content": BODY, "status": "ok", "url": "https://example.invalid/article/1"}]
        item = {"title": "季度业绩", "url": "https://example.invalid/topic/1"}
        enrich_item_summaries([{"titles": [item]}], None, ai_config={})
        self.assertEqual(item["analysis_url"], "https://example.invalid/article/1")
        self.assertEqual(item["url"], "https://example.invalid/topic/1")

    def test_empty_input_does_not_fetch_or_construct_ai(self):
        enrich_item_summaries([], None, ai_config={})
        self.reader.assert_not_called()
        self.client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
