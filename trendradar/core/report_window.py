"""跨日早报的数据选择；沿用日库、AI 筛选及报告数据结构。"""

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trendradar.ai.filter_pipeline import AIFilterPipeline


def item_key(item):
    """跨来源 URL 去重；缺少 URL 时按标题去重。"""
    value = item.get("url") or "title:" + item.get("title", "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def window_bounds(now, settings):
    hour, minute = map(int, settings.get("cutoff", "08:00").split(":"))
    end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    start = (end - timedelta(days=1)).replace(hour=0, minute=0)
    return start, end


def _timestamp(value, day, tz, assume_utc=False):
    if not value:
        return None
    try:
        if len(value) <= 5:
            value = day + "T" + value.replace("-", ":")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # RSS dates without an offset came from feedparser's UTC struct_time.
            # Treat them as UTC so legacy rows stay aligned with the local window.
            source_tz = timezone.utc if assume_utc else tz
            parsed = (source_tz.localize(parsed) if hasattr(source_tz, "localize")
                      else parsed.replace(tzinfo=source_tz))
        return parsed.astimezone(tz)
    except (ValueError, TypeError):
        return None


def select_window_items(storage, now, settings, sent_keys=()):
    """读取保留的七天日库，避免跨日重复上榜被误判为新增。"""
    start, end = window_bounds(now, settings)
    first_seen = {}
    candidates = {}
    for offset in range(6, -1, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        for kind, data in (("hotlist", storage.get_today_all_data(day)),
                           ("rss", storage.get_rss_data(day))):
            if not data:
                continue
            for source_id, items in data.items.items():
                for item in items:
                    row = {"title": item.title, "url": item.url, "source_id": source_id,
                           "source_type": kind, "summary": getattr(item, "summary", ""),
                           "source_name": data.id_to_name.get(source_id, source_id),
                           "ranks": getattr(item, "ranks", []), "rank": getattr(item, "rank", 0)}
                    key = item_key(row)
                    observed = _timestamp(item.first_time or item.crawl_time, day, now.tzinfo)
                    if observed is None:
                        continue
                    first_seen[key] = min(first_seen.get(key, observed), observed)
                    published = _timestamp(
                        getattr(item, "published_at", ""), day, now.tzinfo, assume_utc=True
                    )
                    stamp = published if kind == "rss" and published else first_seen[key]
                    basis = "发布时间" if kind == "rss" and published else "首次采集"
                    if start <= stamp <= end and key not in sent_keys:
                        row.update(key=key, timestamp=stamp, time_basis=basis)
                        lookup = (kind, source_id, item.url or item.title)
                        # Prefer a source summary when the same RSS entry appears on both days.
                        if lookup not in candidates or row["summary"]:
                            candidates[lookup] = row
    return candidates


def build_window_stats(ctx, now, settings, interests_file=None, sent_keys=()):
    """复用原有兴趣、分类提示词、分数门槛、标签排序及日库分类缓存。"""
    start, end = window_bounds(now, settings)
    storage = ctx.get_storage_manager()
    candidates = select_window_items(storage, now, settings, sent_keys)
    platforms = {p["id"] for p in ctx.config.get("PLATFORMS", [])}
    feeds = {f["id"] for f in ctx.config.get("RSS", {}).get("FEEDS", []) if f.get("enabled", True)}
    candidates = {k: v for k, v in candidates.items()
                  if (k[0] == "hotlist" and k[1] in platforms and ctx.config.get("ENABLE_CRAWLER", True))
                  or (k[0] == "rss" and k[1] in feeds and ctx.config.get("RSS", {}).get("ENABLED", False))}
    config = copy.deepcopy(ctx.config)
    # 精确窗口代替 RSS 滚动 N 天过滤，避免遗漏昨日 00:00—08:00。
    config.setdefault("RSS", {}).setdefault("FRESHNESS_FILTER", {})["ENABLED"] = False
    pipeline = AIFilterPipeline(config, storage, ctx.get_time)
    groups = {}
    seen = set()
    for day in (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")):
        result = pipeline.run(interests_file, date=day)
        if not result or not result.success:
            raise RuntimeError("早报 AI 筛选未完成，保留数据等待下次运行")
        for tag in result.tags:
            selected = []
            for item in tag["items"]:
                lookup = (item.get("source_type", "hotlist"), item["source_id"],
                          item.get("url") or item["title"])
                source = candidates.get(lookup)
                if not source or source["key"] in seen:
                    continue
                if item.get("relevance_score", 0) < config.get("AI_FILTER", {}).get("MIN_SCORE", 0):
                    continue
                seen.add(source["key"])
                selected.append(item)
            tag["items"] = selected
            tag["count"] = len(selected)
        hotlist, rss, _ = pipeline.convert_to_report_data(result, mode="daily")
        for kind, stats in (("hotlist", hotlist), ("rss", rss)):
            for group in stats:
                key = (kind, group["word"])
                target = groups.setdefault(key, {**group, "titles": [], "count": 0})
                for title in group["titles"]:
                    source = candidates[(kind, title["source_id"], title.get("url") or title["title"])]
                    title["summary"] = source["summary"]
                    title["item_key"] = source["key"]
                    title["time_display"] = source["time_basis"] + " " + source["timestamp"].strftime("%m-%d %H:%M")
                    target["titles"].append(title)
                target["count"] = len(target["titles"])
    hotlist = [g for (kind, _), g in groups.items() if kind == "hotlist"]
    rss = [g for (kind, _), g in groups.items() if kind == "rss"]
    priority = config.get("FILTER", {}).get("PRIORITY_SORT_ENABLED", False)
    for stats in (hotlist, rss):
        stats.sort(key=lambda g: (g.get("position", 9999), -g["count"]) if priority
                   else (-g["count"], g.get("position", 9999)))
    return hotlist, rss


def build_window_standalone(ctx, now, settings, excluded_keys):
    """保留原独立源配置与条数上限，但同样受时间窗口和已发去重约束。"""
    display = ctx.config.get("DISPLAY", {})
    if not display.get("REGIONS", {}).get("STANDALONE", False):
        return None
    settings_sa = display.get("STANDALONE", {})
    sources = {"hotlist": settings_sa.get("PLATFORMS", []),
               "rss": settings_sa.get("RSS_FEEDS", [])}
    candidates = select_window_items(ctx.get_storage_manager(), now, settings, excluded_keys)
    result = {"platforms": [], "rss_feeds": []}
    seen = set(excluded_keys)
    for kind, target in (("hotlist", "platforms"), ("rss", "rss_feeds")):
        for source_id in sources[kind]:
            rows = [r for r in candidates.values() if r["source_type"] == kind and r["source_id"] == source_id]
            rows.sort(key=lambda r: r["timestamp"], reverse=True)
            limit = settings_sa.get("MAX_ITEMS", 20)
            items = []
            for row in rows:
                if row["key"] in seen:
                    continue
                seen.add(row["key"])
                stamp = row["timestamp"].isoformat()
                item = {k: v for k, v in row.items() if k not in {"timestamp", "key"}}
                items.append({**item, "item_key": row["key"], "published_at": stamp,
                              "first_time": row["timestamp"].strftime("%H:%M"), "count": 1})
                if limit > 0 and len(items) >= limit:
                    break
            if items:
                result[target].append({"id": source_id, "name": rows[0]["source_name"], "items": items})
    return result if any(result.values()) else None


def read_sent_keys(directory):
    """发送中/结果不明也不自动重发，交由人工核实后处理。"""
    keys = set()
    for path in Path(directory).glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") in {"attempted", "sent", "uncertain"}:
            keys.update(record.get("item_keys", []))
    return keys


def write_delivery_record(path, status, keys):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"status": status, "item_keys": sorted(keys)}, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
