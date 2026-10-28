from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.report_window import (
    item_key, read_sent_keys, select_window_items, window_bounds, write_delivery_record,
    build_window_stats, build_window_standalone,
)
from trendradar.core.scheduler import Scheduler
from trendradar.report.formatter import format_title_for_platform
from trendradar.report.generator import prepare_report_data
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.base import NewsData, NewsItem, RSSData, RSSItem


NOW = datetime(2026, 10, 20, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def hot(title, time="07:30", url=None):
    return NewsItem(title=title, source_id="p", source_name="平台", rank=1,
                    url=url or "https://example.com/" + title, crawl_time=time)


def save_news(storage, day, items, time="07:30"):
    assert storage.save_news_data(NewsData(date=day, crawl_time=time,
                                         items={"p": items}, id_to_name={"p": "平台"}))


def test_window_boundaries_and_cross_day_duplicates(tmp_path):
    storage = LocalStorageBackend(str(tmp_path))
    save_news(storage, "2026-10-18", [hot("old")])
    save_news(storage, "2026-10-19", [hot("old"), hot("yesterday", "00:00")])
    save_news(storage, "2026-10-20", [hot("yesterday"), hot("at_cutoff")], "08:00")
    save_news(storage, "2026-10-20", [hot("after_cutoff")], "08:01")
    selected = select_window_items(storage, NOW, {"cutoff": "08:00"})
    assert {x["title"] for x in selected.values()} == {"yesterday", "at_cutoff"}
    start, end = window_bounds(NOW, {"cutoff": "08:00"})
    assert start.isoformat() == "2026-10-19T00:00:00+08:00"
    assert end.isoformat() == "2026-10-20T08:00:00+08:00"
    assert all(x["time_basis"] == "首次采集" for x in selected.values())
    storage.cleanup()


def test_rss_publication_timezone_and_missing_date(tmp_path):
    storage = LocalStorageBackend(str(tmp_path))
    items = [RSSItem(title=title, feed_id="f", url="https://example.com/" + title,
                     published_at=published, crawl_time="07:00", summary="来源摘要")
             for title, published in [("start", "2026-10-18T16:00:00Z"),
                                      ("end", "2026-10-20T00:00:00Z"),
                                      ("future", "2026-10-20T00:01:00Z"),
                                      ("old", "2026-10-18T15:59:59Z"), ("missing", "")]]
    storage.save_rss_data(RSSData(date="2026-10-20", crawl_time="07:00", items={"f": items}))
    selected = select_window_items(storage, NOW, {})
    assert {v["title"] for v in selected.values()} == {"start", "end", "missing"}
    assert next(v for v in selected.values() if v["title"] == "missing")["time_basis"] == "首次采集"
    assert next(v for v in selected.values() if v["title"] == "end")["summary"] == "来源摘要"
    storage.cleanup()


def test_legacy_naive_rss_publication_is_treated_as_utc(tmp_path):
    """Rows written by the old parser are naive UTC, not naive local time."""
    storage = LocalStorageBackend(str(tmp_path))
    item = RSSItem(
        title="overnight", feed_id="f", url="https://example.com/overnight",
        published_at="2026-10-18T16:30:00", crawl_time="00:30", first_time="00:30",
    )
    storage.save_rss_data(RSSData(date="2026-10-19", crawl_time="00:30", items={"f": [item]}))
    selected = select_window_items(storage, NOW, {})
    assert {row["title"] for row in selected.values()} == {"overnight"}
    storage.cleanup()


def test_sent_and_uncertain_records_exclude_overlapping_news(tmp_path):
    key = item_key({"url": "https://example.com/a"})
    write_delivery_record(tmp_path / "2026-10-19.json", "uncertain", {key})
    assert read_sent_keys(tmp_path) == {key}
    write_delivery_record(tmp_path / "2026-10-19.json", "sent", {key})
    assert read_sent_keys(tmp_path) == {key}


@pytest.mark.parametrize("day,hour,minute,push", [(20, 7, 59, False), (20, 8, 0, True),
    (20, 9, 59, True), (20, 10, 0, True), (20, 13, 59, True), (20, 15, 59, True),
    (20, 16, 0, False),
    (24, 8, 0, False), (25, 8, 0, False), (26, 8, 0, True)])
def test_existing_scheduler_configuration(day, hour, minute, push):
    timeline = yaml.safe_load(open("config/timeline.yaml"))
    config = yaml.safe_load(open("config/config.yaml"))
    now = NOW.replace(day=day, hour=hour, minute=minute)
    schedule = Scheduler(config["schedule"], timeline, Mock(), lambda: now).resolve()
    assert schedule.collect
    assert schedule.push is push
    if push:
        assert schedule.once_push and schedule.analyze and schedule.report_mode == "daily"


def test_dated_filter_reuses_original_cache_and_preserves_summary(tmp_path):
    storage = LocalStorageBackend(str(tmp_path))
    save_news(storage, "2026-10-19", [hot("yesterday")])
    save_news(storage, "2026-10-20", [hot("today")], "07:30")
    save_news(storage, "2026-10-20", [hot("late")], "08:01")
    storage.save_rss_data(RSSData(date="2026-10-19", crawl_time="02:00", items={"f": [
        RSSItem(title="rss", feed_id="f", url="https://example.com/rss",
                crawl_time="02:00", published_at="2026-10-19T01:00:00+08:00", summary="原始 RSS 摘要")]}))
    config = {"AI": {}, "AI_FILTER": {"MIN_SCORE": .7, "BATCH_INTERVAL": 0}, "PLATFORMS": [{"id": "p"}],
              "RSS": {"ENABLED": True, "FEEDS": [{"id": "f"}],
                      "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 1}}}
    ctx = SimpleNamespace(config=config, get_time=lambda: NOW, get_storage_manager=lambda: storage)
    def classify(self, titles, tags, interests):
        return [{"news_item_id": t["id"], "tag_id": tags[0]["id"], "relevance_score": .9} for t in titles]
    with patch.object(AIFilter, "load_interests_content", return_value="科技"), \
         patch.object(AIFilter, "extract_tags", return_value=[{"tag": "科技", "description": "科技新闻"}]), \
         patch.object(AIFilter, "classify_batch", classify):
        hotlist, rss = build_window_stats(ctx, NOW, {})
        assert {t["title"] for g in hotlist for t in g["titles"]} == {"yesterday", "today"}
        assert rss[0]["titles"][0]["summary"] == "原始 RSS 摘要"
        # Repeated construction consumes the same daily classification cache.
        with patch.object(AIFilter, "classify_batch", side_effect=AssertionError("cache miss")):
            hot2, rss2 = build_window_stats(ctx, NOW, {})
        assert hot2 == hotlist and rss2 == rss
        excluded = {t["item_key"] for g in hotlist + rss for t in g["titles"]}
        assert build_window_stats(ctx, NOW, {}, sent_keys=excluded) == ([], [])
    storage.cleanup()


@pytest.mark.parametrize("response", ["", "{", "{}"])
def test_invalid_classification_is_not_successful_zero_matches(response):
    ai = AIFilter.__new__(AIFilter)
    ai.debug = False
    assert ai._parse_classify_response(response, [{"id": 1, "title": "新闻"}], [{"id": 1}]) is None
    assert ai._parse_classify_response("[]", [{"id": 1, "title": "新闻"}], [{"id": 1, "tag": "科技"}]) == []


def test_partial_classification_is_cached_and_only_failed_items_are_retried(tmp_path):
    storage = LocalStorageBackend(str(tmp_path))
    save_news(storage, "2026-10-19", [hot(f"item{i}") for i in range(4)])
    config = {"AI": {}, "AI_FILTER": {"BATCH_SIZE": 40, "BATCH_INTERVAL": 0},
              "PLATFORMS": [{"id": "p"}]}
    ctx = SimpleNamespace(config=config, get_time=lambda: NOW, get_storage_manager=lambda: storage)

    def classify(self, titles, tags, interests):
        if len(titles) > 2 or any(t["title"] in {"item2", "item3"} for t in titles):
            return None
        # item1 is a valid zero-match item; it should also be cached.
        return [{"news_item_id": t["id"], "tag_id": tags[0]["id"], "relevance_score": .9}
                for t in titles if t["title"] == "item0"]

    with patch.object(AIFilter, "load_interests_content", return_value="科技"), \
         patch.object(AIFilter, "extract_tags", return_value=[{"tag": "科技", "description": "科技新闻"}]), \
         patch.object(AIFilter, "classify_batch", classify):
        with pytest.raises(RuntimeError, match="早报 AI 筛选未完成"):
            build_window_stats(ctx, NOW, {})

        analyzed = storage.get_analyzed_news_ids("hotlist", interests_file="ai_interests.txt", date="2026-10-19")
        assert len(analyzed) == 2
        retried = []

        def recover(self, titles, tags, interests):
            retried.extend(t["title"] for t in titles)
            return [{"news_item_id": t["id"], "tag_id": tags[0]["id"], "relevance_score": .9}
                    for t in titles]

        with patch.object(AIFilter, "classify_batch", recover):
            hotlist, rss = build_window_stats(ctx, NOW, {})
        assert set(retried) == {"item2", "item3"}
        assert len(retried) == 2
        assert {t["title"] for g in hotlist for t in g["titles"]} == {"item0", "item2", "item3"}
        assert rss == []
    storage.cleanup()


def test_summary_survives_existing_report_and_feishu_formatter():
    title = {"title": "新闻", "source_name": "源", "time_display": "10-20 07:30",
             "count": 1, "ranks": [1], "rank_threshold": 10,
             "url": "https://example.com/a", "ai_summary": "内容分析：正文事实及意义。"}
    report = prepare_report_data([{"word": "科技", "count": 1, "titles": [title]}])
    line = format_title_for_platform("feishu", report["stats"][0]["titles"][0])
    assert "[新闻](https://example.com/a)" in line
    assert "\n内容分析：正文事实及意义。" in line


def test_independent_feed_keeps_configured_limit_and_summary(tmp_path):
    storage = LocalStorageBackend(str(tmp_path))
    items = [RSSItem(title=f"rss{i}", feed_id="f", url=f"https://example.com/{i}",
                     published_at=f"2026-10-19T0{i}:00:00+08:00", crawl_time="07:00", summary="来源摘要")
             for i in range(4)]
    storage.save_rss_data(RSSData(date="2026-10-20", crawl_time="07:00", items={"f": items}))
    config = {"DISPLAY": {"REGIONS": {"STANDALONE": True},
                          "STANDALONE": {"RSS_FEEDS": ["f"], "MAX_ITEMS": 2}}}
    ctx = SimpleNamespace(config=config, get_storage_manager=lambda: storage)
    excluded = {item_key({"url": "https://example.com/3"})}
    result = build_window_standalone(ctx, NOW, {}, excluded)
    selected = result["rss_feeds"][0]["items"]
    assert [r["title"] for r in selected] == ["rss2", "rss1"]
    assert all(r["summary"] == "来源摘要" for r in selected)
    import json
    assert json.loads(json.dumps(result))["rss_feeds"][0]["items"] == selected
    storage.cleanup()


def _window_analyzer(tmp_path):
    from trendradar.__main__ import NewsAnalyzer
    analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
    config = {"REPORT_WINDOW": {"enabled": True}, "ENABLE_NOTIFICATION": True,
              "STORAGE": {"LOCAL": {"DATA_DIR": str(tmp_path)}, "FORMATS": {"HTML": False}},
              "AI_ANALYSIS": {"ENABLED": True, "ITEM_SUMMARIES": True}}
    analyzer.ctx = Mock(config=config)
    analyzer.ctx.get_time.return_value = NOW
    analyzer.ctx.create_scheduler.return_value.already_executed.return_value = False
    analyzer.filter_method = "ai"
    analyzer.interests_file = None
    analyzer._has_notification_configured = Mock(return_value=True)
    analyzer._send_notification_if_needed = Mock(return_value=True)
    analyzer.ctx.create_notification_dispatcher.return_value.translate_content.side_effect = (
        lambda rd, rss_items=None, standalone_data=None: (rd, rss_items, None, standalone_data))
    return analyzer


@pytest.mark.parametrize("send_result,status", [(True, "sent"), (False, "uncertain")])
def test_pipeline_does_not_repeat_success_or_uncertain_delivery(tmp_path, send_result, status):
    import json
    analyzer = _window_analyzer(tmp_path)
    analyzer._send_notification_if_needed.return_value = send_result
    schedule = SimpleNamespace(push=True, once_push=True, period_key="morning")
    stats = [{"word": "科技", "count": 1, "titles": [{"title": "新闻", "item_key": "a"}]}]
    with patch("trendradar.core.report_window.build_window_stats", return_value=(stats, [])), \
         patch("trendradar.core.report_window.build_window_standalone", return_value=None), \
         patch("trendradar.ai.item_summary.enrich_item_summaries"):
        if send_result:
            analyzer._execute_window_report(schedule, [])
        else:
            with pytest.raises(RuntimeError, match="核实"):
                analyzer._execute_window_report(schedule, [])
        analyzer._execute_window_report(schedule, [])
    assert analyzer._send_notification_if_needed.call_count == 1
    record = json.loads((tmp_path / "briefings/2026-10-20.json").read_text())
    assert record["status"] == status


@pytest.mark.parametrize("content_kind", ["hotlist", "standalone", "rss"])
def test_delivery_after_real_translation_handles_empty_rss(tmp_path, content_kind):
    import json
    from trendradar.notification.dispatcher import NotificationDispatcher

    analyzer = _window_analyzer(tmp_path)
    translator = SimpleNamespace(enabled=True, target_language="中文",
                                 scope={"HOTLIST": False, "RSS": False, "STANDALONE": False})
    analyzer.ctx.create_notification_dispatcher.return_value = NotificationDispatcher(
        analyzer.ctx.config, lambda: NOW, Mock(), translator=translator,
    )
    group = {"word": "科技", "count": 1,
             "titles": [{"title": "新闻", "item_key": "a", "ai_summary": "正文分析"}]}
    stats = [group] if content_kind == "hotlist" else []
    rss = [group] if content_kind == "rss" else []
    standalone = ({"platforms": [], "rss_feeds": [{"items": group["titles"]}]}
                  if content_kind == "standalone" else None)
    schedule = SimpleNamespace(push=True, once_push=True, period_key="morning")
    with patch("trendradar.core.report_window.build_window_stats", return_value=(stats, rss)), \
         patch("trendradar.core.report_window.build_window_standalone", return_value=standalone), \
         patch("trendradar.ai.item_summary.enrich_item_summaries"):
        analyzer._execute_window_report(schedule, [])
        analyzer._execute_window_report(schedule, [])
    analyzer._send_notification_if_needed.assert_called_once()
    assert analyzer._send_notification_if_needed.call_args.kwargs["rss_items"] == rss
    record = json.loads((tmp_path / "briefings/2026-10-20.json").read_text())
    assert record["status"] == "sent"
    assert read_sent_keys(tmp_path / "briefings") == {"a"}


def test_failed_summary_never_sends_or_claims_delivery(tmp_path):
    analyzer = _window_analyzer(tmp_path)
    schedule = SimpleNamespace(push=True, once_push=True, period_key="morning")
    stats = [{"word": "科技", "count": 1, "titles": [{"title": "新闻", "item_key": "a"}]}]
    with patch("trendradar.core.report_window.build_window_stats", return_value=(stats, [])), \
         patch("trendradar.core.report_window.build_window_standalone", return_value=None), \
         patch("trendradar.ai.item_summary.enrich_item_summaries", side_effect=ValueError("bad AI")):
        with pytest.raises(ValueError):
            analyzer._execute_window_report(schedule, [])
    analyzer._send_notification_if_needed.assert_not_called()
    assert not (tmp_path / "briefings/2026-10-20.json").exists()


def test_failed_classification_never_sends_or_claims_delivery(tmp_path):
    analyzer = _window_analyzer(tmp_path)
    schedule = SimpleNamespace(push=True, once_push=True, period_key="morning")
    with patch("trendradar.core.report_window.build_window_stats",
               side_effect=RuntimeError("早报 AI 筛选未完成")):
        with pytest.raises(RuntimeError, match="早报 AI 筛选未完成"):
            analyzer._execute_window_report(schedule, [])
    analyzer._send_notification_if_needed.assert_not_called()
    assert not (tmp_path / "briefings/2026-10-20.json").exists()


def test_html_and_actual_feishu_splitter_show_every_summary():
    from trendradar.notification.splitter import split_content_into_batches
    from trendradar.report.html import render_html_content
    title = {"title": "新闻", "source_name": "源", "time_display": "10-20 07:30",
             "count": 1, "ranks": [1], "rank_threshold": 10, "url": "https://example.com/a",
             "mobile_url": "", "ai_summary": "内容分析：热榜正文分析。"}
    report = prepare_report_data([{"word": "科技", "count": 1, "titles": [title]}])
    report["report_type"] = "工作日早报 · 10-19 00:00—10-20 08:00"
    rss = [{"word": "订阅", "count": 1, "titles": [{**title, "title": "RSS新闻", "ai_summary": "RSS内容摘要。"}]}]
    standalone = {"platforms": [], "rss_feeds": [{"id": "f", "name": "独立源", "items": [
        {"title": "独立新闻", "url": "https://example.com/b", "published_at": "2026-10-19T07:30:00+08:00",
         "ai_summary": "独立源内容摘要。"}]}]}
    content = "\n".join(split_content_into_batches(report, "feishu", rss_items=rss,
                                                standalone_data=standalone, get_time_func=lambda: NOW))
    html = render_html_content(report, 1, rss_items=rss, standalone_data=standalone,
                               get_time_func=lambda: NOW)
    for summary in ("内容分析：热榜正文分析。", "RSS内容摘要。", "独立源内容摘要。"):
        assert summary in content
        assert summary in html
    assert report["report_type"] in html
