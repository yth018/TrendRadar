# 工作日八点早报

本仓库使用原有 `office_hours` 预设、星期映射、`once.push`、SQLite 日库、兴趣筛选、模型配置、翻译及通知渠道。早报只新增跨日取数、逐条摘要和发送条目去重；不另建定时服务或更换数据源。

## 当前规则

- 时区：`Asia/Shanghai`。周一至周五早上 08:00 触发，周末仅采集；不识别法定节假日及调休。
- `config/timeline.yaml` 的 `office_hours.periods.morning_briefing` 为 08:00—10:00，`once.push: true`。窗口用于容纳 Actions 排队延迟，不代表重复推送。超过 10:00 不自动补发。
- 保留每小时第 33 分钟采集，另加 UTC `0 0 * * 1-5`。Actions 可能延迟，抓取和模型处理也需时间，不能保证 08:00 整送达。
- `config/config.yaml` 的 `report.window.enabled: true`、`cutoff: "08:00"` 把 daily 数据范围扩展为昨日 00:00 至今日 08:00（包含边界）。延迟执行不改变截止时间；周一不额外补入周五和周六。
- RSS 优先按源发布时间，缺失/无法解析时按首次采集并标注。热榜按保留历史内的首次采集时间；每小时采样可能漏掉两次采集之间短暂上榜的内容，也不能把首次采集当原文发布时间。
- 已发过或发送结果待核实的条目不会进入后续早报。跨源相同 URL 去重，缺 URL 时按标题去重；不同 URL 的同一事件不做语义合并。
- 继续使用 `ai_interests.txt`、原分类提示词、`min_score`、标签优先级及已配置独立订阅源。独立订阅区同样限制在窗口内，并去掉已经在正文展示的条目。
- `ai_analysis.item_summaries: true` 为每条附 1—2 句中文摘要，沿用 `ai` 模型及 Secret。RSS 有源摘要时据此概括；只有标题则标注“标题简述”，不补充来源未提供的事实。早报以逐条摘要替代整份趋势分析。
- 无符合条件的新内容时沿用跳过通知的行为。摘要或分类失败会让运行失败，不把空/损坏的模型响应当成合法的零匹配结果。

## 数据保存与恢复

Actions 通过已有环境变量选择 local SQLite 后端并将日库保留七天；本地配置的 auto 后端和永久保留设置不变。工作流从同分支、同工作流最新的 `trendradar-state` artifact 恢复 `output/news`、`output/rss`、`output/briefings`；每次运行后再保存，产物保留七天。这里保存的是新闻、模型结果和发送状态，不包含 API key 或 webhook。

初次部署没有历史日库，热榜无法补回部署前已经消失的新闻。`coverage.json` 记录本次采集链的起点，早报会注明历史采集不足；需持续采集覆盖完整窗口后才有完整的已采样历史。API/下载/解压失败会停止执行，避免带空状态继续推送。

取消七天签到自动停用。原 `Check In` 手动清理改为保留最近七天运行及至少十次运行，以免删除有效状态产物。不要手工删除所有近期运行或产物，否则会丢失历史和发送记录。

每天的 `output/briefings/YYYY-MM-DD.json` 记录 `attempted`、`sent` 或 `uncertain`。正常成功沿用调度器 `once` 记录；部分发送失败或结果不明时保留记录，停止自动重发，先核实接收端。原通知器可能将长早报拆成多个消息批次，“一次”指一次早报发送任务。

Actions 串行运行且不主动取消正在执行的任务。状态在运行结束后上传；运行器异常终止且产物未保存时，不具备与外部通知接口之间的原子事务保证。恢复/保存失败应查看 Actions 报错并核实接收端。

## 验证与维护

```bash
# 显示原调度器解析结果，不发送
uv run python -m trendradar --show-schedule

# 真实抓取并生成当前日期早报预览，调用 AI，但绝不发通知
uv run python -m trendradar --preview-report

# 正常采集流程，禁用通知；不会强制在窗口外生成早报
uv run python -m trendradar --no-notify

# 离线回归
python -m pytest -q
```

GitHub 手动运行 `Get Hot News` 时，`dry_run` 默认开启，使用 `--preview-report`。预览位于状态产物的 `briefings/preview.json` 和 `briefings/preview.html`。预览不写入日期发送记录，也不占用当天推送次数。8 点以前预览不会提前生成尚未截止的早报。

关闭 `report.window.enabled` 会回到上游原有报告流程；原有 `daily` 本身仍仅表示当日汇总。需要改推送星期或时间时，继续编辑 `timeline.yaml`；同时修改窗口 `cutoff` 与 Actions 对应触发时间，避免两者不一致。
