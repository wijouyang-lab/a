# 消息+逻辑推演驱动版 | 事件→产业链→受益标的 | 个股新闻深度版 | Top5详细分析+评分版
# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import yfinance as yf
import datetime
import os
import json
import re
import smtplib
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import tushare as ts
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic
import time
import random
import email.utils  # 【新增】解析RSS日期，过滤过期新闻

# ==========================================
# 启动前置校验：AI 凭证
# ==========================================
_missing_env = [k for k in ("CLAWSOCKET_API_KEY", "CLAWSOCKET_BASE_URL") if not os.environ.get(k)]
if _missing_env:
    print(f"致命错误：未检测到环境变量 {', '.join(_missing_env)}！请检查 GitHub Actions 仓库的 Secrets 配置（Settings → Secrets and variables → Actions），并确认 workflow yml 中已通过 env: 正确传递。")
    import sys; sys.exit(1)

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

# ==========================================
# 版本标记
# ==========================================
def update_version_marker():
    version_file = "scan_version.txt"
    try:
        with open("scan.py", "rb") as f:
            current_hash = hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        print(f"⚠️ 版本标记读取自身失败，跳过: {e}")
        return

    old_hash = None
    if os.path.exists(version_file):
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    old_hash = content.split(",")[0]
        except Exception:
            pass

    if old_hash != current_hash:
        today_str = get_bj_time().strftime('%Y-%m-%d')
        with open(version_file, "w", encoding="utf-8") as f:
            f.write(f"{current_hash},{today_str}")
        print(f"📌 检测到 scan.py 内容已变化，记录新版本起始日期: {today_str}")
    else:
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            version_date = existing.split(",")[1] if "," in existing else "未知"
            print(f"📌 scan.py 版本未变化，当前版本起始日期: {version_date}")
        except Exception:
            pass

update_version_marker()

print(f"当前北京时间: {get_bj_time()}")
print(f"星期: {get_bj_time().weekday()} (0=周一 6=周日)")

today = get_bj_time().weekday()
if today >= 5:
    print("周末不开盘，退出早盘扫描。")
    import sys; sys.exit(0)

bj_hour = get_bj_time().hour
if bj_hour < 6 or bj_hour >= 15:
    print(f"现在是北京时间 {bj_hour} 点，不在交易时段（6-15点），跳过扫描。")
    import sys; sys.exit(0)

print("时间检查通过，开始扫描...")

# 恢复双引擎架构：报告核心推演使用 Pro 模型，排雷审查使用 Flash 模型
TARGET_MODEL = 'claude-opus-4-8'
DEFAULT_STOP_LOSS_PCT = -5.0
# ✅ 【新增】ATR止损倍数：止损距离 = ATR_Pct(该股自己的14日真实波幅占价格的百分比) × 这个倍数。
ATR_STOP_MULTIPLIER = 2.0
ATR_STOP_FLOOR_PCT = 3.0
ATR_STOP_CEIL_PCT = 12.0

ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()


# ==========================================
# 0. 扫描前：统一获取最新可用收盘价表
# ==========================================
def get_latest_price_map():
    holding_tickers = []
    try:
        log_file = "trade_history.csv"
        if os.path.exists(log_file):
            df_h = pd.read_csv(log_file)
            active_tags = {'Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon'}
            active = df_h[df_h['Tag'].isin(active_tags)]
            holding_tickers = active['Ticker'].dropna().unique().tolist()
    except Exception:
        pass

    price_map = {}

    if holding_tickers:
        try:
            bare_codes = [t.split('.')[0] for t in holding_tickers]
            df_rt = ts.get_realtime_quotes(bare_codes)
            if df_rt is not None and not df_rt.empty and 'price' in df_rt.columns:
                exchange_map = {t.split('.')[0]: t for t in holding_tickers}
                for _, row in df_rt.iterrows():
                    code = str(row.get('code', ''))
                    ts_code = exchange_map.get(code)
                    try:
                        price = float(row['price'])
                        if ts_code and price > 0:
                            price_map[ts_code] = price
                    except (ValueError, TypeError):
                        pass
                if price_map:
                    print(f"✅ 实时行情拉取成功，覆盖 {len(price_map)} 只持仓现价（盘中实时口径）")
                    return price_map
        except Exception as e:
            print(f"⚠️ 实时行情接口失败，回退收盘价: {e}")

    try:
        trade_date_latest = get_bj_time().strftime('%Y%m%d')
        df_prices = pro.daily(trade_date=trade_date_latest)
        if df_prices is not None and not df_prices.empty:
            price_map = dict(zip(df_prices['ts_code'], df_prices['close']))
            print(f"✅ 今日收盘价拉取成功，共 {len(price_map)} 只（盘后口径）")
            return price_map
    except Exception as e:
        print(f"⚠️ 今日 daily 失败: {e}")

    try:
        yesterday_str = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
        df_prices = pro.daily(trade_date=yesterday_str)
        if df_prices is not None and not df_prices.empty:
            price_map = dict(zip(df_prices['ts_code'], df_prices['close']))
            print(f"⚠️ 使用昨日收盘价兜底，共 {len(price_map)} 只（止损判断可能轻微滞后一日）")
            return price_map
    except Exception as e:
        print(f"⚠️ 昨日 daily 也失败: {e}")

    print("🚨 价格拉取全部失败，price_map 为空，止损判断将使用买入价（盈亏=0），请检查 tushare token 与网络。")
    return {}


# ==========================================
# 0a. 扫描前：读取持仓 + 消息面与宏观大宗数据 → AI 判断哪些应该强清与暂停追踪
# ==========================================
def pre_scan_portfolio_review(macro_news_text, macro_data_text, price_map):
    log_file = "trade_history.csv"
    review_log = "review_history.csv"

    if not os.path.exists(log_file):
        print("📋 [阶段0] trade_history.csv 不存在，跳过持仓审查。")
        return []

    try:
        df = pd.read_csv(log_file)
        df['Date'] = pd.to_datetime(df['Date'])
        cutoff = get_bj_time() - datetime.timedelta(days=30)
        recent = df[df['Date'] >= cutoff.replace(tzinfo=None)].copy()

        active_tags = ['Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon']
        holdings = recent[recent['Tag'].isin(active_tags)].copy()

        if holdings.empty:
            print("📋 [阶段0] 当前无有效持仓，跳过持仓审查。")
            return []

        _INVALID_P0 = {'', 'n/a', 'nan', 'none'}
        for _col in ['Hold_Period', 'Stop_Loss', 'Score']:
            if _col not in holdings.columns:
                holdings[_col] = ''
        _valid_mask_p0 = (
            holdings['Hold_Period'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID_P0) &
            holdings['Stop_Loss'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID_P0) &
            holdings['Score'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID_P0)
        )
        _dropped_p0 = (~_valid_mask_p0).sum()
        if _dropped_p0 > 0:
            print(f"📋 [阶段0] 三字段过滤：剔除 {_dropped_p0} 条旧版本/不完整持仓记录，不纳入风控审查。")
        holdings = holdings[_valid_mask_p0].copy()

        if holdings.empty:
            print("📋 [阶段0] 过滤后无有效新版本持仓，跳过持仓审查。")
            return []

        holdings = holdings.sort_values('Date', ascending=False).drop_duplicates(subset='Ticker', keep='first')
        print(f"📋 [阶段0] 发现 {len(holdings)} 只持仓，正在结合宏观大宗指标与突发消息进行风险审查...")

    except Exception as e:
        print(f"⚠️ [阶段0] 持仓读取失败: {e}")
        return []

    holdings_info = []
    for _, row in holdings.iterrows():
        holdings_info.append({
            "代码": row['Ticker'],
            "名称": row.get('Name', row['Ticker']),
            "行业": row.get('Industry', '未知'),
            "买入价": row.get('Close_Price', 'N/A'),
            "持股周期": row.get('Hold_Period', 'N/A'),
            "止损价": row.get('Stop_Loss', 'N/A'),
            "推荐日期": str(row['Date'])[:10],
        })

    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )

    review_prompt = f"""
你是顶级A股风控总监，负责每日盘前的持仓突发风险与宏观环境审查。

【今日全球宏观与A股消息面】：
{macro_news_text[:1000]}

【今日国际宏观大宗指标】：
{macro_data_text}

【当前持仓列表】：
{json.dumps(holdings_info, ensure_ascii=False)}

【你的任务】：
审查每只持仓股票，判断今日消息面、全球宏观数据以及大宗商品价格异动，是否对该股票产生了严重的负面冲击，从而需要立即强制清仓。

判断标准（满足任意一条即建议清仓）：
1. 今日新闻中有该公司或其所在行业的直接突发重大负面消息
2. 宏观事件或大宗商品剧烈震荡导致该行业的产业链逻辑根本性反论
3. 美债收益率持续狂飙或重要宏观数据导致全球资金流向根本扭转，影响整体A股高位核心板块的估值底层逻辑

【输出格式】：
严格输出一个 JSON 数组，每个元素包含：
- ticker: 股票代码（如 000001.SZ）
- name: 股票名称
- action: "清仓" 或 "持有"
- reason: 一句话说明理由，需包含对宏观或微观异动的归因

只输出 JSON，不要任何其他文字，格式示例：
[
  {{"ticker": "000001.SZ", "name": "平安银行", "action": "持有", "reason": "流动性逻辑未变"}},
  {{"ticker": "600519.SH", "name": "贵州茅台", "action": "清仓", "reason": "PCE数据引发全球趋势逆转风险"}}
]
"""

    try:
        response = client.messages.create(
            model=TARGET_MODEL,
            max_tokens=80000, 
            temperature=0.1,
            messages=[{"role": "user", "content": review_prompt}]
        )
        raw = response.content[0].text.strip()
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            print("⚠️ [阶段0] AI 返回格式异常，跳过持仓审查。")
            return []
        results = json.loads(json_match.group())
    except Exception as e:
        print(f"⚠️ [阶段0] 持仓审查 AI 调用失败: {e}")
        return []

    to_remove = []
    for item in results:
        if item.get('action') == '清仓':
            to_remove.append(item['ticker'])
            print(f"🚨 [阶段0] 突发清仓预警: {item['name']} ({item['ticker']}) — {item['reason']}")

    if not to_remove:
        print("✅ [阶段0] 所有持仓经消息面与宏观指标审查均无需清仓，继续正常扫描。")
        return []

    try:
        df_orig = pd.read_csv(log_file)
        _terminal_tags = {'Stop_Loss_Hit', 'Period_Matured', 'Forced_Exit', 'Dropped', 'Trap_Warning'}
        for ticker in to_remove:
            _mask = (df_orig['Ticker'] == ticker) & (~df_orig['Tag'].isin(_terminal_tags))
            df_orig.loc[_mask, 'Tag'] = 'Forced_Exit'
        df_orig.to_csv(log_file, index=False)
        print(f"🔒 [阶段0] 已在 trade_history.csv 中将 {to_remove} 的标签锁定为 'Forced_Exit'（暂停后续追踪）")
    except Exception as e:
        print(f"⚠️ [阶段0] trade_history.csv 标签状态更新失败: {e}")

    try:
        if os.path.exists(review_log):
            df_review = pd.read_csv(review_log, on_bad_lines='skip')
        else:
            df_review = pd.DataFrame(columns=["Review_Date","Ticker","Name","Tag","Rec_Date","Rec_Price","Cur_Price","Days_Held","PnL_Pct","Maturity_PnL","Hold_Period","Stop_Loss","Rec_Count","Status","Score"])
        
        review_date_str = get_bj_time().strftime('%Y-%m-%d')
        for ticker in to_remove:
            ticker_rows = holdings[holdings['Ticker'] == ticker]
            if not ticker_rows.empty:
                last_row = ticker_rows.iloc[0]
                buy_price = float(last_row['Close_Price'])
                sell_price = price_map.get(ticker, buy_price)
                pnl = round(((sell_price - buy_price) / buy_price) * 100, 2)
                days_held = (get_bj_time().replace(tzinfo=None) - pd.to_datetime(last_row['Date'])).days
                
                new_rec = {
                    "Review_Date": review_date_str,
                    "Ticker": ticker,
                    "Name": last_row.get('Name', ticker),
                    "Tag": "Forced_Exit",
                    "Rec_Date": str(last_row['Date'])[:10],
                    "Rec_Price": buy_price,
                    "Cur_Price": sell_price,
                    "Days_Held": days_held,
                    "PnL_Pct": pnl,
                    "Maturity_PnL": pnl,
                    "Hold_Period": last_row.get('Hold_Period', 'N/A'),
                    "Stop_Loss": last_row.get('Stop_Loss', 'N/A'),
                    "Rec_Count": last_row.get('Score', 'N/A'),
                    "Status": "突发清仓暂停",
                    "Score": last_row.get('Score', 'N/A')
                }
                df_review = pd.concat([df_review, pd.DataFrame([new_rec])], ignore_index=True)
        df_review.to_csv(review_log, index=False)
        print(f"🔒 [阶段0] 已将清仓标的之买入价与卖出价归档至 review_history.csv 且状态设为 '突发清仓暂停'")
    except Exception as e:
        print(f"⚠️ [阶段0] review_history.csv 风险归档失败: {e}")

    try:
        forced_exit_log = "forced_exit_log.csv"
        log_exists = os.path.exists(forced_exit_log)
        with open(forced_exit_log, "a", encoding="utf-8") as f:
            if not log_exists:
                f.write("Date,Ticker,Name,Reason\n")
            for item in results:
                if item.get('action') == '清仓':
                    f.write(f"{get_bj_time().strftime('%Y-%m-%d')},{item['ticker']},{item['name']},{item['reason']}\n")
    except Exception as e:
        print(f"⚠️ 清仓独立记录保存失败: {e}")

    return to_remove


# ==========================================
# 0b. 规则驱动卖出信号检测（止损触发 / 持有到期）—— 不依赖AI，纯数值判断
# ==========================================
def check_rule_based_sell_signals(price_map, exclude_tickers=None):
    log_file = "trade_history.csv"
    review_log = "review_history.csv"
    signal_log = "sell_signal_log.csv"
    exclude_tickers = set(exclude_tickers or [])

    if not os.path.exists(log_file):
        print("📋 [阶段0b] trade_history.csv 不存在，跳过规则卖出信号检测。")
        return [], []

    try:
        df = pd.read_csv(log_file)
        df['Date'] = pd.to_datetime(df['Date'])
        cutoff = get_bj_time() - datetime.timedelta(days=30)
        recent = df[df['Date'] >= cutoff.replace(tzinfo=None)].copy()

        active_tags = ['Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon']
        holdings_latest = recent[recent['Tag'].isin(active_tags)].copy()
        if holdings_latest.empty:
            print("📋 [阶段0b] 当前无有效持仓，跳过规则卖出信号检测。")
            return [], []

        _INVALID = {'', 'n/a', 'nan', 'none'}
        for _col in ['Hold_Period', 'Stop_Loss', 'Score']:
            if _col not in holdings_latest.columns:
                holdings_latest[_col] = ''
        _valid_mask = (
            holdings_latest['Hold_Period'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID) &
            holdings_latest['Stop_Loss'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID) &
            holdings_latest['Score'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID)
        )
        holdings_latest = holdings_latest[_valid_mask].copy()
        if holdings_latest.empty:
            print("📋 [阶段0b] 过滤后无有效新版本持仓，跳过规则卖出信号检测。")
            return [], []

        holdings_latest = holdings_latest.sort_values('Date', ascending=False).drop_duplicates(subset='Ticker', keep='first')
        holdings_latest = holdings_latest[~holdings_latest['Ticker'].astype(str).isin(exclude_tickers)]
        if holdings_latest.empty:
            print("📋 [阶段0b] 持仓已被阶段0a全部处理，跳过规则卖出信号检测。")
            return [], []

        # 【修复】取最近一行 active 记录作为买入参考，避免历史多行导致到期计算错误
        latest_active_by_ticker = (
            recent[recent['Ticker'].isin(holdings_latest['Ticker'])]
            .sort_values('Date', ascending=False)  # 【改】最近一行
            .drop_duplicates(subset='Ticker', keep='first')
            .set_index('Ticker')
        )
        holdings = holdings_latest
    except Exception as e:
        print(f"⚠️ [阶段0b] 持仓读取失败: {e}")
        return [], []

    def _parse_hold_days(hold_period_str):
        s = str(hold_period_str).strip()
        if not s or s.lower() in ['n/a', 'nan'] or s in ['坚决空仓', '观望']:
            return None
        nums = re.findall(r'\d+', s)
        return int(nums[-1]) if nums else None

    def _parse_stop_loss_price(stop_loss_str):
        s = str(stop_loss_str).strip()
        if not s or s.lower() in ['n/a', 'nan'] or s in ['坚决空仓', '绝对规避', '观望']:
            return None
        nums = re.findall(r'\d+\.?\d*', s)
        return float(nums[0]) if nums else None

    now = get_bj_time().replace(tzinfo=None)
    sell_signals = []
    removed_tickers = []

    for _, row in holdings.iterrows():
        try:
            ticker = str(row['Ticker'])
            earliest_row = latest_active_by_ticker.loc[ticker] if ticker in latest_active_by_ticker.index else row
            buy_price = float(earliest_row['Close_Price'])
            buy_date = earliest_row['Date']
            orig_tag = row['Tag']
            hold_days = _parse_hold_days(row.get('Hold_Period'))
            stop_loss_val = _parse_stop_loss_price(row.get('Stop_Loss'))
            cur_price = price_map.get(ticker, buy_price)

            signal_type = None
            reason = ""
            if stop_loss_val is not None and cur_price <= stop_loss_val:
                signal_type = "止损触发"
                reason = f"现价{cur_price}已跌破止损位{stop_loss_val}元，按风控纪律应立即止损离场"
            elif hold_days is not None:
                maturity_date = buy_date + datetime.timedelta(days=hold_days)
                if now >= maturity_date:
                    signal_type = "持有到期"
                    days_held_now = (now - buy_date).days
                    reason = f"已持有{days_held_now}天，达到/超过建议持股周期（{row.get('Hold_Period')}）上限，按纪律应清仓离场"

            if signal_type is None:
                continue

            pnl_pct = round(((cur_price - buy_price) / buy_price) * 100, 2)
            sell_signals.append({
                "ticker": ticker,
                "name": str(row.get('Name', ticker)),
                "industry": str(row.get('Industry', '未知')),
                "signal_type": signal_type,
                "orig_tag": orig_tag,
                "buy_price": buy_price,
                "buy_date": buy_date.strftime('%Y-%m-%d'),
                "current_price": cur_price,
                "pnl_pct": pnl_pct,
                "days_held": (now - buy_date).days,
                "hold_period": row.get('Hold_Period', 'N/A'),
                "stop_loss": row.get('Stop_Loss', 'N/A'),
                "score": row.get('Score', 'N/A'),
                "reason": reason.replace(",", "，"),
            })
            removed_tickers.append(ticker)
        except Exception as e:
            print(f"⚠️ [阶段0b] {row.get('Ticker','?')} 止损/到期检测出错，跳过该标的（不影响其余持仓的检测）: {e}")
            continue

    if not sell_signals:
        print("✅ [阶段0b] 规则审查：当前持仓无止损触发或持有到期信号。")
        return [], []

    try:
        df_orig = pd.read_csv(log_file)
        _terminal_tags = {'Stop_Loss_Hit', 'Period_Matured', 'Forced_Exit', 'Dropped', 'Trap_Warning'}
        for s in sell_signals:
            tag_to_set = 'Stop_Loss_Hit' if s['signal_type'] == '止损触发' else 'Period_Matured'
            _mask = (df_orig['Ticker'] == s['ticker']) & (~df_orig['Tag'].isin(_terminal_tags))
            df_orig.loc[_mask, 'Tag'] = tag_to_set
        df_orig.to_csv(log_file, index=False)
        print(f"🔒 [阶段0b] 已锁定 {len(sell_signals)} 只标的标签（止损触发/持有到期），停止后续追踪")
    except Exception as e:
        print(f"⚠️ [阶段0b] trade_history.csv 标签更新失败: {e}")

    try:
        if os.path.exists(review_log):
            df_review = pd.read_csv(review_log, on_bad_lines='skip')
        else:
            df_review = pd.DataFrame(columns=["Review_Date","Ticker","Name","Tag","Rec_Date","Rec_Price","Cur_Price","Days_Held","PnL_Pct","Maturity_PnL","Hold_Period","Stop_Loss","Rec_Count","Status","Score"])

        review_date_str = get_bj_time().strftime('%Y-%m-%d')
        for s in sell_signals:
            status_text = "止损触发清仓" if s['signal_type'] == '止损触发' else "周期到期清仓"
            new_rec = {
                "Review_Date": review_date_str,
                "Ticker": s['ticker'],
                "Name": s['name'],
                "Tag": s['orig_tag'],
                "Rec_Date": s['buy_date'],
                "Rec_Price": s['buy_price'],
                "Cur_Price": s['current_price'],
                "Days_Held": s['days_held'],
                "PnL_Pct": s['pnl_pct'],
                "Maturity_PnL": s['pnl_pct'],
                "Hold_Period": s['hold_period'],
                "Stop_Loss": s['stop_loss'],
                "Rec_Count": s['score'],
                "Status": status_text,
                "Score": s['score']
            }
            df_review = pd.concat([df_review, pd.DataFrame([new_rec])], ignore_index=True)
        df_review.to_csv(review_log, index=False)
        print(f"🔒 [阶段0b] 已将 {len(sell_signals)} 笔止损/到期卖出信号归档至 review_history.csv（保留原推荐标签供胜率统计）")
    except Exception as e:
        print(f"⚠️ [阶段0b] review_history.csv 归档失败: {e}")

    try:
        signal_log = "sell_signal_log.csv"
        log_exists = os.path.exists(signal_log)
        with open(signal_log, "a", encoding="utf-8") as f:
            if not log_exists:
                f.write("Date,Ticker,Name,Signal_Type,Buy_Price,Current_Price,PnL_Pct,Hold_Period,Stop_Loss,Reason\n")
            for s in sell_signals:
                f.write(f"{get_bj_time().strftime('%Y-%m-%d')},{s['ticker']},{s['name']},{s['signal_type']},{s['buy_price']},{s['current_price']},{s['pnl_pct']},{s['hold_period']},{s['stop_loss']},{s['reason']}\n")
    except Exception as e:
        print(f"⚠️ [阶段0b] sell_signal_log.csv 写入失败: {e}")

    for s in sell_signals:
        icon = "🛑" if s['signal_type'] == '止损触发' else "⏰"
        print(f"{icon} [阶段0b] 卖出信号: {s['name']}({s['ticker']}) — {s['signal_type']} | 现价{s['current_price']} 买入价{s['buy_price']} 盈亏{s['pnl_pct']:+.2f}%")

    return sell_signals, removed_tickers


# ==========================================
# 0c. 统一渲染"今日卖出信号"卡片
# ==========================================
def build_sell_signal_card(macro_removed_tickers, rule_sell_signals):
    if not macro_removed_tickers and not rule_sell_signals:
        return ""

    total = len(macro_removed_tickers) + len(rule_sell_signals)
    stop_count = sum(1 for s in rule_sell_signals if s.get('signal_type') == '止损触发')
    mature_count = sum(1 for s in rule_sell_signals if s.get('signal_type') == '持有到期')
    forced_count = len(macro_removed_tickers)

    detail_lines = []
    if forced_count > 0:
        detail_lines.append(f"突发强清 {forced_count} 只")
    if stop_count > 0:
        detail_lines.append(f"止损触发 {stop_count} 只")
    if mature_count > 0:
        detail_lines.append(f"持有到期 {mature_count} 只")

    detail_text = " / ".join(detail_lines) if detail_lines else ""

    return f"""
<div style="background:#fff3e0; border-left:6px solid #e65100; padding:12px 20px; margin-bottom:20px; border-radius:8px; font-size:14px;">
    <span style="color:#bf360c; font-weight:bold;">🔔 今日卖出信号：共{total}只（{detail_text}）</span>
    <span style="color:#8d6e63; margin-left:10px;">已自动清仓归档，详情见 review_history.csv</span>
</div>
"""



# ==========================================
# 0d. 渲染"当前持仓监控"卡片
# ==========================================
def build_current_holdings_card(price_map):
    log_file = "trade_history.csv"
    if not os.path.exists(log_file):
        return ""

    try:
        df = pd.read_csv(log_file)
        if df.empty or 'Tag' not in df.columns:
            return ""

        df['Date'] = pd.to_datetime(df['Date'])
        cutoff = get_bj_time() - datetime.timedelta(days=30)
        recent = df[df['Date'] >= cutoff.replace(tzinfo=None)].copy()

        active_tags = {'Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon'}
        holdings = recent[recent['Tag'].isin(active_tags)].copy()

        if holdings.empty:
            return ""

        _INVALID = {'', 'n/a', 'nan', 'none'}
        for _col in ['Hold_Period', 'Stop_Loss', 'Score']:
            if _col not in holdings.columns:
                holdings[_col] = ''
        _valid_mask = (
            holdings['Hold_Period'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID) &
            holdings['Stop_Loss'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID) &
            holdings['Score'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID)
        )
        holdings = holdings[_valid_mask].copy()

        if holdings.empty:
            return ""

        holdings = holdings.sort_values('Date', ascending=False).drop_duplicates(subset='Ticker', keep='first')

        now = get_bj_time().replace(tzinfo=None)
        rows_html = ""
        for _, row in holdings.iterrows():
            ticker = str(row['Ticker'])
            name = str(row.get('Name', ticker))
            buy_price = float(row['Close_Price'])
            buy_date = pd.to_datetime(row['Date'])
            days_held = (now - buy_date).days
            cur_price = price_map.get(ticker, buy_price)
            pnl_pct = round(((cur_price - buy_price) / buy_price) * 100, 2)
            pnl_color = "#d32f2f" if pnl_pct >= 0 else "#388e3c"

            rows_html += f"""
        <tr style="border-bottom:1px solid #c8e6c9;">
            <td style="padding:8px 4px;"><b>{name}({ticker})</b></td>
            <td style="padding:8px 4px;">{str(buy_date)[:10]}（已持股{days_held}天）</td>
            <td style="padding:8px 4px;">买入¥{buy_price:.2f} → 现价¥{cur_price:.2f}</td>
            <td style="padding:8px 4px; color:{pnl_color}; font-weight:bold;">{pnl_pct:+.2f}%</td>
            <td style="padding:8px 4px;">{row.get('Hold_Period', 'N/A')}</td>
            <td style="padding:8px 4px;">{row.get('Stop_Loss', 'N/A')}</td>
        </tr>"""

        total = len(holdings)
        return f"""
<div style="background:#e8f5e9; border-left:6px solid #2e7d32; padding:20px; margin-bottom:25px; border-radius:8px; max-height:320px; overflow-y:auto;">
    <h3 style="margin:0 0 12px 0; color:#1b5e20;">📦 当前持仓监控（共 {total} 只）</h3>
    <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <tr style="text-align:left; color:#2e7d32; border-bottom:1px solid #a5d6a7;">
            <th style="padding:6px 4px;">标的</th>
            <th style="padding:6px 4px;">买入时间</th>
            <th style="padding:6px 4px;">价格变动</th>
            <th style="padding:6px 4px;">浮动盈亏</th>
            <th style="padding:6px 4px;">建议周期</th>
            <th style="padding:6px 4px;">止损位</th>
        </tr>
        {rows_html}
    </table>
</div>
"""
    except Exception as e:
        print(f"⚠️ 渲染当前持仓卡片失败: {e}")
        return ""


# ==========================================
# 2.6  美股板块大跌 → A股联动封禁清单
# ==========================================
US_SECTOR_TO_ASHARE = {
    "SOXX": ["半导体", "芯片", "封测", "晶圆", "半导体材料", "半导体设备"],
    "XLK":  ["科技", "AI算力", "光模块", "CPO", "云计算", "数据中心"],
    "XLE":  ["石油", "煤炭", "天然气", "能源"],
    "XLF":  ["银行", "保险", "券商", "金融"],
    "XLV":  ["医药", "创新药", "医疗器械", "CXO"],
    "XLY":  ["消费", "汽车", "零售", "白酒"],
    "XLI":  ["军工", "航空", "制造", "机器人"],
    "XLB":  ["有色金属", "化工", "矿业"],
    "ARKK": ["AI", "基因", "新能源汽车", "自动驾驶"],
}
EMBARGO_THRESHOLD_PCT = -1.5

def parse_sector_embargo(us_sector_text):
    if not us_sector_text or "暂无" in us_sector_text:
        return [], ""

    embargo_sectors = []
    embargo_lines = []

    for line in us_sector_text.strip().split('\n'):
        line = line.strip()
        if not line or '📉' not in line:
            continue
        try:
            parts = line.replace('📉', '').strip().split(':')
            etf = parts[0].strip()
            pct_str = parts[1].strip().split('%')[0].strip()
            pct = float(pct_str)
        except Exception:
            continue

        if pct >= EMBARGO_THRESHOLD_PCT:
            continue

        a_share_labels = US_SECTOR_TO_ASHARE.get(etf, [])
        if not a_share_labels:
            continue

        embargo_sectors.extend(a_share_labels)

        strength = "⛔ 强封（跌幅≥3%，A股高度联动）" if pct <= -3.0 else "🚫 预警封禁（跌幅≥1.5%，情绪联动）"
        embargo_lines.append(
            f"  {strength} {etf} 昨日 {pct:+.2f}% → 今日禁止推荐A股相关板块：{'、'.join(a_share_labels)}"
        )

    if not embargo_lines:
        return [], ""

    embargo_sectors = list(dict.fromkeys(embargo_sectors))
    embargo_text = f"""
🚨【美股板块联动封禁名单 —— 硬性纪律，不可违反】：
根据昨夜美股板块涨跌数据，以下A股板块今日进入联动封禁区：

{chr(10).join(embargo_lines)}

【执行要求——无例外】：
1. 以上封禁板块内的任何个股，今日一律不得进入【核心精选】Top 1-5，即使该个股今日技术面信号强烈、个股新闻利好、宏观逻辑通顺，也绝对禁止推荐。
2. "A股有独立逻辑"不构成例外理由——联动封禁的核心不是基本面，是情绪传导：美股半导体大跌后，A股半导体在盘前开盘初期必然承压，追入是错误的。
3. 如确实需要提及这些板块，只能出现在【受损预警】区，明确注明"受美股联动压制，今日回避"。
4. 今日精选方向应聚焦于：美股昨日涨幅为正的板块对应的A股方向 + 与美股相关性低的本土催化标的（政策催化、事件驱动、低位反转）。
封禁板块关键词列表（只要股票所属行业/板块包含以下任一关键词，即触发封禁）：[{', '.join(embargo_sectors)}]
"""
    print(f"🚫 [阶段2.6] 美股联动封禁触发：{len(embargo_lines)} 个板块受限，封禁关键词: {embargo_sectors}")
    return embargo_sectors, embargo_text


# ==========================================
# 1. 获取交易额 Top 300
# ==========================================
def get_top_300_pool():
    print(f"🔍 [阶段1] 正在拉取最近交易日的A股全市场数据，圈定 Top 300 主力资金池...")
    df_daily = None
    trade_date = None

    for i in range(1, 8):
        try_date = (get_bj_time() - datetime.timedelta(days=i)).strftime('%Y%m%d')
        df_try = pro.daily(trade_date=try_date)
        if df_try is not None and not df_try.empty:
            df_daily = df_try
            trade_date = try_date
            print(f"   ✅ 找到最近交易日数据: {try_date}")
            break
        else:
            print(f"   {try_date} 无数据（非交易日），继续往前找...")

    if df_daily is None:
        print("🚨 连续7天都没有拉取到数据，返回空池。")
        return {}, [], None

    basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
    name_map = dict(zip(basic['ts_code'], basic['name']))
    industry_map = dict(zip(basic['ts_code'], basic.get('industry', ['未知'] * len(basic))))

    df_sorted = df_daily.sort_values(by='amount', ascending=False).head(300)
    codes = [row['ts_code'] for _, row in df_sorted.iterrows()]

    full_pool = {}
    for _, row in df_sorted.iterrows():
        ts_code = row['ts_code']
        full_pool[ts_code] = {
            "Ticker": ts_code,
            "Name": name_map.get(ts_code, ts_code),
            "Industry": industry_map.get(ts_code, "未知"),
            "Open": row.get('open', row['close']),
            "Close": row['close'],
            "Amount": row['amount'],
            "pct_chg": row.get('pct_chg', 0),
        }

    print(f"✅ 成功圈定 {len(full_pool)} 只核心活跃标的（数据日期: {trade_date}）。")
    return full_pool, codes, trade_date


# ==========================================
# 2. 宏观新闻采集
# ==========================================

# ==========================================
# 【新增】新闻日期过滤辅助函数
# ==========================================
def _parse_rss_date(date_str: str):
    """解析RSS日期字符串，返回datetime对象（BJ时区）"""
    if not date_str:
        return None
    try:
        # RFC 822 格式: Mon, 15 Jan 2024 08:30:00 GMT
        dt = email.utils.parsedate_to_datetime(date_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(BEIJING_TZ)
    except Exception:
        pass
    # 兜底：尝试常见格式
    for fmt in ("%a, %d %b %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(date_str.strip()[:25], fmt)
            return dt.replace(tzinfo=BEIJING_TZ)
        except Exception:
            pass
    return None

def _is_recent_news(date_str: str, max_days: int = 2):
    """判断新闻日期是否在允许范围内（默认2天）"""
    dt = _parse_rss_date(date_str)
    if dt is None:
        return False
    now_bj = get_bj_time()
    delta = now_bj - dt
    return delta.days <= max_days and delta.days >= -1  # 允许1天未来（时区误差）

def get_free_macro_news():
    print("📡 [阶段2] 正在抓取全球财经与A股新闻...")
    news_lines = []
    current_year = str(get_bj_time().year)

    sources = [
        ("新浪A股热点", "https://rss.sina.com.cn/roll/finance/hot_roll.xml"),
        ("华尔街日报(宏观)", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
        ("CNBC(宏观)", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ]

    for source_name, url in sources:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
            root = ET.fromstring(xml_data)
            items = root.findall('.//item')[:8]
            for item in items:
                title = item.find('title')
                pub_date = item.find('pubDate')
                if title is not None:
                    time_str = pub_date.text[:25] if pub_date is not None else ""
                    if not _is_recent_news(time_str, max_days=2):
                        continue
                    news_lines.append(f"[{source_name}] {time_str} - {title.text}")
            print(f"   ✅ {source_name} 节点抓取成功")
        except Exception as e:
            print(f"   ⚠️ {source_name} 节点抓取失败: {e}")

    if news_lines:
        print(f"✅ 盘前新闻矩阵组装完毕，共 {len(news_lines)} 条。")
        return "\n".join(news_lines)
    return "暂无实时新闻，请基于昨日收盘及底层产业逻辑推演。"


# ==========================================
# 2.6 获取国际宏观大宗数据
# ==========================================
def get_global_macro_data():
    print("🌐 [阶段2.6] 正在抓取国际宏观与大宗商品核心指标数据...")
    macro_tickers = {
        "10Y_US_Bond": ("^TNX", "美国10年期国债收益率"),
        "VIX": ("^VIX", "美股恐慌指数VIX"),
        "Gold": ("GC=F", "COMEX黄金期货"),
        "Silver": ("SI=F", "COMEX白银期货"),
        "Copper": ("HG=F", "COMEX铜期货"),
        "WTI_Oil": ("CL=F", "WTI原油期货"),
        "Brent_Oil": ("BZ=F", "布伦特原油期货"),
    }
    results = []
    vix_value = None

    for key, (ticker, desc) in macro_tickers.items():
        try:
            df = yf.download(ticker, period="5d", progress=False)
            if df is None or df.empty:
                results.append(f"❓ {desc} ({ticker}): 指标抓取受限")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            close_val = float(df['Close'].iloc[-1])
            prev_close = float(df['Close'].iloc[-2])
            pct_chg = round((close_val - prev_close) / prev_close * 100, 2)
            sign = "📈" if pct_chg > 0 else "📉"
            if key == "VIX":
                vix_value = close_val
                results.append(f"{sign} {desc} ({ticker}): {round(close_val, 2)} (当日变动: {pct_chg:+.2f}%)")
            elif key == "10Y_US_Bond":
                results.append(f"{sign} {desc} ({ticker}): {round(close_val, 3)}% (当日变动: {pct_chg:+.2f}%)")
            else:
                results.append(f"{sign} {desc} ({ticker}): ${round(close_val, 2)} (当日变动: {pct_chg:+.2f}%)")
        except Exception:
            results.append(f"❓ {desc} ({ticker}): 指标抓取受限")

    if not results:
        return "暂无外部宏观大宗商品监控数据。"

    guidance = ("\n【使用提示】以上大宗商品数据对不同行业的相关性差异很大：原油/WTI/布伦特"
                "主要影响石油化工、煤炭开采、航空运输、水路运输等上下游行业，对其他行业"
                "（如软件、消费、医药等）相关性很低，请结合每支标的自己的所属行业判断，"
                "不要不分行业地把油价波动同等代入所有个股的评分。")
    if vix_value is not None:
        if vix_value >= 30:
            guidance += (f"\n【VIX风控提示】当前VIX={round(vix_value,1)}，处于极度恐慌区间（>=30），"
                         f"全球风险偏好明显转弱。请提高评分门槛，对纯逻辑推演、缺乏新闻验证的"
                         f"高位追涨标的更加谨慎。")
        elif vix_value >= 25:
            guidance += f"\n【VIX风控提示】当前VIX={round(vix_value,1)}，处于偏高波动区间（>=25），请相应提高评分门槛。"
    return "\n".join(results) + guidance


# ==========================================
# 2.5 昨日美股板块表现
# ==========================================
def get_us_sector_performance():
    print("🇺🇸 [阶段2.5] 正在抓取昨日美股板块表现...")
    sector_map = {
        "XLK": "科技板块（半导体/软件/硬件）→ A股科技/半导体/AI板块",
        "SOXX": "费城半导体指数 → A股半导体/芯片设计/封测板块",
        "XLE": "能源板块（石油/天然气）→ A股石油/煤炭/新能源板块",
        "XLF": "金融板块（银行/保险/券商）→ A股银行/保险/券商板块",
        "XLV": "医疗健康板块 → A股医药/创新药/医疗器械板块",
        "XLY": "非必需消费（零售/汽车）→ A股消费/汽车板块",
        "XLI": "工业板块（航空/防务/制造）→ A股军工/制造/机器人板块",
        "XLB": "材料板块（矿业/化工）→ A股有色金属/化工板块",
        "ARKK": "创新科技（AI/基因/自动驾驶）→ A股AI/创新药/新能源汽车板块",
    }

    results = []
    try:
        import urllib.request
        yesterday = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        two_days_ago = (get_bj_time() - datetime.timedelta(days=3)).strftime('%Y-%m-%d')

        for ticker, description in sector_map.items():
            try:
                url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&d1={two_days_ago.replace('-','')}&d2={yesterday.replace('-','')}&i=d"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    content = resp.read().decode('utf-8')

                lines = [l.strip() for l in content.strip().split('\n') if l.strip()]
                if len(lines) >= 2:
                    last_line = lines[-1].split(',')
                    prev_line = lines[-2].split(',') if len(lines) >= 3 else None

                    if len(last_line) >= 5:
                        close_price = float(last_line[4])
                        if prev_line and len(prev_line) >= 5:
                            prev_close = float(prev_line[4])
                            pct_chg = round((close_price - prev_close) / prev_close * 100, 2)
                            sign = "📈" if pct_chg > 0 else "📉"
                            results.append(f"{sign} {ticker}: {pct_chg:+.2f}% — {description}")
                        else:
                            results.append(f"➖ {ticker}: 数据不足 — {description}")
                time.sleep(0.3)
            except Exception:
                results.append(f"❓ {ticker}: 抓取失败 — {description}")

        if results:
            print(f"✅ 美股板块数据获取完毕，共 {len(results)} 个板块。")
            return "\n".join(results)

    except Exception as e:
        print(f"⚠️ 美股板块数据抓取失败: {e}")

    return "暂无美股板块数据，请基于宏观新闻推演A股跟随效应。"


# ==========================================
# 3. 个股新闻抓取
# ==========================================
def get_stock_news(ticker_code: str, ticker_name: str, max_items: int = 5) -> list[str]:
    news_items = []
    code = ticker_code.split('.')[0]

    _HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Referer': 'https://www.eastmoney.com/'}

    try:
        url = (f"https://np-anotice-stock.eastmoney.com/api/security/ann"
               f"?sr=-1&page=1&size={max_items}&s=&c={code}&t=1,2,9,22,40")
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=7) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        ann_list = data.get('data', {}).get('list', [])
        for item in ann_list[:max_items]:
            title = str(item.get('title', '')).strip()
            date  = str(item.get('notice_date', ''))[:10]
            # 【新增】过滤过期公告（7天）
            try:
                notice_dt = datetime.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
                if (get_bj_time() - notice_dt).days > 7:
                    continue
            except Exception:
                pass
            if title:
                news_items.append(f"[东财公告][{date}] {title}")
    except Exception as e:
        pass

    if len(news_items) < max_items:
        try:
            if ticker_code.upper().endswith('.SH'):
                yahoo_ticker = code + '.SS'
            else:
                yahoo_ticker = code + '.SZ'
            cutoff_ts = time.time() - 14 * 86400
            raw = yf.Ticker(yahoo_ticker).news or []
            for item in raw:
                if len(news_items) >= max_items:
                    break
                if item.get('providerPublishTime', 0) < cutoff_ts:
                    continue
                title     = str(item.get('title', '')).strip()
                publisher = str(item.get('publisher', 'Yahoo'))
                pub_ts    = item.get('providerPublishTime', 0)
                date_str  = datetime.datetime.fromtimestamp(pub_ts).strftime('%m-%d') if pub_ts else ''
                if title:
                    news_items.append(f"[Yahoo/{publisher}][{date_str}] {title}")
        except Exception:
            pass

    if len(news_items) < max_items:
        try:
            sina_url = (f"https://feed.mix.sina.com.cn/api/roll/get"
                        f"?pageid=153&lid=2512&k={code}&num={max_items}&page=1")
            req = urllib.request.Request(sina_url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=6) as resp:
                content = resp.read().decode('utf-8')
            sina_data = json.loads(content)
            _JUNK_KEYWORDS = ('盘口', '亚盘', '竞彩', '让球', '胜负彩', '比分',
                              '欧冠', '英超', '西甲', '中超', 'NBA', 'CBA', '足彩', '首发阵容', '让分')
            for item in sina_data.get('result', {}).get('data', []):
                if len(news_items) >= max_items:
                    break
                title    = str(item.get('title', '')).strip()
                ctime    = str(item.get('ctime', ''))[:10]
                # 【新增】过滤过期新闻（7天）
                try:
                    # ctime 可能是时间戳或日期字符串
                    if ctime.isdigit() and len(ctime) == 10:
                        ctime_dt = datetime.datetime.fromtimestamp(int(ctime), tz=BEIJING_TZ)
                    else:
                        ctime_dt = datetime.datetime.strptime(ctime, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
                    if (get_bj_time() - ctime_dt).days > 7:
                        continue
                except Exception:
                    pass
                media    = str(item.get('media_name', '新浪财经'))
                is_relevant = bool(title) and (code in title or ticker_name[:2] in title)
                is_junk = any(kw in title for kw in _JUNK_KEYWORDS)
                if is_relevant and not is_junk:
                    news_items.append(f"[新浪/{media}][{ctime}] {title}")
        except Exception:
            pass

    return news_items[:max_items]


def enrich_pool_with_news(pool_data: list) -> list:
    print("📰 [阶段4] 正在逐只抓取个股新闻...")
    enriched = 0
    for idx, item in enumerate(pool_data[:100]):
        ticker_code = item.get('Ticker', '')
        ticker_name = item.get('Name', '')

        if idx < 30:
            news = get_stock_news(ticker_code, ticker_name, max_items=5)
            time.sleep(random.uniform(0.25, 0.55))
        else:
            code = ticker_code.split('.')[0]
            news = []
            try:
                url = (f"https://np-anotice-stock.eastmoney.com/api/security/ann"
                       f"?sr=-1&page=1&size=3&s=&c={code}&t=1,2,9,22,40")
                req = urllib.request.Request(
                    url,
                    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.eastmoney.com/'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                for ann in data.get('data', {}).get('list', [])[:3]:
                    title = str(ann.get('title', '')).strip()
                    date  = str(ann.get('notice_date', ''))[:10]
                    if title:
                        news.append(f"[东财公告][{date}] {title}")
            except Exception:
                pass
            time.sleep(random.uniform(0.08, 0.18))

        item['个股新闻'] = news
        if news:
            enriched += 1

    print(f"✅ 个股新闻抓取完毕：{enriched}/100 只标的有新闻")
    return pool_data


# ==========================================
# 4. 定向计算技术指标
# ==========================================
def calc_tech_indicators(full_pool, codes, trade_date):
    print("⚙️ [阶段3] 正在拉取日线+周线K线，分批计算技术指标...")
    start_hist   = (get_bj_time() - datetime.timedelta(days=120)).strftime('%Y%m%d')
    start_weekly = (get_bj_time() - datetime.timedelta(days=400)).strftime('%Y%m%d')
    batch_size   = 40

    all_hist = []
    for i in range(0, len(codes), batch_size):
        try:
            df_b = pro.daily(ts_code=",".join(codes[i:i+batch_size]),
                             start_date=start_hist, end_date=trade_date)
            if df_b is not None and not df_b.empty:
                all_hist.append(df_b)
            time.sleep(0.12)
        except Exception as e:
            pass
    df_hist = pd.concat(all_hist, ignore_index=True) if all_hist else pd.DataFrame()

    all_weekly = []
    for i in range(0, len(codes), batch_size):
        try:
            df_w = pro.weekly(ts_code=",".join(codes[i:i+batch_size]),
                              start_date=start_weekly, end_date=trade_date)
            if df_w is not None and not df_w.empty:
                all_weekly.append(df_w)
            time.sleep(0.15)
        except Exception as e:
            pass
    df_weekly = pd.concat(all_weekly, ignore_index=True) if all_weekly else pd.DataFrame()

    FALLBACK = [
        ("乖离率(%)", 0.0), ("RSI", 50.0), ("MACD趋势", "N/A"),
        ("MACD_HIST_LAST", 0.0), ("MACD_HIST_PREV", 0.0),
        ("MACD金叉", False), ("MACD绿柱缩短", False),
        ("周线共振", False),
        ("KDJ_J", 50.0), ("KDJ_J回升", False), ("KDJ_J超卖", False),
        ("量能放大", False), ("量比", 1.0), ("看涨形态", []),
        ("ATR", 0.0), ("ATR_Pct", 5.0),
    ]

    for code in list(full_pool.keys()):
        weekly_bullish = False
        weekly_macd_rising = False  # 【新增】初始化，防止 len(wk)<12 时 NameError
        if not df_weekly.empty and code in df_weekly['ts_code'].values:
            wk = df_weekly[df_weekly['ts_code'] == code].sort_values('trade_date')
            if len(wk) >= 12:
                wc     = wk['close'].values.astype(float)
                wma5   = float(pd.Series(wc).rolling(5).mean().iloc[-1])
                wma10  = float(pd.Series(wc).rolling(10).mean().iloc[-1])
                w_exp1 = pd.Series(wc).ewm(span=12, adjust=False).mean()
                w_exp2 = pd.Series(wc).ewm(span=26, adjust=False).mean()
                w_hist = (w_exp1 - w_exp2 - (w_exp1 - w_exp2).ewm(span=9, adjust=False).mean()) * 2
                weekly_macd_rising = float(w_hist.iloc[-1]) > float(w_hist.iloc[-2])  # 【新增】回测核心因子
                weekly_bullish = bool(wma5 > wma10 and weekly_macd_rising)
        full_pool[code]["周线共振"] = weekly_bullish
        full_pool[code]["周线MACD上升"] = weekly_macd_rising  # 【新增】存储周线上升状态
        if df_hist.empty or code not in df_hist['ts_code'].values:
            for fld, dflt in FALLBACK:
                full_pool[code].setdefault(fld, dflt)
            continue

        sd = df_hist[df_hist['ts_code'] == code].sort_values('trade_date')
        if len(sd) < 30:
            for fld, dflt in FALLBACK:
                full_pool[code].setdefault(fld, dflt)
            continue

        cp  = sd['close'].values.astype(float)
        hp  = sd['high'].values.astype(float)
        lp  = sd['low'].values.astype(float)
        op  = sd['open'].values.astype(float)
        vol = sd['vol'].values.astype(float)
        sc  = pd.Series(cp)

        ma20 = float(sc.rolling(20).mean().iloc[-1])
        full_pool[code]["乖离率(%)"] = round(((full_pool[code]["Close"] - ma20) / ma20) * 100, 2)

        exp1   = sc.ewm(span=12, adjust=False).mean()
        exp2   = sc.ewm(span=26, adjust=False).mean()
        ml     = exp1 - exp2
        sl     = ml.ewm(span=9, adjust=False).mean()
        hist   = (ml - sl) * 2
        h_last, h_prev, h_prev2 = float(hist.iloc[-1]), float(hist.iloc[-2]), float(hist.iloc[-3])
        ml_last, ml_prev = float(ml.iloc[-1]), float(ml.iloc[-2])
        sl_last, sl_prev = float(sl.iloc[-1]), float(sl.iloc[-2])

        full_pool[code]["MACD趋势"]      = "走强" if h_last > h_prev else "走弱"
        full_pool[code]["MACD_HIST_LAST"] = round(h_last, 4)
        full_pool[code]["MACD_HIST_PREV"] = round(h_prev, 4)
        full_pool[code]["MACD金叉"]       = bool(ml_last > sl_last and ml_prev <= sl_prev)
        full_pool[code]["MACD绿柱缩短"]   = bool(h_last < 0 and h_last > h_prev and h_prev < h_prev2)

        full_pool[code]["日线MACD上升"]   = h_last > h_prev  # 【新增】回测核心因子
        delta = sc.diff()
        gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss  = (-1 * delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
        full_pool[code]["RSI"] = round(float((100 - 100 / (1 + gain / (loss + 1e-9))).iloc[-1]), 2)

        K, D = 50.0, 50.0
        j_lst = []
        for i_k in range(len(cp)):
            if i_k < 8:
                j_lst.append(3*K - 2*D)
                continue
            h9  = max(hp[i_k-8: i_k+1])
            l9  = min(lp[i_k-8: i_k+1])
            rsv = (cp[i_k] - l9) / (h9 - l9 + 1e-9) * 100
            K   = 2/3*K + 1/3*rsv
            D   = 2/3*D + 1/3*K
            j_lst.append(3*K - 2*D)
        j_last, j_prev, j_prev2 = j_lst[-1], j_lst[-2], j_lst[-3]
        full_pool[code]["KDJ_J"]    = round(float(j_last), 2)
        full_pool[code]["KDJ_J回升"] = bool(j_last < 80 and j_last > j_prev and j_prev <= j_prev2)
        full_pool[code]["KDJ_J超卖"] = bool(j_prev2 < 20)

        prev_close_arr = np.roll(cp, 1)
        prev_close_arr[0] = cp[0]
        tr_arr = np.maximum(hp - lp, np.maximum(np.abs(hp - prev_close_arr), np.abs(lp - prev_close_arr)))
        atr_last = float(pd.Series(tr_arr).ewm(com=13, adjust=False).mean().iloc[-1])
        full_pool[code]["ATR"] = round(atr_last, 4)
        full_pool[code]["ATR_Pct"] = round((atr_last / full_pool[code]["Close"]) * 100, 2) if full_pool[code].get("Close") else 5.0

        avg5  = float(pd.Series(vol[:-1]).tail(5).mean()) if len(vol) >= 6 else 0
        vtdy  = float(vol[-1])
        full_pool[code]["量能放大"] = bool(avg5 > 0 and vtdy >= avg5 * 1.3)
        full_pool[code]["量比"]     = round(vtdy / (avg5 + 1e-9), 2)

        patterns = []
        if len(op) >= 3:
            o, c   = op[-1], cp[-1]
            o1, c1 = op[-2], cp[-2]
            rng    = hp[-1] - lp[-1] + 1e-9
            body   = abs(c - o)
            l_shd  = min(o, c) - lp[-1]
            u_shd  = hp[-1] - max(o, c)
            if c1 < o1 and c > o and o <= c1 and c >= o1:
                patterns.append("看涨吞没")
            if body/rng < 0.35 and l_shd >= 2*body and u_shd <= body*0.5:
                patterns.append("锤子线")
            if c1 < o1 and c > o and o < c1 and c > (o1+c1)/2 and c < o1:
                patterns.append("刺穿线")
            o2, c2 = op[-3], cp[-3]
            if c2 < o2 and abs(c2-o2) > rng*0.3 and abs(c1-o1) < abs(c2-o2)*0.4 and c > o and c > (o2+c2)/2:
                patterns.append("启明星")
        full_pool[code]["看涨形态"] = patterns

    final_pool = sorted(list(full_pool.values()), key=lambda x: x.get("Amount", 0), reverse=True)
    return final_pool


# ==========================================
# 5. AI 事件推演选股
# ==========================================
# ==========================================
# 【新增】周期共振检测（回测验证：A股胜率57.43%）
# 条件：日线MACD↑ + 周线MACD↑ + 看涨形态（吞没/启明星/锤子/刺穿）
# ==========================================
def check_period_resonance(stock):
    """
    检测单只股票是否满足周期共振
    返回: (是否共振, 形态列表)
    """
    # 1. 检查MACD状态
    daily_rising = stock.get("日线MACD上升", False)
    weekly_rising = stock.get("周线MACD上升", False)
    if not daily_rising or not weekly_rising:
        return False, []
    
    # 2. 检查看涨形态
    patterns = stock.get("看涨形态", [])
    valid_patterns = ["看涨吞没", "启明星", "刺穿线", "锤子线"]
    matched = [p for p in patterns if p in valid_patterns]
    
    if not matched:
        return False, []
    
    return True, matched

def screen_technical_setups(final_pool):
    sector_groups = {}
    for stock in final_pool[:100]:
        tech_score   = 0
        tech_reasons = []

        # 【新增】周期共振检测（最高优先级）
        is_resonance, resonance_patterns = check_period_resonance(stock)
        stock["周期共振"] = is_resonance
        stock["共振形态"] = resonance_patterns

        if stock.get("MACD金叉"):
            tech_score += 15
            tech_reasons.append("MACD金叉(+15)")
        elif stock.get("MACD绿柱缩短"):
            h_last = stock.get("MACD_HIST_LAST", 0)
            h_prev = stock.get("MACD_HIST_PREV", 0)
            pts = 12 if (h_last < 0 and abs(h_last) < abs(h_prev) * 0.85) else 8
            tech_score += pts
            tech_reasons.append(f"MACD绿柱收敛(+{pts})")
        elif stock.get("MACD趋势") == "走强" and stock.get("MACD_HIST_LAST", 0) > 0:
            tech_score += 4
            tech_reasons.append("MACD红柱走强(+4)")

        j_val = stock.get("KDJ_J", 50)
        if stock.get("KDJ_J回升"):
            if stock.get("KDJ_J超卖") or j_val < 20:
                tech_score += 10; tech_reasons.append(f"KDJ超卖回头J={j_val:.0f}(+10)")
            elif j_val < 50:
                tech_score += 7;  tech_reasons.append(f"KDJ低位回升J={j_val:.0f}(+7)")
            else:
                tech_score += 3;  tech_reasons.append(f"KDJ中位回升J={j_val:.0f}(+3)")

        vr = stock.get("量比", 1.0)
        if stock.get("量能放大"):
            pts = 10 if vr >= 2.0 else 7
            tech_score += pts; tech_reasons.append(f"量比{vr:.1f}倍(+{pts})")

        patterns = stock.get("看涨形态", [])
        if patterns:
            pm = {"看涨吞没": 5, "启明星": 5, "刺穿线": 4, "锤子线": 3}
            base = min(max(pm.get(p, 2) for p in patterns) + (2 if len(patterns) > 1 else 0), 5)
            tech_score += base; tech_reasons.append(f"{'&'.join(patterns)}(+{base})")

        weekly = stock.get("周线共振", False)
        if weekly:
            tech_score = min(int(tech_score * 1.25), 40)
            tech_reasons.append("✅周日共振×1.25")
        elif tech_score > 0:
            tech_score = int(tech_score * 0.6)
            tech_reasons.append("⚠️仅日线×0.6")

        stock["技术评分"] = min(tech_score, 40)

        # 【新增】如果满足周期共振，直接加15分（不破坏40分上限）
        if is_resonance:
            tech_score = min(tech_score + 15, 40)
            tech_reasons.append("🔥周期共振(+15)")
        stock["技术信号"] = tech_reasons

        industry = stock.get("Industry", "其他")
        sector_groups.setdefault(industry, []).append({
            "名称": stock["Name"], "代码": stock["Ticker"],
            "技术评分": tech_score, "技术信号": tech_reasons,
        })

    summary = {
        sec: sorted(stks, key=lambda x: x["技术评分"], reverse=True)
        for sec, stks in sector_groups.items()
        if any(s["技术评分"] > 0 for s in stks)
    }
    return summary


def load_evolved_rules() -> str:
    rules_file = "evolved_rules.json"
    if not os.path.exists(rules_file):
        return ""
    try:
        with open(rules_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        patches      = data.get("prompt_patches", [])
        active_rules = data.get("active_rules", [])
        if not patches:
            return ""
        last_updated = data.get("last_updated", "未知")
        recent = data.get("recent_win_rate")
        if recent and recent.get("胜率") is not None:
            win_rate_display = f"{recent['胜率']}%（最近{recent.get('样本数','?')}笔，当前规则的真实表现）"
        else:
            win_rate_display = f"{data.get('overall_win_rate', '未知')}%（全部历史混合，仅供参考）"
        lines = [
            f"【📈 历史绩效驱动进化规则（上次更新: {last_updated} | 胜率: {win_rate_display}）】",
            "以下规则由策略进化引擎基于真实交易数据自动生成，必须严格遵守：",
            ""
        ]
        for i, (rule, patch) in enumerate(zip(active_rules, patches), 1):
            lines.append(f"规则{i}【{rule.get('type','')}】{rule.get('description','')}")
            if rule.get("evidence"):
                lines.append(f"  数据依据: {rule['evidence']}")
            lines.append(f"  执行要求: {patch}")
            lines.append("")
        lines.append("（以上规则优先级高于一般选股偏好，但低于今日突发事件强制封禁）")
        return "\n".join(lines)
    except Exception as e:
        return ""


def generate_ai_report(pool_data, macro_news_text, macro_data_text, us_sector_text, removed_tickers, embargo_text="", sector_tech_data=None):
    print("🧠 [阶段4] 召唤 AI 大脑...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )
    today_str = get_bj_time().strftime('%Y年%m月%d日')

    compact_pool = []
    for d in pool_data[:100]:
        item = {
            "名称": d["Name"], "代码": d["Ticker"], "行业": d["Industry"],
            "收盘价": d["Close"], "今日涨跌(%)": d.get("pct_chg", 0),
            "乖离率(%)": d.get("乖离率(%)", "N/A"), "RSI": d.get("RSI", "N/A"),
            "MACD": d.get("MACD趋势", "N/A"),
            "技术评分(满分40)": d.get("技术评分", 0),
            "技术信号": d.get("技术信号", []),
            "周线共振": "🟢是" if d.get("周线共振") else "🔴否",
            "MACD金叉": "✅是" if d.get("MACD金叉") else "否",
            "KDJ_J": d.get("KDJ_J", "N/A"), "量比": d.get("量比", "N/A"),
            "看涨形态": d.get("看涨形态", []),
        }
        if d.get('个股新闻'):
            item["个股新闻"] = d['个股新闻']
        compact_pool.append(item)

    tech_sector_block = ""
    if sector_tech_data:
        lines = []
        for sec, stks in sorted(sector_tech_data.items(),
                                 key=lambda x: max(s["技术评分"] for s in x[1]), reverse=True)[:8]:
            top3 = [f"{s['名称']}({s['代码']})技{s['技术评分']}分"
                    for s in stks[:3] if s["技术评分"] > 0]
            if top3:
                lines.append(f"  {sec}: {' / '.join(top3)}")
        if lines:
            tech_sector_block = "【今日技术形态板块共振（评分>0的标的按板块归类）】：\n" + "\n".join(lines)

    evolved_rules_block = load_evolved_rules()

    removed_notice = ""
    if removed_tickers:
        removed_notice = f"""
⚠️ 【今日盘前突发事件强制清仓暂停股】：
以下股票今日已被风险控制强平暂停，今日选股策略中绝对禁止再次重新选入或推荐：
{', '.join(removed_tickers)}
"""

    prompt = f'''
你是顶级A股事件驱动型游资操盘手，擅长从全球宏观事件、美债大宗异动推演底层传导链条，并结合个股新闻做三重交叉验证。

今天是{today_str}。

{removed_notice}

{evolved_rules_block}

{embargo_text}

【今日全球宏观与A股消息面】：
{macro_news_text}

【今日核心国际宏观与金银铜油大宗数据监测】：
{macro_data_text}

【昨日美股各板块涨跌】：
{us_sector_text}

{tech_sector_block}

【今日A股交易额 Top 100（含技术评分+个股新闻）】：
{json.dumps(compact_pool, ensure_ascii=False)}

【核心工作流程】（基于回测验证，严格执行）：
第一步（新闻定方向）：从宏观新闻中提炼出今日1-2条最强产业链主线（例如：AI算力→半导体、降息预期→有色、地缘冲突→油气）。
第二步（板块锁定）：只在你提炼出的主线板块中寻找标的。严禁跳出主线去买"技术面好但没新闻"的票。
第三步（技术选个股）：在主线板块内，**必须优先选择【周期共振】为 True 的标的**（即代码已自动标记满足：日线MACD↑ + 周线MACD↑ + 看涨吞没/启明星/刺穿线/锤子线）。
第四步（评分确认）：如果存在周期共振标的，直接将其排入Top1-5，除非该标的有重大负面新闻。若无共振标的，再退而求其次选择技术评分≥20的票，但须在报告中明确警示"无共振信号"。

【输出格式要求】（严格按以下HTML骨架输出，不要输出任何其他文字）：

1. 今日产业链主线研判（用 <div class="header-card"> 包裹）：
   - 提炼1-2条最强产业链主线
   - 标注今日雷区（高乖离率+高RSI的高位票、大股东抛售、负面传闻等）

2. 产业链主线优选 Top 1-5（每只用一个 <div class="card core-card"> 包裹，必须包含以下字段）：
   <div class="card core-card">
     <h3>股票名称 (代码) | RSI:xx | 乖离率:xx%</h3>
     <p><b>产业链逻辑:</b> ...</p>
     <p><b>个股新闻核查:</b> ...</p>
     <p><b>技术确认:</b> ...</p>
     <p><b>推荐评分:</b> 评分:[xx]/100 — ...</p>
     <p><b>风控底线:</b> 周期:[x-x天] | 止损:[xx元或-x%]</p>
   </div>

3. 观察池（用 <div class="card obs-card"> 包裹，里面用 <li> 列出）：
   技术评分≥20但新闻面不够强的标的，标注"观望"

4. 新闻预警组（用 <div class="card trap-card"> 包裹，里面用 <li> 列出）：
   有负面新闻或技术形态走坏的标的，标注"回避"

【极其重要】：
- Top 1-5 必须包含 class="card core-card"，否则系统无法解析入库
- 观察池必须包含 class="card obs-card"
- 新闻预警组必须包含 class="card trap-card"
- 直接输出HTML代码，不要加 markdown 代码块（```html）
- 第一个字符必须是 < 符号
'''

    # 【修复】调用 AI 生成报告（替换原来的 dummy 实现）
    try:
        response = client.messages.create(
            model=TARGET_MODEL,
            max_tokens=80000,
            messages=[{"role": "user", "content": prompt}]
        )
        ai_html = response.content[0].text.strip()
        # 清理可能的 markdown 代码块
        ai_html = ai_html.replace("```html", "").replace("```", "").strip()
        html_start = ai_html.find("<div")
        if html_start > 0:
            print(f"⚠️ 检测到AI输出前置了 {html_start} 字符的非HTML内容，已自动截断丢弃")
            ai_html = ai_html[html_start:]
        print(f"✅ AI 报告生成完成，共 {len(ai_html)} 字符")
    except Exception as e:
        print(f"🚨 AI 报告生成失败: {e}")
        ai_html = "<div class='header-card'><h2>🌍 AI报告生成失败</h2><p>请检查API配置和网络连接</p></div>"

    return ai_html

def build_email(ai_html):
    style = """
    <style>
        body{font-family:sans-serif;background:#f4f6f9;color:#333;padding:20px;line-height:1.6}
        .container{max-width:1000px;margin:0 auto}
        .header-card{background:#eaf4ff;border-radius:8px;padding:25px;margin-bottom:25px;border-left:6px solid #1976d2}
        .card{background:#fff;border-radius:10px;padding:25px;margin-bottom:25px;box-shadow:0 4px 15px rgba(0,0,0,.06)}
        .core-card{border-left:6px solid #d32f2f}
        .sub-card{border-left:6px solid #546e7a}
        .obs-card{background:#fffcf9;border-left:6px solid #ff9800}
        .trap-card{background:#fbfcfe;border-left:6px solid #607d8b}
        .tag{display:inline-block;padding:4px 10px;border-radius:4px;font-weight:bold;font-size:13px;color:#fff;margin-right:8px}
        .bg-red{background:#d32f2f}
        .bg-blue{background:#455a64}
        .bg-purple{background:#6a1b9a}
        .bg-orange{background:#e64a19}
        .bg-gray{background:#607d8b}
        .bg-green{background:#2e7d32}
        .bg-teal{background:#00897b}
        .bear-text{color:#d32f2f;font-weight:bold}
        .market-section{margin-bottom:30px}
        .market-title{font-size:20px;font-weight:bold;color:#1565c0;margin-bottom:20px;padding-bottom:10px;border-bottom:2px solid #1565c0}
    </style>
    """
    return f"<!DOCTYPE html><html><head><meta charset='utf-8'>{style}</head><body><div class='container'>{ai_html}</div></body></html>"

def send_emails(html_content):
    acc = os.environ.get("EMAIL_ACCOUNT")
    pwd = os.environ.get("EMAIL_PASSWORD")
    email_list_str = os.environ.get("TARGET_EMAILS")

    if not acc or not pwd or not email_list_str:
        print("⚠️ 邮箱配置缺失，跳过发送。")
        return

    msg = MIMEMultipart()
    msg['Subject'], msg['From'] = "【宏观大宗事件驱动】A股逻辑推演精选", f"Alpha Radar <{acc}>"
    msg.attach(MIMEText(html_content, 'html'))
    targets = [e.strip() for e in email_list_str.split(",")]

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(acc, pwd)
        server.sendmail(acc, targets, msg.as_string())
        server.quit()
        print("✅ 邮件密送成功！")
    except Exception as e:
        print(f"🚨 邮件发送失败: {e}")

def match_pool_to_report(pool_data, ai_html, default_stop_loss_pct):
    def clean_fragment(text):
        t = re.sub(r'<[^>]+>', ' ', text)
        return re.sub(r'\s+', ' ', t).strip()

    def title_hit(fragment, name, ticker):
        head = fragment[:110]
        if f"({ticker})" in head:
            return True
        if '.' in ticker and f"({ticker.split('.')[0]})" in head:
            return True
        return name in fragment[:30]

    obs_start = ai_html.find('class="card obs-card"')
    if obs_start == -1:
        obs_start = ai_html.find('观察池')
    if obs_start == -1:
        obs_start = len(ai_html)

    trap_start = ai_html.find('class="card trap-card"')
    if trap_start == -1:
        trap_start = ai_html.find('新闻预警组')
    if trap_start == -1 or trap_start < obs_start:
        trap_start = len(ai_html)

    core_zone_raw = ai_html[:obs_start]
    obs_zone_raw = ai_html[obs_start:trap_start]
    trap_zone_raw = ai_html[trap_start:]

    core_cards = [clean_fragment(c) for c in re.split(r'(?=<div class="card core-card">)', core_zone_raw) if 'core-card' in c]
    obs_items = [clean_fragment(c) for c in re.split(r'(?=<li>)', obs_zone_raw) if c.strip().startswith('<li>')]
    trap_items = [clean_fragment(c) for c in re.split(r'(?=<li>)', trap_zone_raw) if c.strip().startswith('<li>')]

    chosen = []
    for item in pool_data:
        ticker_code = str(item['Ticker'])
        name = str(item['Name'])

        tag, chunk = None, None
        for card in core_cards:
            if title_hit(card, name, ticker_code):
                tag, chunk = "Core_Dragon", card
                break
        if tag is None:
            for li in obs_items:
                if title_hit(li, name, ticker_code):
                    tag, chunk = "Observation", li
                    break
        if tag is None:
            for li in trap_items:
                if title_hit(li, name, ticker_code):
                    tag, chunk = "Trap_Warning", li
                    break

        if tag is None or tag == "Trap_Warning":
            continue

        period_match = re.search(r'周期\s*[:：]\s*\[?(\d+[-~]\d+天|\d+天|观望)', chunk)

        if tag == "Observation":
            hold_period, stop_loss, score = "观望", "观望", "N/A"
        else:
            hold_period = period_match.group(1).strip() if period_match else "5-12天"
            sl_match = re.search(r'止损\s*[:：]\s*\[?(\d{1,5}\.\d{1,2}元)', chunk)
            stop_loss_raw = sl_match.group(1).strip() if sl_match else None

            if stop_loss_raw:
                try:
                    sl_value = float(re.sub(r'[^\d.]', '', stop_loss_raw))
                    ref_price = item.get('Open', item['Close'])
                    if abs(sl_value - ref_price) / ref_price > 0.30:
                        stop_loss_raw = None
                except (ValueError, ZeroDivisionError):
                    stop_loss_raw = None

            atr_pct = item.get('ATR_Pct', 5.0)
            dynamic_stop_pct = -max(ATR_STOP_FLOOR_PCT, min(ATR_STOP_CEIL_PCT, atr_pct * ATR_STOP_MULTIPLIER))
            stop_loss = stop_loss_raw if stop_loss_raw else f"{round(item.get('Open', item['Close']) * (1 + dynamic_stop_pct / 100), 2)}元"
            score_match = re.search(r'评分\s*[:：]\s*\[?(\d{1,3})\]?\s*/\s*100', chunk)
            score = score_match.group(1).strip() if score_match else "N/A"

        item['Tag'] = tag
        item['Hold_Period'] = hold_period
        item['Stop_Loss'] = stop_loss
        item['Score'] = score
        item['Daily_Pct'] = item.get('pct_chg', 0)
        chosen.append(item)

    return chosen


if __name__ == "__main__":
    macro_news = get_free_macro_news()
    macro_data_text = get_global_macro_data()
    latest_price_map = get_latest_price_map()

    removed_tickers_macro = pre_scan_portfolio_review(macro_news, macro_data_text, latest_price_map)
    rule_sell_signals, removed_tickers_rule = check_rule_based_sell_signals(
        latest_price_map, exclude_tickers=removed_tickers_macro
    )

    removed_tickers = removed_tickers_macro + removed_tickers_rule

    sell_signal_card_html = build_sell_signal_card(removed_tickers_macro, rule_sell_signals)
    current_holdings_card_html = build_current_holdings_card(latest_price_map)

    us_sector_text = get_us_sector_performance()
    embargo_sectors, embargo_text = parse_sector_embargo(us_sector_text)

    full_pool, codes, trade_date = get_top_300_pool()
    if not full_pool:
        print("🚨 核心资金池为空，程序退出。")
        import sys; sys.exit(1)

    final_pool = calc_tech_indicators(full_pool, codes, trade_date)
    sector_tech_summary = screen_technical_setups(final_pool)

    pool_with_news = enrich_pool_with_news(final_pool)

    ai_report_html = generate_ai_report(
        pool_with_news,
        macro_news,
        macro_data_text,
        us_sector_text,
        removed_tickers,
        embargo_text,
        sector_tech_summary
    )

    # Insert cards into HTML
    insertion_point = ai_report_html.find('<div class="market-section">')
    if insertion_point != -1:
        ai_report_html = ai_report_html[:insertion_point] + sell_signal_card_html + current_holdings_card_html + ai_report_html[insertion_point:]
    else:
        ai_report_html = sell_signal_card_html + current_holdings_card_html + ai_report_html

    chosen = match_pool_to_report(pool_with_news, ai_report_html, DEFAULT_STOP_LOSS_PCT)

    if chosen:
        log_file = "trade_history.csv"
        df_new = pd.DataFrame(chosen)
        
        # 【新增】过滤：已在持仓中的标的不再重复追加，避免卖出信号每天重复触发
        if os.path.exists(log_file):
            try:
                df_old = pd.read_csv(log_file)
                _active_tags = {'Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon'}
                _active_tickers = set(df_old[df_old['Tag'].isin(_active_tags)]['Ticker'].unique())
                _before_filter = len(df_new)
                df_new = df_new[~df_new['Ticker'].isin(_active_tickers)]
                _skipped = _before_filter - len(df_new)
                if _skipped > 0:
                    print(f"📋 跳过 {_skipped} 只已在持仓中的标的，不重复追加")
            except Exception as e:
                print(f"⚠️ 持仓去重过滤失败，继续追加全部: {e}")
        
        if not df_new.empty:
            df_new['Date'] = get_bj_time().strftime('%Y-%m-%d %H:%M:%S')
            df_new['Close_Price'] = df_new['Close']
            cols = ['Date', 'Ticker', 'Name', 'Industry', 'Tag', 'Close_Price', 'Hold_Period', 'Stop_Loss', 'Score', 'Daily_Pct']
            for c in cols:
                if c not in df_new.columns:
                    df_new[c] = ''
            df_new = df_new[cols]
            
            if os.path.exists(log_file):
                df_old = pd.read_csv(log_file)
                df_final = pd.concat([df_old, df_new], ignore_index=True)
            else:
                df_final = df_new
            df_final.to_csv(log_file, index=False)
            print(f"✅ 已将 {len(df_new)} 只精选标的入账 trade_history.csv")
        else:
            print("📋 今日无新入选标的，全部已在持仓中，跳过入账。")

    final_email_html = build_email(ai_report_html)
    send_emails(final_email_html)
    print("🎉 今日早盘扫描与逻辑推演全部完成！")
