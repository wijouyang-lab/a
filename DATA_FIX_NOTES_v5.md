# v5 数据修复

## 根因
v4 的当日 OHLC 已经禁止 Yahoo/历史 K 线兜底，但新增的腾讯/新浪实时行情函数使用了 `urllib.request` 却没有在 `review.py` 顶层导入 `urllib`。异常被函数内部吞掉，因此腾讯/新浪全部静默返回空值，最终 40 只持仓全部被跳过。

另外，v4 的腾讯字段索引也错了一位：标准布局为 `2=当前价、3=昨收、4=今开、5=最高、6=最低`，旧代码把代码/昨收等字段错当成 OHLC，硬校验会失败。

## v5 修复
- 顶层增加 `import urllib.request`，让腾讯/新浪实时源真正可用。
- 修正腾讯 OHLC 字段映射：current=f[2], open=f[4], high=f[5], low=f[6]。
- 腾讯盘后数据必须存在并匹配目标日期，否则拒绝。
- 保持“今日行情宁缺毋滥”：Yahoo、历史 market-kline、df_hist_all 不再冒充今日 OHLC。
- 保留 `scan_recommendation_history.csv` 的推荐历史逻辑。
