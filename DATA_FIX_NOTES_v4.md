# v4 数据正确性修复

## 今日 OHLC
- Review 今日开盘/收盘现在只允许雪球详情、腾讯、新浪的“当日实时行情”提供。
- 删除 Yahoo/历史 market-kline/df_hist_all/未验证实时价对“今日开盘”的兜底。
- 所有 OHLC 先做 `low <= open/high/close` 等硬校验。
- 当所有实时源都失败时，明确显示“今日行情无法确认”，不会再把旧交易日开盘价冒充今天。

## 推荐历史
- 保留 `scan_recommendation_history.csv`：每次 Scan 实际推荐先记录，再做持仓去重。
- Review 按当前持仓生命周期读取推荐历史；已有旧数据无法凭空恢复，后续运行会持续累积。
