# -*- coding: utf-8 -*-
"""
A股策略进化引擎 evolve_a.py

真正的进化闭环：
  trade_history.csv（历史交易结果）
      ↓
  多维度绩效分析（按板块/评分区间/技术信号/持股周期分拆）
      ↓
  AI 识别哪些条件下胜率高、哪些在拖后腿
      ↓
  生成具体规则补丁 → 写入 evolved_rules.json
      ↓
  scan.py 下次运行时自动读取 evolved_rules.json 并注入 AI prompt
      ↓（等待下次交易结果）
  下一轮进化

与旧版的本质区别：
  旧版：生成一条 new_prompt_rule 字符串 → print 出来 → 永远不被使用
  新版：规则写进文件 → scan.py 启动时读取 → 真正影响选股行为
"""

import pandas as pd
import os
import json
import anthropic
import datetime
import re

# ── 配置 ──
EVOLVE_MODEL   = "claude-opus-4-8"
HISTORY_FILE   = "trade_history.csv"
REVIEW_FILE    = "review_history.csv"   # A股卖出价写在这里，不在trade_history.csv
EVOLVE_LOG     = "strategy_evolution.json"
EVOLVED_RULES  = "evolved_rules.json"   # scan.py 会读取这个文件

# 触发进化的最小已平仓样本数（太少则统计无意义）
MIN_CLOSED = 8

# A股账本字段
CLOSED_TAGS  = {"Stop_Loss_Hit", "Period_Matured", "Forced_Exit", "Dropped"}
ACTIVE_TAGS  = {"Core_Double_Dragon", "Core_Dragon", "Sub_Pioneer"}
PRICE_COL    = "Close_Price"   # 买入价列名
EXIT_COL     = "Exit_Price"    # trade_history.csv里可能为N/A，会从review_history.csv补充
SCORE_COL    = "Score"
INDUSTRY_COL = "Industry"


# ============================================================
# 1. 多维度绩效指标计算
# ============================================================
def safe_float(val, default=None):
    try:
        v = float(str(val).strip().replace(",", ""))
        return v if v > 0 else default
    except Exception:
        return default


def _load_evolution_boundaries():
    """
    从 strategy_evolution.json 读取历次进化发生的时间点，作为"世代"分界线。
    第0代 = 第一次进化之前的所有交易（原始策略，还没被任何规则调整过）
    第N代 = 第N次进化生效之后、第N+1次进化之前产生的交易
    """
    if not os.path.exists(EVOLVE_LOG):
        return []
    try:
        with open(EVOLVE_LOG, "r", encoding="utf-8") as f:
            history = json.load(f)
        dates = [entry.get("date", "")[:10] for entry in history if entry.get("date")]
        return sorted(set(d for d in dates if d))
    except Exception:
        return []


def _segment_by_generation(df_c, boundaries):
    """
    把已平仓交易按"进化世代"切段分别算胜率——这是回答"进化之后到底有没有变好"
    的关键：如果只看一个把全部历史混在一起的总胜率，早期（可能很差的）原始策略
    表现会和最近几轮进化后的表现混在一起，看不出规则调整到底有没有效果，对
    "刚调整过规则"的这一段也不公平（它的样本会被历史其他世代的表现稀释）。
    """
    if not boundaries or "date" not in df_c.columns:
        return {}, None

    df_c = df_c.copy()
    df_c["_dt"] = pd.to_datetime(df_c["date"], errors="coerce")
    bounds_dt = [pd.to_datetime(b) for b in boundaries]
    edges = [pd.Timestamp.min] + bounds_dt + [pd.Timestamp.max]

    segments = {}
    for i in range(len(edges) - 1):
        label = "第0代-进化前(原始策略)" if i == 0 else f"第{i}代-进化后"
        seg = df_c[(df_c["_dt"] >= edges[i]) & (df_c["_dt"] < edges[i + 1])]
        if len(seg) >= 2:
            segments[label] = {
                "样本数":    int(len(seg)),
                "胜率":      round(float((seg["pnl_pct"] > 0).sum() / len(seg) * 100), 1),
                "平均盈亏%": round(float(seg["pnl_pct"].mean()), 2),
            }

    # 单独拎出来"最近一次进化之后"这一段——这是当前规则版本真正的战绩，
    # 不该被进化之前、或者更早几轮规则的历史表现稀释。
    since_last = None
    if len(edges) > 2:
        seg = df_c[df_c["_dt"] >= edges[-2]]
        if len(seg) >= 2:
            since_last = {
                "样本数":    int(len(seg)),
                "胜率":      round(float((seg["pnl_pct"] > 0).sum() / len(seg) * 100), 1),
                "平均盈亏%": round(float(seg["pnl_pct"].mean()), 2),
            }
        elif len(seg) > 0:
            since_last = {"样本数": int(len(seg)), "提示": "样本数不足2笔，暂不单独计算胜率"}

    return segments, since_last


def calculate_metrics(df: pd.DataFrame) -> dict | None:
    """
    从账本计算多维度绩效指标。

    A股的卖出价（Cur_Price）写在 review_history.csv，不在 trade_history.csv。
    这里先尝试从 review_history.csv 建立 Ticker→卖出价 的映射，
    再回填到平仓记录里。
    """
    if df.empty:
        return None

    closed = df[df["Tag"].isin(CLOSED_TAGS)].copy()
    active = df[df["Tag"].isin(ACTIVE_TAGS)].copy()

    if len(closed) < MIN_CLOSED:
        print(f"⚠️ 已平仓记录仅 {len(closed)} 条，不足 {MIN_CLOSED} 条，暂缓进化。")
        return None

    # ✅ 【修复】A股账本是"只要还在追踪就每天新增一行"，一笔真实交易平仓前可能已经
    # 累积了好几天的行——平仓时这几行会被打上同一个终态Tag（见 scan.py 阶段0/0b），
    # 结果这里按行遍历会把同一笔交易算成好几笔，胜率的样本数被人为放大，平均盈亏也会
    # 被"同一笔交易在不同日子的收盘价"稀释。按(Ticker, Tag)分组，每组只保留日期最新的
    # 一行——同一支票如果先后有两段完全不同的持仓（比如3月止损过一次、8月又重新入选
    # 再平仓），Tag分组不会把它们混在一起，两段还是分开算两笔。
    if "Date" in closed.columns:
        closed["_dt"] = pd.to_datetime(closed["Date"], errors="coerce")
        before_dedup = len(closed)
        closed = closed.sort_values("_dt").drop_duplicates(subset=["Ticker", "Tag"], keep="last")
        deduped_count = before_dedup - len(closed)
        if deduped_count > 0:
            print(f"🗂️ 按(Ticker,Tag)去重：{before_dedup} 行平仓记录合并为 {len(closed)} 笔独立交易（同一笔交易在持仓期间产生的多行不再重复计入胜率）")

    # ── 从 review_history.csv 补充卖出价 ──
    # review_history.csv 字段：Ticker, Rec_Price(买入), Cur_Price(卖出), PnL_Pct 等
    review_exit_map = {}   # Ticker → (sell_price, pnl_pct)
    if os.path.exists(REVIEW_FILE):
        try:
            df_rv = pd.read_csv(REVIEW_FILE, keep_default_na=False)
            # 每只 ticker 取最新一条（按 Review_Date 排序）
            if "Review_Date" in df_rv.columns:
                df_rv["Review_Date"] = pd.to_datetime(df_rv["Review_Date"], errors="coerce")
                df_rv = df_rv.sort_values("Review_Date", ascending=False)
            for _, r in df_rv.drop_duplicates(subset="Ticker", keep="first").iterrows():
                ticker = str(r.get("Ticker", "")).strip()
                cur    = safe_float(r.get("Cur_Price"))
                pnl    = safe_float(r.get("PnL_Pct"), default=None)
                if ticker and cur is not None:
                    review_exit_map[ticker] = (cur, pnl)
            print(f"📋 从 review_history.csv 补充了 {len(review_exit_map)} 只标的的卖出价")
        except Exception as e:
            print(f"⚠️ 读取 review_history.csv 失败: {e}")

    rows = []
    skipped_no_sell = []
    for _, row in closed.iterrows():
        buy = safe_float(row.get(PRICE_COL))
        if buy is None:
            continue

        ticker = str(row.get("Ticker", "")).strip()

        # 优先用 review_history.csv 的价格，次用 trade_history.csv 的 Exit_Price
        sell = None
        pnl_pct_direct = None
        if ticker in review_exit_map:
            sell, pnl_pct_direct = review_exit_map[ticker]
        if sell is None:
            sell = safe_float(row.get(EXIT_COL))
        if sell is None:
            # 如果有 PnL_Pct 字段可以反推卖出价
            pnl_str = safe_float(row.get("PnL_Pct", row.get("Maturity_PnL")))
            if pnl_str is not None and buy is not None:
                sell = round(buy * (1 + pnl_str / 100), 2)
        if sell is None:
            # 三个来源都没拿到卖出价——大概率是 review_history.csv 里从没留下过这只票
            # 的记录（比如刚入账就同一天触发止损，还没被review.py正常巡检到一次就已经
            # 被跳过）。这类记录目前会被排除在胜率统计外，打印出来方便你自己核对是否
            # 真的该算作"没有止损统计进去"。
            skipped_no_sell.append(f"{row.get('Name','')}({ticker})[{row.get('Tag','')}]")
            continue

        pnl_pct = pnl_pct_direct if pnl_pct_direct is not None else round((sell - buy) / buy * 100, 2)

        rows.append({
            "ticker":   ticker,
            "name":     str(row.get("Name", "")),
            "industry": str(row.get(INDUSTRY_COL, "未知")),
            "tag":      str(row.get("Tag", "")),
            "score":    safe_float(row.get(SCORE_COL), default=50),
            "pnl_pct":  float(pnl_pct),
            "buy":      float(buy),
            "sell":     float(sell),
            "hold_period": str(row.get("Hold_Period", "")),
            "date":     str(row.get("Date", "")),
        })

    if skipped_no_sell:
        print(f"⚠️ {len(skipped_no_sell)} 条已平仓记录因三个来源都拿不到卖出价，被排除在胜率统计外: {skipped_no_sell[:15]}")

    if not rows:
        print("⚠️ 平仓记录均无有效买入/卖出价（trade_history.csv 和 review_history.csv 都没有）。")
        print("   提示：请确认 review_history.csv 中有 Ticker / Cur_Price 列。")
        return None

    df_c = pd.DataFrame(rows)
    wins    = (df_c["pnl_pct"] > 0).sum()
    total   = len(df_c)
    wr      = round(float(wins / total * 100), 1)
    avg_pnl = round(float(df_c["pnl_pct"].mean()), 2)
    best    = df_c.loc[df_c["pnl_pct"].idxmax()]
    worst   = df_c.loc[df_c["pnl_pct"].idxmin()]

    def _stats(grp):
        return {
            "样本数":    int(len(grp)),
            "胜率":      round(float((grp["pnl_pct"] > 0).sum() / len(grp) * 100), 1),
            "平均盈亏%": round(float(grp["pnl_pct"].mean()), 2),
        }

    # 按板块
    sector_stats = {
        sec: _stats(g)
        for sec, g in df_c.groupby("industry")
        if len(g) >= 2
    }
    sector_stats = dict(sorted(sector_stats.items(), key=lambda x: x[1]["胜率"], reverse=True))

    # 按评分区间
    def score_bucket(s):
        if s is None:  return "未知"
        if s >= 80:    return "80-100(高信心)"
        elif s >= 65:  return "65-79(中信心)"
        elif s >= 50:  return "50-64(低信心)"
        else:          return "<50(勉强入选)"

    df_c["score_bucket"] = df_c["score"].apply(score_bucket)
    score_stats = {bk: _stats(g) for bk, g in df_c.groupby("score_bucket") if len(g) >= 2}

    # 按退出方式
    tag_map = {"Stop_Loss_Hit": "止损触发", "Period_Matured": "持有到期",
               "Forced_Exit": "突发强清", "Dropped": "主动斩仓"}
    exit_stats = {
        tag_map.get(tag, tag): _stats(g)
        for tag, g in df_c.groupby("tag")
        if len(g) >= 1
    }

    # 上一轮规则
    prev_rules = []
    if os.path.exists(EVOLVE_LOG):
        try:
            with open(EVOLVE_LOG, "r", encoding="utf-8") as f:
                history = json.load(f)
                if history:
                    prev_rules = history[-1].get("applied_rules", [])
        except Exception:
            pass

    active_summary = [
        f"{r.get('Name','')}({r.get('Ticker','')}) 评分{r.get(SCORE_COL,'-')}"
        for _, r in active.iterrows()
    ][:10]

    generation_boundaries = _load_evolution_boundaries()
    generation_stats, since_last_evolution = _segment_by_generation(df_c, generation_boundaries)

    return {
        "total_closed":    total,
        "overall_win_rate": wr,
        "avg_pnl_pct":     avg_pnl,
        "best_trade":      f"{best['name']}({best['ticker']}) +{best['pnl_pct']}%",
        "worst_trade":     f"{worst['name']}({worst['ticker']}) {worst['pnl_pct']}%",
        "sector_stats":    sector_stats,
        "score_stats":     score_stats,
        "exit_stats":      exit_stats,
        "generation_stats": generation_stats,
        "since_last_evolution": since_last_evolution,
        "active_count":    len(active),
        "active_summary":  active_summary,
        "prev_rules":      prev_rules,
    }


# ============================================================
# 2. AI 分析 + 生成规则补丁
# ============================================================
def evolve_strategy(metrics: dict):
    print(f"🧬 启动策略进化引擎（{EVOLVE_MODEL}）...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL"),
    )

    prompt = f"""
你是一个量化策略进化系统。你的任务是：
1. 分析以下交易绩效数据，找出策略的真实优势和缺陷
2. 生成具体可执行的规则补丁，这些规则会被直接注入 scan.py 的 AI 选股 prompt

【当前绩效报告】：
- 已平仓交易总数：{metrics['total_closed']}
- 总体胜率（全部历史混合，仅供参考）：{metrics['overall_win_rate']}%（及格线60%）
- 平均盈亏：{metrics['avg_pnl_pct']}%
- 最佳交易：{metrics['best_trade']}
- 最差交易：{metrics['worst_trade']}

【按进化世代拆分胜率】（重点看这个，而不是上面那个混合了所有历史的总胜率——
这里能看出每一轮规则调整之后，胜率到底是变好了还是变差了）：
{json.dumps(metrics['generation_stats'], ensure_ascii=False, indent=2) if metrics['generation_stats'] else "尚无进化历史，这是第一轮"}

【最近一次进化之后的战绩】（当前生效规则的真实表现，样本量可能还小，仅供参考）：
{json.dumps(metrics['since_last_evolution'], ensure_ascii=False, indent=2) if metrics['since_last_evolution'] else "尚无数据或样本不足"}

【按行业板块拆分胜率】（识别哪些板块该超配/回避）：
{json.dumps(metrics['sector_stats'], ensure_ascii=False, indent=2)}

【按推荐评分区间拆分胜率】（验证评分体系是否有真正的预测力）：
{json.dumps(metrics['score_stats'], ensure_ascii=False, indent=2)}

【按退出方式拆分】（判断止损位/持股周期是否合理）：
{json.dumps(metrics['exit_stats'], ensure_ascii=False, indent=2)}

【上一轮已应用的进化规则】（请在此基础上演进，不要重复）：
{json.dumps(metrics['prev_rules'], ensure_ascii=False, indent=2) if metrics['prev_rules'] else "无（首次进化）"}

【分析要求】：
根据以上数据，用归纳法找出规律：
- 优先看"按进化世代拆分胜率"：如果最近一代相比上一代胜率下降了，说明上一轮的规则
  可能是错的或者用力过猛，这一轮应该考虑撤销或调整方向，而不是继续在错的方向加码。
- 哪些板块持续亏损应该明确回避？哪些持续盈利应该加权？
- 评分区间和实际收益是否正相关？如果低分区间胜率反而高，说明评分体系有问题，请指出。
- 止损触发次数多 = 止损位太紧；到期清仓亏损多 = 持股周期太长或趋势判断有误。
- 请推断每个发现背后的原因，而不只是描述现象。

必须只输出以下 JSON 格式，不要输出任何其他文字：
{{
    "assessment": "总体策略表现评估（3句话以内）",
    "key_findings": [
        "核心发现1（要有数据支撑，如：半导体板块胜率仅38%，远低于均值，建议降权）",
        "核心发现2",
        "核心发现3（最多3条）"
    ],
    "identified_flaws": "最关键的逻辑缺陷（一句话，要指出根因而非现象）",
    "applied_rules": [
        {{
            "rule_id": "rule_{datetime.date.today().strftime('%Y%m%d')}_001",
            "type": "SECTOR_AVOID 或 SECTOR_BOOST 或 SCORE_ADJUST 或 HOLD_PERIOD_ADJUST 或 STOPLOSS_ADJUST 或 CONDITION_ADD",
            "description": "规则的中文说明",
            "prompt_patch": "直接注入 scan.py AI prompt 的文字（具体、可执行，如：'由于历史数据显示半导体板块胜率长期低于40%，今日推荐时半导体板块标的评分上限降至70分，除非出现极强的宏观/政策催化'）",
            "evidence": "支撑这条规则的数据证据（如：半导体板块胜率38%，样本数12，平均亏损-4.2%）",
            "expires_after_trades": 20
        }},
        {{
            "rule_id": "rule_{datetime.date.today().strftime('%Y%m%d')}_002",
            "type": "...",
            "description": "...",
            "prompt_patch": "...",
            "evidence": "...",
            "expires_after_trades": 20
        }}
    ],
    "next_focus": "下一轮进化应该重点观察什么（如：观察加权后医药板块的胜率变化）"
}}
"""

    try:
        response = client.messages.create(
            model=EVOLVE_MODEL,
            max_tokens=2000,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        # claude-opus-4-8 可能返回 ThinkingBlock（内部推理），需要过滤出 TextBlock
        text_block = next((b for b in response.content if hasattr(b, "text")), None)
        if text_block is None:
            print("❌ AI 未返回文本内容（只有 ThinkingBlock）")
            return
        text = text_block.text.strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start == -1 or end == 0:
            print("❌ AI 未返回有效 JSON")
            return

        result = json.loads(text[start:end])
        result["date"]    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        result["metrics"] = {k: v for k, v in metrics.items()
                             if k not in ("prev_rules", "active_summary")}

        # ── 追加进化日志 ──
        log_data = []
        if os.path.exists(EVOLVE_LOG):
            try:
                with open(EVOLVE_LOG, "r", encoding="utf-8") as f:
                    log_data = json.load(f)
            except Exception:
                pass
        log_data.append(result)
        with open(EVOLVE_LOG, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        print(f"📚 进化日志已追加至 {EVOLVE_LOG}（历史共 {len(log_data)} 轮）")

        # ── 写入 evolved_rules.json（scan.py 启动时读取）──
        # 合并所有历史仍有效的规则（未超过 expires_after_trades 次数）
        all_rules = []
        total_closed_now = metrics["total_closed"]
        for entry in log_data:
            for rule in entry.get("applied_rules", []):
                trade_count_at_creation = entry.get("metrics", {}).get("total_closed", 0)
                expires = rule.get("expires_after_trades", 20)
                if total_closed_now - trade_count_at_creation < expires:
                    all_rules.append(rule)

        # 去重（同 rule_id 只保留最新）
        seen = {}
        for r in reversed(all_rules):
            seen.setdefault(r["rule_id"], r)
        deduped_rules = list(reversed(seen.values()))

        evolved_output = {
            "last_updated": result["date"],
            "total_closed_at_update": total_closed_now,
            "overall_win_rate": metrics["overall_win_rate"],
            "recent_win_rate": metrics.get("since_last_evolution"),
            "active_rules": deduped_rules,
            "prompt_patches": [r["prompt_patch"] for r in deduped_rules],
        }
        with open(EVOLVED_RULES, "w", encoding="utf-8") as f:
            json.dump(evolved_output, f, ensure_ascii=False, indent=2)

        print(f"\n✅ 策略进化完成！共生成/更新 {len(deduped_rules)} 条有效规则")
        print(f"📂 规则已写入 {EVOLVED_RULES}，scan.py 下次运行时将自动读取\n")
        for i, rule in enumerate(deduped_rules, 1):
            print(f"  规则{i} [{rule['type']}] {rule['description']}")
            print(f"    证据: {rule['evidence']}")
            print(f"    注入文本: {rule['prompt_patch'][:80]}...")

        print(f"\n📋 总体评估: {result.get('assessment', '')}")
        print(f"🎯 下轮重点: {result.get('next_focus', '')}")

    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失败: {e}\n原始返回:\n{text[:500]}")
    except Exception as e:
        print(f"❌ 进化引擎运行出错: {e}")


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    if not os.path.exists(HISTORY_FILE):
        print(f"未检测到 {HISTORY_FILE}，策略进化中止。")
        exit()

    df_raw = pd.read_csv(HISTORY_FILE, keep_default_na=False)

    # 过滤：Hold_Period / Stop_Loss / Score 三字段必须有效
    # 修复：原来要求 Hold_Period/Stop_Loss/Score 三个字段都有效才纳入统计，但历史上
    # 评分正则曾有bug导致大量记录 Score=N/A（Hold_Period/Stop_Loss 没受影响）。这批记录
    # 里包含了不少已经止损/到期的真实交易，用旧过滤条件会被整批剔除出胜率统计，导致
    # 统计出来的胜率虚高。改成只要求 Hold_Period/Stop_Loss 有效，Score 缺失不再剔除。
    _INVALID = {"", "n/a", "nan", "none", "坚决空仓", "观望", "绝对规避"}
    for col in ["Hold_Period", "Stop_Loss", SCORE_COL]:
        if col not in df_raw.columns:
            df_raw[col] = ""
    valid_mask = (
        df_raw["Hold_Period"].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID) &
        df_raw["Stop_Loss"].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID)
    )
    no_score_count = df_raw[SCORE_COL].astype(str).str.strip().str.lower().isin(_INVALID).sum()
    if no_score_count > 0:
        print(f"⚠️ {no_score_count} 条记录 Score=N/A（可能是历史评分bug所致），仍纳入胜率统计。")
    dropped_count = (~valid_mask).sum()
    if dropped_count > 0:
        print(f"🗂️ 过滤掉 {dropped_count} 条旧版本/不完整记录，不纳入绩效统计。")
    df = df_raw[valid_mask].copy()

    if df.empty:
        print("⚠️ 过滤后无有效记录，进化中止。")
        exit()

    metrics = calculate_metrics(df)
    if metrics is None:
        exit()

    print(f"\n📊 绩效概览：")
    print(f"  已平仓 {metrics['total_closed']} 笔 | 总体胜率(全部历史) {metrics['overall_win_rate']}% | 平均盈亏 {metrics['avg_pnl_pct']}%")
    print(f"  当前持仓 {metrics['active_count']} 只")
    if metrics["generation_stats"]:
        print(f"  按进化世代拆分：{metrics['generation_stats']}")
    if metrics["since_last_evolution"]:
        print(f"  最近一次进化之后：{metrics['since_last_evolution']}")
    if metrics["sector_stats"]:
        print(f"  板块胜率 Top3：{list(metrics['sector_stats'].items())[:3]}")
    if metrics["score_stats"]:
        print(f"  评分区间胜率：{metrics['score_stats']}")

    evolve_strategy(metrics)
