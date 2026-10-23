"""Classification retries stay within one run and preserve per-item success."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

from trendradar.ai.filter import AIFilter
from trendradar.ai.filter_pipeline import AIFilterPipeline


NOW = datetime(2026, 10, 22, 9, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
TAGS = [{"id": 1, "tag": "科技", "description": "科技新闻"}]


def news(count):
    return [{"id": i, "title": f"新闻{i}", "source_name": "来源"}
            for i in range(1, count + 1)]


def matches(titles):
    return [{"news_item_id": t["id"], "tag_id": 1, "relevance_score": .9}
            for t in titles]


def response(content, reason="stop", tokens=100):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=reason)],
        usage=SimpleNamespace(completion_tokens=tokens),
    )


def pipeline():
    return AIFilterPipeline({}, Mock(), lambda: NOW)


def ai_filter():
    return AIFilter({"MODEL": "test/model", "API_KEY": "test-key"},
                    {"PROMPT_FILE": "prompt.txt", "EXTRACT_PROMPT_FILE": "extract_prompt.txt"},
                    lambda: NOW)


def test_empty_length_response_recovers_37_items_in_same_run(capsys):
    items = news(37)
    ai = ai_filter()
    recovered = [response(json.dumps([
        {"id": t["id"], "tag_id": 1, "score": .9} for t in batch
    ])) for batch in (items[:18], items[18:])]
    with patch("trendradar.ai.client.completion", side_effect=[
        response("", "length", 5000), *recovered,
    ]) as completion:
        results, hot_ids, rss_ids = pipeline()._classify_batches(
            ai, items, [], TAGS, "科技兴趣", {"BATCH_SIZE": 40, "BATCH_INTERVAL": 0},
        )
    assert completion.call_count == 3
    assert hot_ids == list(range(1, 38))
    assert rss_ids == []
    assert results == [{**r, "source_type": "hotlist"} for r in matches(items)]
    assert "finish_reason=length, completion_tokens=5000, empty_content=True" in capsys.readouterr().out


@pytest.mark.parametrize("bad_response", ["{", "{}", OSError("temporary failure")])
def test_bad_response_and_request_error_use_same_bounded_recovery(bad_response):
    items = news(4)
    ai = ai_filter()
    first = bad_response if isinstance(bad_response, Exception) else response(bad_response)
    with patch("trendradar.ai.client.completion", side_effect=[
        first, response("[]"), response("[]"),
    ]) as completion:
        results, hot_ids, _ = pipeline()._classify_batches(
            ai, items, [], TAGS, "科技兴趣", {"BATCH_INTERVAL": 0},
        )
    assert completion.call_count == 3
    assert hot_ids == [1, 2, 3, 4]
    assert results == []


def test_hotlist_and_rss_retries_share_interval_and_keep_separate_ids():
    # Both source tables can use the same numeric IDs.
    ai = Mock()
    ai.classify_batch.side_effect = [None, [], matches(news(2)[1:]), None, [], []]
    with patch("time.sleep") as sleep:
        results, hot_ids, rss_ids = pipeline()._classify_batches(
            ai, news(2), news(2), TAGS, "科技兴趣",
            {"BATCH_SIZE": 40, "BATCH_INTERVAL": 2},
        )
    assert ai.classify_batch.call_count == 6
    assert sleep.call_count == 5
    assert all(call.args == (2,) for call in sleep.call_args_list)
    assert hot_ids == rss_ids == [1, 2]
    assert results == [{**r, "source_type": "hotlist"} for r in matches(news(2)[1:])]
    for call in ai.classify_batch.call_args_list:
        assert call.args[1:] == (TAGS, "科技兴趣")
        assert all(t["source"] == "来源" for t in call.args[0])


@pytest.mark.parametrize("count,expected_calls", [(1, 3), (37, 7)])
def test_permanent_failure_stops_without_marking_items_analyzed(count, expected_calls):
    ai = Mock()
    ai.classify_batch.return_value = None
    assert pipeline()._classify_batches(
        ai, news(count), [], TAGS, "科技兴趣", {"BATCH_INTERVAL": 0},
    ) == ([], [], [])
    assert ai.classify_batch.call_count == expected_calls
