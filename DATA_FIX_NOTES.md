# A股 Review 当日行情修复

本次修复针对盘后复盘中“今日实际行情”偶发读取上一交易日开盘价的问题。

核心调整：
- `review.py`：盘后当日 OHLC 优先读取雪球详情报价 `open/high/low/current`，再回退到精确日期日K。
- `xueqiu_client.py`：`get_daily_ohlc_on_date()` 对“今天”优先读取雪球详情报价，避免历史日K尚未刷新时静默命中上一交易日。
- 保留原有 Yahoo 精确日期备用层，不允许用上一交易日 close 冒充今日 close。

已验证：
- 模拟雪球日K仅有上一交易日数据（例如 11.51）时，今天详情报价 `12.31/12.56/12.08/12.11` 会被正确优先采用。
- `review.py`、`xueqiu_client.py` 均通过 `py_compile`。

## 2026-09-08 recommendation history enhancement
- Review now displays the current holding lifecycle's recommendation history in each holding card.
- Shows total recommendation count, each recommendation date, and the recorded `Open_Price` as the recommendation/entry price (falls back to `Close_Price` only when `Open_Price` is unavailable).
- The first recommendation date/price remains separately displayed.
- Added `Rec_History` to `review_history.csv` for persistence.
- The recommendation history is derived from the active lifecycle only; a prior lifecycle separated by `Stop_Loss_Hit`, `Period_Matured`, `Forced_Exit`, etc. is not merged into the new holding.
