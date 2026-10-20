"""Offline coverage for public content reading and topic resolution."""
import threading
import unittest
from unittest.mock import Mock, patch

from trendradar.ai.article_content import (
    MAX_CONTENT_CHARS, _Reader, _clean_content, _fetch_one,
    _public_url, fetch_article_contents,
)

PROSE = ("公司本季度的收入同比增长百分之二十，管理层称主要来自新增客户和订阅业务。"
         "与此同时，研发支出上升使利润增速低于收入增速，后续仍需观察投入是否能够转化为持续收入。") * 3


def article(text):
    return {"success": True, "data": {"content": "Title: 新闻\n\nMarkdown Content:\n" + text}}


class ArticleContentTests(unittest.TestCase):
    def test_public_urls_exclude_private_credentials_and_local_hosts(self):
        for url in ["file:///tmp/a", "http://localhost/a", "http://127.0.0.1/", "http://10.0.0.2/",
                    "http://[::1]/", "http://169.254.169.254/", "http://user:secret@example.com/",
                    "http://service.internal/a", "http://2130706433/", "http://127.1/", "http://x.local/",
                    "http://example.com:9999/a", "http://example.com\n/a"]:
            with self.subTest(url=url): self.assertFalse(_public_url(url))
        self.assertTrue(_public_url("https://www.thepaper.cn/newsDetail_forward_123"))

    def test_navigation_and_captcha_are_not_article_content(self):
        self.assertEqual(_clean_content("Title: 新闻\nMarkdown Content:\n首页\n登录\n推荐\n新闻标题"), "")
        self.assertEqual(_clean_content("安全验证，请输入验证码。" * 30), "")

    def test_article_with_login_navigation_keeps_body_and_strips_urls_images(self):
        cleaned = _clean_content("Markdown Content:\n[登录](https://example.com/login)\n" + PROSE +
                                 "\n![图片](https://example.com/image.jpg)")
        self.assertIn(PROSE, cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("![", cleaned)

    def test_content_is_bounded(self):
        self.assertEqual(len(_clean_content(PROSE * 200)), MAX_CONTENT_CHARS)

    def test_toutiao_follows_only_matching_article(self):
        reader = Mock()
        reader.read_article.side_effect = [article(
            '[推荐](https://www.toutiao.com/article/111/ "无关新闻")\n'
            '[图片](https://www.toutiao.com/article/222/ "公司发布季度业绩：增长与投入并存")'), article(PROSE)]
        result = _fetch_one(reader, {"url": "https://www.toutiao.com/trending/333/", "title": "公司发布季度业绩"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["url"], "https://www.toutiao.com/article/222/")
        self.assertEqual(reader.read_article.call_count, 2)
        reader.read_article.assert_called_with(result["url"], timeout=20)

    def test_topic_without_matching_article_is_unavailable(self):
        reader = Mock()
        reader.read_article.return_value = article('[其他](https://www.toutiao.com/article/111/ "无关新闻")')
        self.assertEqual(_fetch_one(reader, {"url": "https://www.toutiao.com/trending/333/",
                                             "title": "公司发布季度业绩"})["status"], "unavailable")
        self.assertEqual(reader.read_article.call_count, 1)

    def test_failure_does_not_retry_or_expose_exception(self):
        reader = Mock()
        reader.read_article.side_effect = TimeoutError("secret-request-details")
        result = _fetch_one(reader, {"url": "https://example.com/a"})
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret-request-details", str(result))
        self.assertEqual(reader.read_article.call_count, 1)

    def test_results_keep_input_order(self):
        with patch("trendradar.ai.article_content._Reader") as reader:
            reader.return_value.read_article.side_effect = lambda url, **kw: article(PROSE + url.rsplit('/',1)[-1])
            result = fetch_article_contents([{"url": "https://example.com/one"}, {"url": "https://example.com/two"}])
        self.assertTrue(result[0]["content"].endswith("one"))
        self.assertTrue(result[1]["content"].endswith("two"))

    def test_throttle_shares_start_interval(self):
        reader = _Reader()
        with patch("trendradar.ai.article_content.time.monotonic", side_effect=[100,100,101,105]), \
             patch("trendradar.ai.article_content.time.sleep") as sleep:
            reader._throttle()
            reader._throttle()
        sleep.assert_called_once_with(4)


if __name__ == "__main__":
    unittest.main()
