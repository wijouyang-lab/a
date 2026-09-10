# A股 Scan → Review 推荐历史修复

scan.py 已写入 `scan_recommendation_history.csv`，但原工作流未持久化该文件到 GitHub，导致后续 review 无法读取此前的 Scan 推荐事件。

本版本：
- scan.yml 提交 `scan_recommendation_history.csv`；
- review.yml 保护该文件；
- 提供空表头文件，保证首次运行后即可持久化；
- 不改变 trade_history 的实际建仓语义；
- review.py 原有推荐历史合并逻辑继续保留。
