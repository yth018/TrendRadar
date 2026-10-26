"""One authorized delivery of the already-reviewed 2026-10-26 briefing."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trendradar.__main__ import NewsAnalyzer
from trendradar.core.loader import load_config
from trendradar.core.report_window import read_sent_keys


def main():
    directory = Path('output/briefings')
    data = json.loads((directory / 'preview.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert digest == '367f303e8ba0988192c2a3211c323612390883d1475dc4a4640f12abd30f4496', 'Preview changed; refuse delivery'
    assert data['window_end'] == '2026-10-26T08:00:00+08:00'
    items = [t for g in data['stats'] + (data['rss'] or []) for t in g['titles']]
    items += [t for groups in (data['standalone'] or {}).values() for g in groups for t in g['items']]
    keys = {t['item_key'] for t in items}
    assert len(items) == len(keys) == 79 and all(t.get('ai_summary') for t in items)
    record = directory / '2026-10-26.json'
    assert not record.exists(), 'Delivery already attempted; refuse duplicate'
    assert keys.isdisjoint(read_sent_keys(directory)), 'Previously attempted items; refuse duplicate'

    config = load_config()
    assert config.get('FEISHU_WEBHOOK_URL'), 'Feishu channel missing'
    for name in ('TELEGRAM_BOT_TOKEN', 'DINGTALK_WEBHOOK_URL', 'WEWORK_WEBHOOK_URL',
                 'EMAIL_FROM', 'EMAIL_TO', 'NTFY_SERVER_URL', 'NTFY_TOPIC', 'BARK_URL',
                 'SLACK_WEBHOOK_URL', 'GENERIC_WEBHOOK_URL'):
        config[name] = ''
    config['ENABLE_NOTIFICATION'] = True
    config['PREVIEW_REPORT'] = False
    config['SHOW_VERSION_UPDATE'] = False
    analyzer = NewsAnalyzer(config)
    analyzer.filter_method = 'ai'
    assert analyzer.ctx.format_date() == '2026-10-26', 'Authorization is for 2026-10-26 only'
    schedule = replace(analyzer.ctx.create_scheduler().resolve(), push=True, once_push=True,
                       period_key='morning_briefing', period_name='工作日八点早报', report_mode='daily')
    print('LIVE_DELIVERY_VERIFIED_INPUT items=79 cutoff=2026-10-26T08:00:00+08:00')
    # Reuse reviewed content; exercise the real translation, delivery and state-writing path.
    try:
        with patch('trendradar.core.report_window.build_window_stats', return_value=(data['stats'], data['rss'] or [])), \
             patch('trendradar.core.report_window.build_window_standalone', return_value=data['standalone']), \
             patch('trendradar.ai.item_summary.enrich_item_summaries'):
            analyzer._execute_window_report(schedule, [])
        saved = json.loads(record.read_text())
        assert saved['status'] == 'sent' and set(saved['item_keys']) == keys
        print('LIVE_DELIVERY_RESULT sent=True recorded_items=79')
    finally:
        analyzer.ctx.get_storage_manager().cleanup()


if __name__ == '__main__':
    main()
