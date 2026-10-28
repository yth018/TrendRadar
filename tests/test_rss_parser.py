"""Network-free RSS timestamp normalization tests."""

import json

from trendradar.crawler.rss.parser import RSSParser


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example</title><link>https://example.com/</link>
<item><title>GMT item</title><link>https://example.com/a</link><guid>a</guid>
<pubDate>Wed, 28 Oct 2026 00:22:00 GMT</pubDate><description>Body</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Example</title>
<entry><title>Offset item</title><link href="https://example.com/b"/><id>b</id>
<updated>2026-10-28T08:22:00+08:00</updated><summary>Body</summary></entry></feed>"""


def test_rss_gmt_date_keeps_utc_offset():
    parsed = RSSParser().parse(RSS)
    assert len(parsed) == 1
    assert parsed[0].published_at == "2026-10-28T00:22:00+00:00"


def test_atom_offset_is_normalized_to_utc():
    parsed = RSSParser().parse(ATOM)
    assert len(parsed) == 1
    assert parsed[0].published_at == "2026-10-28T00:22:00+00:00"


def test_json_feed_date_without_offset_is_treated_as_utc():
    content = json.dumps({
        "version": "https://jsonfeed.org/version/1.1",
        "items": [{"id": "c", "title": "JSON item", "url": "https://example.com/c",
                   "date_published": "2026-10-28T00:22:00"}],
    })
    parsed = RSSParser().parse(content)
    assert len(parsed) == 1
    assert parsed[0].published_at == "2026-10-28T00:22:00+00:00"
