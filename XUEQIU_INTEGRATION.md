# 雪球优先数据源说明

本版本将雪球设为 A 股 Scan / Review 的第一股票数据源。

## 数据优先级

### Scan
1. 雪球：Top 300 活跃榜、实时行情、日线 K 线、个股动态、PE/PB
2. 非行情源/其他数据源：行业元数据、资金流，以及雪球失败时的行情/K线兜底
3. Eastmoney → 新浪 → Yahoo：历史 K 线最终兜底

### Review
1. 雪球：当日 OHLC、当前价格、持仓历史 K 线
2. 非行情源/其他数据源：雪球失败时的当日/历史行情兜底

宏观数据仍使用现有官方/原有接口，不强行替换为雪球，因为这些数据不是雪球股票行情接口的职责。

## GitHub Actions Secret

建议在仓库 Settings → Secrets and variables → Actions 中增加：

`XUEQIU_COOKIE`

值填写浏览器雪球站点的 Cookie 字符串，例如包含 `xq_a_token=...` 的完整 Cookie。

该 Secret 是可选的。没有时程序会先访问雪球首页/股票页尝试获得匿名 Cookie；但 GitHub Actions 的长期稳定性通常使用自己的 Cookie 更好。

## 重要安全规则

不要把 `XUEQIU_COOKIE` 写进代码、CSV、日志或 Git 提交。

雪球接口可能出现 403/429/Cookie 失效，因此本版本保留完整备用数据链路，不会因为雪球单点失败而让 Scan/Review 直接退出。

## 日期口径

Review 获取“某交易日开盘/最高/最低/收盘”时，雪球 K 线必须命中目标日期才使用；不会把前一交易日的收盘价静默当作目标日期价格。

这条规则用于避免盘前/盘后复盘时出现收益方向被错误反转的问题。
