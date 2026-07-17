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
EVOLVE_LOG     = "strategy_evolution.json"
EVOLVED_RULES  = "evolved_rules.json"   # scan.py 会读取这个文件

# 触发进化的最小已平仓样本数（太少则统计无意义）
MIN_CLOSED = 8

# A股账本字段
CLOSED_TAGS  = {"Stop_Loss_Hit", "Period_Matured", "Forced_Exit", "Dropped"}
ACTIVE_TAGS  = {"Core_Double_Dragon", "Core_Dragon", "Sub_Pioneer"}
PRICE_COL    = "Close_Price"   # 买入价列名
EXIT_COL     = "Exit_Price"    # 卖出价列名（可能为"N/A"）
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


def calculate_metrics(df: pd.DataFrame) -> dict | None:
    """
    从账本计算多维度绩效指标，涵盖：
    - 总体胜率 / 平均盈亏
    - 按行业板块拆分
    - 按 AI 推荐评分区间拆分（评分是否真的能预测收益）
    - 已到期 vs 止损 vs 强清 的结果对比
    - 还在持仓中的浮动盈亏（参考用，不计入胜率）
    """
    if df.empty:
        return None

    closed = df[df["Tag"].isin(CLOSED_TAGS)].copy()
    active = df[df["Tag"].isin(ACTIVE_TAGS)].copy()

    if len(closed) < MIN_CLOSED:
        print(f"⚠️ 已平仓记录仅 {len(closed)} 条，不足 {MIN_CLOSED} 条，暂缓进化以避免过拟合。")
        return None

    # 安全计算每笔 P&L%
    rows = []
    for _, row in closed.iterrows():
        buy = safe_float(row.get(PRICE_COL))
        sell = safe_float(row.get(EXIT_COL))
        if buy is None or sell is None:
            continue
        pnl_pct = round((sell - buy) / buy * 100, 2)
        rows.append({
            "ticker":   str(row.get("Ticker", "")),
            "name":     str(row.get("Name", "")),
            "industry": str(row.get(INDUSTRY_COL, "未知")),
            "tag":      str(row.get("Tag", "")),
            "score":    safe_float(row.get(SCORE_COL), default=50),
            "pnl_pct":  pnl_pct,
            "buy":      buy,
            "sell":     sell,
            "hold_period": str(row.get("Hold_Period", "")),
        })

    if not rows:
        print("⚠️ 平仓记录均无有效买入/卖出价，无法计算绩效。")
        return None

    df_c = pd.DataFrame(rows)
    wins = (df_c["pnl_pct"] > 0).sum()
    total = len(df_c)
    overall_wr = round(wins / total * 100, 1)
    avg_pnl    = round(df_c["pnl_pct"].mean(), 2)
    best_trade = df_c.loc[df_c["pnl_pct"].idxmax()]
    worst_trade= df_c.loc[df_c["pnl_pct"].idxmin()]

    # ── 按板块拆分 ──
    sector_stats = {}
    for sector, grp in df_c.groupby("industry"):
        if len(grp) < 2:
            continue
        wr = round((grp["pnl_pct"] > 0).sum() / len(grp) * 100, 1)
        avg = round(grp["pnl_pct"].mean(), 2)
        sector_stats[sector] = {"样本数": len(grp), "胜率": wr, "平均盈亏%": avg}
    sector_stats = dict(sorted(sector_stats.items(), key=lambda x: x[1]["胜率"], reverse=True))

    # ── 按评分区间拆分（验证评分体系是否有预测力）──
    def score_bucket(s):
        if s is None:    return "未知"
        if s >= 80:      return "80-100(高信心)"
        elif s >= 65:    return "65-79(中信心)"
        elif s >= 50:    return "50-64(低信心)"
        else:            return "<50(勉强入选)"

    df_c["score_bucket"] = df_c["score"].apply(score_bucket)
    score_stats = {}
    for bk, grp in df_c.groupby("score_bucket"):
        if len(grp) < 2:
            continue
        wr = round((grp["pnl_pct"] > 0).sum() / len(grp) * 100, 1)
        avg = round(grp["pnl_pct"].mean(), 2)
        score_stats[bk] = {"样本数": len(grp), "胜率": wr, "平均盈亏%": avg}

    # ── 按退出方式拆分（止损多 = 止损位设太紧？到期多 = 周期设太短？）──
    exit_stats = {}
    tag_map = {"Stop_Loss_Hit": "止损触发", "Period_Matured": "持有到期",
               "Forced_Exit": "突发强清", "Dropped": "主动斩仓"}
    for tag, grp in df_c.groupby("tag"):
        if len(grp) < 1:
            continue
        label = tag_map.get(tag, tag)
        wr = round((grp["pnl_pct"] > 0).sum() / len(grp) * 100, 1)
        avg = round(grp["pnl_pct"].mean(), 2)
        exit_stats[label] = {"次数": len(grp), "胜率": wr, "平均盈亏%": avg}

    # ── 当前持仓浮动（参考用）──
    active_summary = []
    for _, row in active.iterrows():
        active_summary.append(f"{row.get('Name','')}({row.get('Ticker','')}) 评分{row.get(SCORE_COL,'-')}")

    # ── 读取上一轮进化结果（若有）──
    prev_rules = []
    if os.path.exists(EVOLVE_LOG):
        try:
            with open(EVOLVE_LOG, "r", encoding="utf-8") as f:
                history = json.load(f)
                if history:
                    last = history[-1]
                    prev_rules = last.get("applied_rules", [])
        except Exception:
            pass

    return {
        "total_closed":   total,
        "overall_win_rate": overall_wr,
        "avg_pnl_pct":    avg_pnl,
        "best_trade":     f"{best_trade['name']}({best_trade['ticker']}) +{best_trade['pnl_pct']}%",
        "worst_trade":    f"{worst_trade['name']}({worst_trade['ticker']}) {worst_trade['pnl_pct']}%",
        "sector_stats":   sector_stats,
        "score_stats":    score_stats,
        "exit_stats":     exit_stats,
        "active_count":   len(active),
        "active_summary": active_summary[:10],
        "prev_rules":     prev_rules,
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
- 总体胜率：{metrics['overall_win_rate']}%（及格线60%）
- 平均盈亏：{metrics['avg_pnl_pct']}%
- 最佳交易：{metrics['best_trade']}
- 最差交易：{metrics['worst_trade']}

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
        text = response.content[0].text.strip()
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
    _INVALID = {"", "n/a", "nan", "none", "坚决空仓", "观望", "绝对规避"}
    for col in ["Hold_Period", "Stop_Loss", SCORE_COL]:
        if col not in df_raw.columns:
            df_raw[col] = ""
    valid_mask = (
        df_raw["Hold_Period"].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID) &
        df_raw["Stop_Loss"].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID) &
        df_raw[SCORE_COL].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID)
    )
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
    print(f"  已平仓 {metrics['total_closed']} 笔 | 总体胜率 {metrics['overall_win_rate']}% | 平均盈亏 {metrics['avg_pnl_pct']}%")
    print(f"  当前持仓 {metrics['active_count']} 只")
    if metrics["sector_stats"]:
        print(f"  板块胜率 Top3：{list(metrics['sector_stats'].items())[:3]}")
    if metrics["score_stats"]:
        print(f"  评分区间胜率：{metrics['score_stats']}")

    evolve_strategy(metrics)
