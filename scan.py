# 消息+逻辑推演驱动版 | 事件→产业链→受益标的 | 个股新闻深度版 | Top5详细分析+评分版
# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import json
import re
import smtplib
import urllib.request
import xml.etree.ElementTree as ET
import tushare as ts
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic
import time

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

ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()


# ==========================================
# 0. 扫描前：统一获取最新可用收盘价表
# ==========================================
def get_latest_price_map():
    """
    统一拉取持仓股票的最新可用价格，供阶段0a/0b共用。
    优先级：
      1. ts.get_realtime_quotes()  —— 盘中实时行情，9:30-15:00 内有效，拿到的是最新成交价
      2. pro.daily(今日)           —— 盘后收盘价，盘中通常为空
      3. pro.daily(昨日)           —— 最终兜底，至少保证有一个参考价

    盘中运行时强烈依赖方案1，方案2/3仅作兜底，避免全部回退到买入价导致盈亏=0的问题。
    """
    # 先从 trade_history.csv 读出所有需要定价的 ticker
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

    # ── 方案1：实时行情（盘中首选）──
    if holding_tickers:
        try:
            # ts_code 格式: 000001.SZ → tushare realtime 需要去掉后缀变成 "000001" 再加市场前缀
            # ts.get_realtime_quotes 接受不带后缀的代码列表
            bare_codes = [t.split('.')[0] for t in holding_tickers]
            df_rt = ts.get_realtime_quotes(bare_codes)
            if df_rt is not None and not df_rt.empty and 'price' in df_rt.columns:
                # 重建 ts_code（需要判断交易所后缀）
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

    # ── 方案2：今日 daily 收盘价（盘后有效）──
    try:
        trade_date_latest = get_bj_time().strftime('%Y%m%d')
        df_prices = pro.daily(trade_date=trade_date_latest)
        if df_prices is not None and not df_prices.empty:
            price_map = dict(zip(df_prices['ts_code'], df_prices['close']))
            print(f"✅ 今日收盘价拉取成功，共 {len(price_map)} 只（盘后口径）")
            return price_map
    except Exception as e:
        print(f"⚠️ 今日 daily 失败: {e}")

    # ── 方案3：昨日 daily 收盘价（最终兜底）──
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
    """
    在正式选股之前，先读取 trade_history.csv 里的当前持仓，
    结合今日消息面和宏观大宗数据（美债收益率、金银铜油等），
    让 AI 判断哪些股票因为突发消息应该立即强制清仓。
    不再直接擦除记录，而是将 Tag 改为 'Forced_Exit' 并计算买入卖出价录入历史。
    """
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

        # 只看还在持仓窗口内的 Core 类票
        active_tags = ['Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon']
        holdings = recent[recent['Tag'].isin(active_tags)].copy()

        if holdings.empty:
            print("📋 [阶段0] 当前无有效持仓，跳过持仓审查。")
            return []

        # ── 新版本标记过滤：Hold_Period / Stop_Loss / Score 三字段缺一不可 ──
        # 旧版本记录缺少这三个字段，视为无效持仓，不纳入风控审查。
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

        # 每只股只取最新一条
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
{macro_news_text[:1000]} # 节约算力：截取核心新闻

【今日国际宏观大宗指标】：
{macro_data_text}

【当前持仓列表】：
{json.dumps(holdings_info, ensure_ascii=False)}

【你的任务】：
审查每只持仓股票，判断今日消息面、全球宏观数据以及大宗商品价格异动，是否对该股票产生了严重的负面冲击，从而需要立即强制清仓。

判断标准（满足任意一条即建议清仓）：
1. 今日新闻中有该公司或其所在行业的直接突发重大负面消息
2. 宏观事件或大宗商品剧烈震荡导致该行业的产业链逻辑根本性反转
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
        # 使用 Flash 引擎进行日常算力节约版排雷
        response = client.messages.create(
            model=TARGET_MODEL,
            max_tokens=1000, 
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

    # 当前最新价作为卖出价基准（由外部统一传入的 price_map，避免重复请求 tushare）

    # 保留数据并修正 Tag
    try:
        df_orig = pd.read_csv(log_file)
        for ticker in to_remove:
            df_orig.loc[df_orig['Ticker'] == ticker, 'Tag'] = 'Forced_Exit'
        df_orig.to_csv(log_file, index=False)
        print(f"🔒 [阶段0] 已在 trade_history.csv 中将 {to_remove} 的标签锁定为 'Forced_Exit'（暂停后续追踪）")
    except Exception as e:
        print(f"⚠️ [阶段0] trade_history.csv 标签状态更新失败: {e}")

    # 写入 review_history.csv
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
    """
    在阶段0a的AI宏观审查之后，对剩余仍在追踪的持仓做一次纯规则检测：
      1. 现价（price_map中的最新可用收盘价）已跌破 Stop_Loss 价位 → "止损触发"
      2. 距首次推荐日已达到/超过 Hold_Period 建议周期上限 → "持有到期"
    这两类判断完全基于 trade_history.csv 里已经写好的数值（止损价、持股周期建议），
    不需要再调用AI，因此 scan.py 在交易时段内运行时就能立刻产出可执行的卖出信号，
    不必等到收盘后的 review.py 才知道。

    命中后：
      - trade_history.csv：该 Ticker 的 Tag 锁定为 'Stop_Loss_Hit' 或 'Period_Matured'，停止后续追踪/重复推荐。
      - review_history.csv：归档买入价/现价，Tag 保留原始推荐标签（Core_Dragon等，供 evolve.py 计入真实胜率），
        Status 记为 '止损触发清仓' 或 '周期到期清仓'。
      - sell_signal_log.csv：独立记录一份，供邮件顶部"今日卖出信号"卡片渲染使用。

    返回: (sell_signals: List[dict], removed_tickers: List[str])
    """
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
        holdings = recent[recent['Tag'].isin(active_tags)].copy()
        if holdings.empty:
            print("📋 [阶段0b] 当前无有效持仓，跳过规则卖出信号检测。")
            return [], []

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
            print("📋 [阶段0b] 过滤后无有效新版本持仓，跳过规则卖出信号检测。")
            return [], []

        # 每只股只取最新一条（与阶段0a保持一致的去重口径）
        holdings = holdings.sort_values('Date', ascending=False).drop_duplicates(subset='Ticker', keep='first')
        # 阶段0a本轮已经强清的标的不再重复判断
        holdings = holdings[~holdings['Ticker'].astype(str).isin(exclude_tickers)]
        if holdings.empty:
            print("📋 [阶段0b] 持仓已被阶段0a全部处理，跳过规则卖出信号检测。")
            return [], []
    except Exception as e:
        print(f"⚠️ [阶段0b] 持仓读取失败: {e}")
        return [], []

    def _parse_hold_days(hold_period_str):
        s = str(hold_period_str).strip()
        if not s or s.lower() in ['n/a', 'nan'] or s in ['坚决空仓', '观望']:
            return None
        nums = re.findall(r'\d+', s)
        return int(nums[-1]) if nums else None  # 取区间上限，如"5-10天"取10

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
        ticker = str(row['Ticker'])
        buy_price = float(row['Close_Price'])
        buy_date = row['Date']
        orig_tag = row['Tag']  # 此时一定是 active_tags 之一（Core_Dragon等）
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
            "reason": reason.replace(",", "，"),  # 防止英文逗号破坏后续CSV手写格式
        })
        removed_tickers.append(ticker)

    if not sell_signals:
        print("✅ [阶段0b] 规则审查：当前持仓无止损触发或持有到期信号。")
        return [], []

    # 锁定 trade_history.csv 标签，停止后续追踪/重复推荐
    try:
        df_orig = pd.read_csv(log_file)
        for s in sell_signals:
            tag_to_set = 'Stop_Loss_Hit' if s['signal_type'] == '止损触发' else 'Period_Matured'
            df_orig.loc[df_orig['Ticker'] == s['ticker'], 'Tag'] = tag_to_set
        df_orig.to_csv(log_file, index=False)
        print(f"🔒 [阶段0b] 已锁定 {len(sell_signals)} 只标的标签（止损触发/持有到期），停止后续追踪")
    except Exception as e:
        print(f"⚠️ [阶段0b] trade_history.csv 标签更新失败: {e}")

    # 归档至 review_history.csv —— 保留原始推荐 Tag（供 evolve.py 计入真实胜率统计）
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

    # 独立卖出信号日志，供邮件卡片渲染（与AI宏观强清的 forced_exit_log.csv 区分开）
    try:
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
# 0c. 统一渲染"今日卖出信号"卡片（汇总阶段0a的AI强清 + 阶段0b的规则信号）
# ==========================================
def build_sell_signal_card(macro_removed_tickers, rule_sell_signals):
    """
    把阶段0a（AI宏观突发利空强清）与阶段0b（止损触发/持有到期）两类信号
    汇总成一张醒目卡片，插在邮件最顶部。
    这样只要打开今天的scan邮件，就能在一个地方看到"今天必须处理的持仓"，
    可以在交易时段内直接找机会卖出，不需要等盘后review.py的复盘邮件。
    """
    if not macro_removed_tickers and not rule_sell_signals:
        return ""

    rows_html = ""

    if macro_removed_tickers:
        reason_map = {}
        try:
            import csv
            if os.path.exists("forced_exit_log.csv"):
                with open("forced_exit_log.csv", "r", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if row.get("Ticker") in macro_removed_tickers:
                            reason_map[row["Ticker"]] = row.get("Reason", "")
        except Exception:
            pass
        for t in macro_removed_tickers:
            reason_text = reason_map.get(t, "突发宏观/消息面利空，AI风控强平")
            rows_html += f"""
        <tr style="border-bottom:1px solid #ffe0b2;">
            <td style="padding:8px 4px;"><b>{t}</b></td>
            <td style="padding:8px 4px;"><span class="tag bg-red" style="margin:0;">突发利空强清</span></td>
            <td style="padding:8px 4px;" colspan="2">{reason_text}</td>
        </tr>"""

    for s in rule_sell_signals:
        pnl_color = "#d32f2f" if s['pnl_pct'] >= 0 else "#388e3c"
        tag_color = "bg-orange" if s['signal_type'] == '止损触发' else "bg-gray"
        rows_html += f"""
        <tr style="border-bottom:1px solid #ffe0b2;">
            <td style="padding:8px 4px;"><b>{s['name']}({s['ticker']})</b></td>
            <td style="padding:8px 4px;"><span class="tag {tag_color}" style="margin:0;">{s['signal_type']}</span></td>
            <td style="padding:8px 4px;">买入¥{s['buy_price']} → 现价¥{s['current_price']}，<span style="color:{pnl_color};font-weight:bold;">{s['pnl_pct']:+.2f}%</span></td>
            <td style="padding:8px 4px;">{s['reason']}</td>
        </tr>"""

    total = len(macro_removed_tickers) + len(rule_sell_signals)
    return f"""
<div style="background:#fff3e0; border-left:6px solid #e65100; padding:20px; margin-bottom:25px; border-radius:8px;">
    <h3 style="margin:0 0 12px 0; color:#bf360c;">🔔 今日卖出信号汇总（共{total}只 · 交易时段内可直接执行）</h3>
    <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <tr style="text-align:left; color:#6d4c41; border-bottom:1px solid #ffb74d;">
            <th style="padding:6px 4px;">标的</th><th style="padding:6px 4px;">触发类型</th><th style="padding:6px 4px;">价格/浮动盈亏</th><th style="padding:6px 4px;">理由</th>
        </tr>
        {rows_html}
    </table>
    <p style="margin:12px 0 0 0; font-size:13px; color:#6d4c41;">以上标的已在 trade_history.csv 中锁定标签并停止后续追踪，买入价/现价已归档至 review_history.csv 供胜率统计。本卡片仅给出系统信号，实际下单价格与时机仍需结合当时盘口自行判断。</p>
</div>
"""


# ==========================================
# 2.6  美股板块大跌 → A股联动封禁清单（规则驱动，不依赖AI判断）
# ==========================================

# ETF → 对应A股板块中文标签映射（供封禁通知和后续过滤使用）
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

# 封禁阈值：美股板块单日跌幅超过此值，对应A股板块今日进入封禁名单
EMBARGO_THRESHOLD_PCT = -1.5   # -1.5% 触发预警；-3% 触发强封

def parse_sector_embargo(us_sector_text):
    """
    解析 get_us_sector_performance() 的输出，找出跌幅超过阈值的ETF，
    生成两部分输出：
      1. embargo_sectors: list[str] —— 今日A股被封禁的板块关键词列表
         （注入AI prompt中作为硬性禁止推荐的依据）
      2. embargo_text: str —— 格式化的封禁通知文本，直接插入AI prompt醒目位置

    设计原则：跌幅越大封禁力度越强，SOXX是最重要的信号，单独列出；
    不试图让AI自行"综合判断"，而是把已经判断好的结论作为约束传入。
    """
    if not us_sector_text or "暂无" in us_sector_text:
        return [], ""

    embargo_sectors = []
    embargo_lines = []

    for line in us_sector_text.strip().split('\n'):
        line = line.strip()
        if not line or '📉' not in line:
            continue
        try:
            # 格式: 📉 SOXX: -3.45% — 费城半导体指数 → A股半导体...
            parts = line.replace('📉', '').strip().split(':')
            etf = parts[0].strip()
            pct_str = parts[1].strip().split('%')[0].strip()
            pct = float(pct_str)
        except Exception:
            continue

        if pct >= EMBARGO_THRESHOLD_PCT:  # 没超阈值（是正数或跌幅较小），跳过
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

    embargo_sectors = list(dict.fromkeys(embargo_sectors))  # 去重，保持顺序
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
            "Close": row['close'],
            "Amount": row['amount'],
            "pct_chg": row.get('pct_chg', 0),
        }

    print(f"✅ 成功圈定 {len(full_pool)} 只核心活跃标的（数据日期: {trade_date}）。")
    return full_pool, codes, trade_date


# ==========================================
# 2. 宏观新闻采集
# ==========================================
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
                    if current_year not in time_str:
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
# 2.6 获取国际宏观大宗数据 (国债收益率与金银铜油)
# ==========================================
def get_global_macro_data():
    print("🌐 [阶段2.6] 正在抓取国际宏观与大宗商品核心指标数据...")
    macro_symbols = {
        "10Y_US_Bond": ("10y_us.m", "美国10年期国债收益率"),
        "Gold": ("gc.f", "COMEX黄金期货"),
        "Silver": ("si.f", "COMEX白银期货"),
        "Copper": ("hg.f", "COMEX铜期货"),
        "WTI_Oil": ("cl.f", "WTI原油期货"),
        "Brent_Oil": ("cb.f", "布伦特原油期货")
    }
    results = []
    yesterday = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    two_days_ago = (get_bj_time() - datetime.timedelta(days=4)).strftime('%Y-%m-%d')
    
    for key, (symbol, desc) in macro_symbols.items():
        try:
            url = f"https://stooq.com/q/d/l/?s={symbol}&d1={two_days_ago.replace('-','')}&d2={yesterday.replace('-','')}&i=d"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                content = resp.read().decode('utf-8')
            lines = [l.strip() for l in content.strip().split('\n') if l.strip()]
            if len(lines) >= 2:
                last_line = lines[-1].split(',')
                prev_line = lines[-2].split(',') if len(lines) >= 3 else None
                if len(last_line) >= 5:
                    close_val = float(last_line[4])
                    if prev_line and len(prev_line) >= 5:
                        prev_close = float(prev_line[4])
                        pct_chg = round((close_val - prev_close) / prev_close * 100, 2)
                        sign = "📈" if pct_chg > 0 else "📉"
                        if key == "10Y_US_Bond":
                            results.append(f"{sign} {desc} ({symbol}): {close_val}% (当日变动: {pct_chg:+.2f}%)")
                        else:
                            results.append(f"{sign} {desc} ({symbol}): ${close_val} (当日变动: {pct_chg:+.2f}%)")
                    else:
                        results.append(f"原始指标 {desc} ({symbol}): {close_val}")
            time.sleep(0.2)
        except Exception:
            results.append(f"❓ {desc} ({symbol}): 指标抓取受限")
            
    if not results:
        return "暂无外部宏观大宗商品监控数据。"
    return "\n".join(results)


# ==========================================
# 2.5 昨日美股板块表现（用于推论A股跟随效应）
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
def get_stock_news(ticker_name, max_items=3):
    headlines = []
    return headlines


def enrich_pool_with_news(pool_data):
    print("📰 [阶段3.5] 正在为 Top 100 标的抓取个股新闻...")

    all_sina_news = []
    try:
        req = urllib.request.Request(
            "https://rss.sina.com.cn/roll/finance/hot_roll.xml",
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        items = root.findall('.//item')[:200]
        for item in items:
            title = item.find('title')
            if title is not None and title.text:
                all_sina_news.append(title.text.strip())
    except Exception as e:
        print(f"   ⚠️ 新浪新闻批量抓取失败: {e}")

    tushare_news = []
    try:
        df_news = pro.news(src='sina', limit=100)
        if df_news is not None and not df_news.empty:
            tushare_news = df_news['title'].tolist()
    except Exception:
        pass

    combined_news = all_sina_news + tushare_news

    enriched = 0
    for item in pool_data[:100]:
        name = item.get('Name', '')
        keyword = name[:3] if len(name) >= 3 else name

        matched = []
        for news_title in combined_news:
            if keyword in news_title or name in news_title:
                matched.append(news_title)
            if len(matched) >= 3:
                break

        item['个股新闻'] = matched if matched else []
        if matched:
            enriched += 1

    print(f"✅ 个股新闻匹配完毕，{enriched} 只标的找到相关新闻。")
    return pool_data


# ==========================================
# 4. 定向计算技术指标（分批抓取 + 免死金牌）
# ==========================================
def calc_tech_indicators(full_pool, codes, trade_date):
    print("⚙️ [阶段3] 正在回头定向拉取 Top 300 的历史K线，分批次绕过 API 限制...")

    start_hist = (get_bj_time() - datetime.timedelta(days=120)).strftime('%Y%m%d')
    all_hist_data = []
    batch_size = 40

    try:
        for i in range(0, len(codes), batch_size):
            batch_codes = codes[i:i+batch_size]
            try:
                df_batch = pro.daily(
                    ts_code=",".join(batch_codes),
                    start_date=start_hist,
                    end_date=trade_date
                )
                if df_batch is not None and not df_batch.empty:
                    all_hist_data.append(df_batch)
                time.sleep(0.12)
            except Exception as e:
                print(f"   ⚠️ 批次拉取受限: {e}")

        df_hist = pd.concat(all_hist_data, ignore_index=True) if all_hist_data else pd.DataFrame()

        for code in list(full_pool.keys()):
            if not df_hist.empty and code in df_hist['ts_code'].values:
                stock_data = df_hist[df_hist['ts_code'] == code].copy().sort_values('trade_date')
                if len(stock_data) >= 30:
                    close_px = stock_data['close']
                    ma20 = close_px.rolling(window=20).mean().iloc[-1]
                    current_close = full_pool[code]["Close"]
                    full_pool[code]["乖离率(%)"] = round(((current_close - ma20) / ma20) * 100, 2)

                    exp1 = close_px.ewm(span=12, adjust=False).mean()
                    exp2 = close_px.ewm(span=26, adjust=False).mean()
                    macd_line = exp1 - exp2
                    signal_line = macd_line.ewm(span=9, adjust=False).mean()
                    macd_hist = (macd_line - signal_line) * 2
                    full_pool[code]["MACD趋势"] = "走强" if macd_hist.iloc[-1] > macd_hist.iloc[-2] else "走弱"

                    delta = close_px.diff()
                    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
                    loss = (-1 * delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
                    rs = gain / loss
                    full_pool[code]["RSI"] = round((100 - (100 / (1 + rs))).iloc[-1], 2)
                    continue

            full_pool[code]["乖离率(%)"] = 0.0
            full_pool[code]["RSI"] = 50.0
            full_pool[code]["MACD趋势"] = "API限流(纯事件驱动)"

    except Exception as e:
        print(f"🚨 指标全局处理受限: {e}，启用全量兜底。")
        for code in full_pool:
            if "RSI" not in full_pool[code]:
                full_pool[code]["乖离率(%)"] = 0.0
                full_pool[code]["RSI"] = 50.0
                full_pool[code]["MACD趋势"] = "API崩溃保护"

    final_pool = sorted(list(full_pool.values()), key=lambda x: x.get("Amount", 0), reverse=True)
    print(f"✅ 技术指标模块执行完毕，最终保全 {len(final_pool)} 只核心标的。")
    return final_pool


# ==========================================
# 5. AI 事件与全球宏观逻辑推演选股
# ==========================================
def generate_ai_report(pool_data, macro_news_text, macro_data_text, us_sector_text, removed_tickers, embargo_text=""):
    print("🧠 [阶段4] 召唤 AI 大脑（宏观大宗与三重交叉验证，Top5详细分析）...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )
    today_str = get_bj_time().strftime('%Y年%m月%d日')

    compact_pool = []
    for d in pool_data[:100]:
        stock_info = {
            "名称": d["Name"],
            "代码": d["Ticker"],
            "行业": d["Industry"],
            "收盘价": d["Close"],
            "今日涨跌(%)": d.get("pct_chg", 0),
            "乖离率(%)": d.get("乖离率(%)", "N/A"),
            "RSI": d.get("RSI", "N/A"),
            "MACD": d.get("MACD趋势", "N/A"),
        }
        individual_news = d.get('个股新闻', [])
        if individual_news:
            stock_info["个股新闻"] = individual_news
        compact_pool.append(stock_info)

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

【今日全球宏观与A股消息面】：
{macro_news_text}

【今日核心国际宏观与金银铜油大宗数据监测】：
{macro_data_text}

【昨日美股各板块涨跌】：
{us_sector_text}

{embargo_text}

【今日A股交易额 Top 100（含个股最新新闻）】：
{json.dumps(compact_pool, ensure_ascii=False)}

【你的核心工作流程】：

━━━━━━━━━━━━━━━━━━━━━━
第零步：全球宏观、美债收益率与金银铜油大宗传导分析（关键升级）
━━━━━━━━━━━━━━━━━━━━━━
深入结合提供的宏观数据与大宗商品变化（美债收益率变动、金银铜油价格走向）进行大势与逻辑推演：
1. 深入分析外部环境的宏观冲击（例如类似PCE爆表砸盘美股指数等事件），明确判断这种下跌是“短暂的情绪性洗盘”还是“由宏观基本面逆转导致的趋势破位（Trend Reversal）”。
2. 推论高收益美债对A股成长股/高位股的抽水压力，以及金、银、铜、原油暴涨/暴跌对周期股与中游制造业成本链的直接传导关系。
3. 将此宏观及大宗商品综合判定结论写入报告的"全球宏观大宗与美股传导分析"区块。

━━━━━━━━━━━━━━━━━━━━━━
第一步：宏观事件识别与产业链推演
━━━━━━━━━━━━━━━━━━━━━━
仔细阅读上方所有宏观新闻和大宗异动，识别出今日最重要的2-3个核心事件。对每个事件做完整的产业链推演。
在"今日核心事件与完整逻辑链"概述中，尽量用行业或板块描述，避免逐一点名太多具体公司全称，把具体公司名称留给下面各自的详细卡片里说明。

━━━━━━━━━━━━━━━━━━━━━━
第二步：个股新闻交叉验证
━━━━━━━━━━━━━━━━━━━━━━
对每只候选标的，必须检查其个股新闻字段：
✅ 加分情形（优先推荐）：个股新闻与宏观主线高度吻合，或有正面公告共振。
⚠️ 中性情形（正常分析）：暂无个股新闻：需注明"无最新个股消息，纯逻辑推演"。
❌ 减分/排除情形（必须说明）：有负面新闻的票必须强行剥离出精选池。

━━━━━━━━━━━━━━━━━━━━━━
第三步：技术面风控兜底
━━━━━━━━━━━━━━━━━━━━━━
乖离率>20% 且 RSI>85 视为技术极度透支，列入受损避险组。

━━━━━━━━━━━━━━━━━━━━━━
第四步：推荐评分（1-100分）
━━━━━━━━━━━━━━━━━━━━━━
对每一只进入【核心精选】（Top 1-5）的标的，必须给出一个1-100的综合评分：
- 评分格式必须严格为：评分:[XX]/100。
- 评分应结合宏观大宗趋势符合度、个股新闻直接度、资金池额度进行权衡，拉开各标的分数区间。

━━━━━━━━━━━━━━━━━━━━━━
第五步：输出详细报告
━━━━━━━━━━━━━━━━━━━━━━
【硬性纪律】：
1. 【核心精选】Top 1-5 每只都必须按完整模板逐项写满。
2. 同一只股票绝对不能重复出现。
3. 风控底线格式：周期:[X-Y天] | 止损:[XX.XX元]（止损必须贴近该股当前收盘价）。
4. 严格按以下HTML骨架输出，不加markdown外框。第一个字符必须是 < 符号。

<div class="header-card">
    <h2>🌍 今日全球宏观大宗与事件逻辑推演中心</h2>
    <p><b>执行时间：</b>{today_str} 盘前</p>

    <div style="background:#e8f5e9;border-left:4px solid #388e3c;padding:15px;margin-top:10px;border-radius:4px;">
        <b>🇺🇸 全球宏观大宗与美股传导分析：</b>
        <p>[深度整合国债收益率变动及金银铜油大宗异动，全面研判市场当前冲击（例如PCE压制等）是属于短暂回调还是趋势改变，并指出今日A股跟随效应或避险板块方向]</p>
    </div>

    <div style="background:#fff3e0;border-left:4px solid #ff9800;padding:15px;margin-top:10px;border-radius:4px;">
        <b>📋 今日核心事件与完整逻辑链：</b>
        <p><b>事件1：</b>[事件标题] → [完整推演：为什么这个事件利好/利空哪个产业链，受益逻辑是什么，预计持续多久]</p>
        <p><b>事件2：</b>[事件标题] → [完整推演]</p>
        <p><b>受损预警：</b>[哪些行业/标的因宏观数据或者大宗价格链条传导受损，需回避]</p>
    </div>
</div>

<div class="market-section">
    <div class="market-title">🇨🇳 [核心精选] A股事件驱动 Top 1-5 详细分析</div>

    <div class="card core-card">
        <h3>[核心精选] 1. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>[具体事件] → [产业链传导机制] → [该企业核心受益竞争优势点]</p>
        <p><span class="tag bg-green">🇺🇸 宏观大宗加持：</span>[说明收益率/金银铜油趋势对该行业的宏观传导利弊，以及美股对标动向效应]</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>[相关个股新闻匹配判定，如无则写"暂无最新个股消息，纯宏观逻辑推演"]</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>今日交易额位于巨量核心池，涨跌[X]%，[主力、机构等大资金行为推断]</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>乖离率[X]%，RSI[X]，MACD[走强/走弱]，[技术综合研判]</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — [一句话评分科学依据描述]</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | [止损精细依据]</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 2. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-green">🇺🇸 宏观大宗加持：</span>(...)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 3. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-green">🇺🇸 宏观大宗加持：</span>(...)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 4. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-green">🇺🇸 宏观大宗加持：</span>(...)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 5. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-green">🇺🇸 宏观大宗加持：</span>(...)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card obs-card">
        <h3>[观察池] ⚠️ 逻辑待确认或个股新闻有瑕疵 (Rank 6-10)</h3>
        <ul>
            <li><b>6. [名称] ([代码]) | [行业]：</b>[因由阐述] <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            <li><b>7. [名称] ([代码]) | [行业]：</b>(...) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            <li><b>8. [名称] ([代码]) | [行业]：</b>(...) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            <li><b>9. [名称] ([代码]) | [行业]：</b>(...) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            <li><b>10. [名称] ([代码]) | [行业]：</b>(...) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
        </ul>
    </div>
</div>

<div class="card trap-card">
    <h3>🚨 事件逻辑受损或个股新闻预警组（严禁接盘）</h3>
    <ul>
        <li><b>[名称] ([代码]) | <span class="bear-text">逻辑受损/新闻预警</span></b><br>❌ 受损逻辑：[具体宏观或大宗负面破坏链条说明]<br>⚠️ 回避理由：[潜在风险释放空间描述]</li>
    </ul>
</div>
'''

    ai_html = ""
    # 使用 Pro 引擎负责高强度的核心推演
    with client.messages.stream(
        model=TARGET_MODEL,
        max_tokens=8000,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            ai_html += text

    ai_html = ai_html.replace("```html", "").replace("```", "").strip()

    html_start = ai_html.find("<div")
    if html_start > 0:
        print(f"⚠️ 检测到AI输出前置了 {html_start} 字符的非HTML内容，已自动截断丢弃")
        ai_html = ai_html[html_start:]

    # 注：原有的"强制清仓"卡片渲染逻辑已统一移至 build_sell_signal_card()，
    # 与阶段0b的止损/到期信号合并为一张"今日卖出信号汇总"卡片，由 __main__ 在邮件最顶部统一插入，
    # 避免同一批 removed_tickers 在邮件里出现两张内容重复的卡片。

    print("✅ AI 事件逻辑推演报告生成完毕")
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
    msg['Subject'], msg['From'] = "【宏观大宗事件驱动】A股逻辑推演精选(Top5详细+评分)", f"Alpha Radar <{acc}>"
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


def locate_stock_section(clean_html, ticker_code, name):
    bare_code = ticker_code.split('.')[0] if '.' in ticker_code else ticker_code

    idx = clean_html.find(f"({ticker_code})")
    if idx != -1:
        return idx

    idx = clean_html.find(f"({bare_code})")
    if idx != -1:
        return idx

    name_positions = []
    start = 0
    while True:
        pos = clean_html.find(name, start)
        if pos == -1:
            break
        name_positions.append(pos)
        start = pos + 1

    for pos in name_positions:
        nearby = clean_html[max(0, pos - 60):pos + 60]
        if "核心精选" in nearby or "观察池" in nearby or "逻辑受损" in nearby or "新闻预警" in nearby:
            return pos

    return name_positions[0] if name_positions else -1


if __name__ == "__main__":
    # 阶段2：拉取宏观与核心大宗商品高频数据
    macro_news = get_free_macro_news()
    macro_data_text = get_global_macro_data()

    # 统一拉取一次最新可用收盘价，阶段0a/0b共用，避免重复请求 tushare
    latest_price_map = get_latest_price_map()

    # 阶段0a：AI 宏观/消息面驱动的持仓强制清仓审查
    removed_tickers_macro = pre_scan_portfolio_review(macro_news, macro_data_text, latest_price_map)

    # 阶段0b：规则驱动卖出信号检测（止损触发 / 持有到期）——纯数值判断，交易时段内即可拿到结果
    rule_sell_signals, removed_tickers_rule = check_rule_based_sell_signals(
        latest_price_map, exclude_tickers=removed_tickers_macro
    )

    removed_tickers = removed_tickers_macro + removed_tickers_rule

    # 汇总两类信号，生成统一的"今日卖出信号"卡片（后面会插到邮件最顶部）
    sell_signal_card_html = build_sell_signal_card(removed_tickers_macro, rule_sell_signals)

    # 阶段2.5：获取昨日美股板块数据
    us_sector_text = get_us_sector_performance()

    # 阶段2.6：解析美股板块大跌，生成A股联动封禁清单（硬性规则，不依赖AI判断）
    _embargo_sectors, embargo_text = parse_sector_embargo(us_sector_text)

    # 阶段1：拉取今日A股核心资金池
    full_pool, codes, trade_date = get_top_300_pool()

    if full_pool:
        final_pool = calc_tech_indicators(full_pool, codes, trade_date)

        if len(final_pool) < 10:
            print("🚨 触发安全熔断：清洗后有效标的不足10只，终止 AI 调用。")
            import sys; sys.exit(0)

        final_pool = enrich_pool_with_news(final_pool)

        # AI 推演
        ai_html = generate_ai_report(final_pool, macro_news, macro_data_text, us_sector_text, removed_tickers, embargo_text)
        # 把"今日卖出信号"卡片插在邮件最顶部，第一眼就能看到当天该处理的持仓
        ai_html = sell_signal_card_html + ai_html
        full_html = build_email(ai_html)

        chosen = []
        clean_html = re.sub(r'<[^>]+>', ' ', ai_html)
        clean_html = re.sub(r'\s+', ' ', clean_html)

        for item in final_pool:
            ticker_code = str(item['Ticker'])
            name = str(item['Name'])

            idx = locate_stock_section(clean_html, ticker_code, name)
            if idx == -1:
                continue

            chunk = clean_html[idx:idx + 1500]
            context = clean_html[max(0, idx - 300):idx] + chunk[:200]

            tag = None
            if "核心精选" in context:
                tag = "Core_Dragon"
            elif "观察池" in context:
                tag = "Observation"
            elif "逻辑受损" in context or "坚决回避" in context or "新闻预警" in context:
                tag = "Trap_Warning"

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
                        if abs(sl_value - item['Close']) / item['Close'] > 0.30:
                            print(f"⚠️ {item['Name']} 止损价 {stop_loss_raw} 与现价 {item['Close']} 偏离过大，改用默认止损")
                            stop_loss_raw = None
                    except (ValueError, ZeroDivisionError):
                        stop_loss_raw = None

                stop_loss = stop_loss_raw if stop_loss_raw else f"{round(item['Close'] * (1 + DEFAULT_STOP_LOSS_PCT / 100), 2)}元"
                score_match = re.search(r'评分\s*[:：]\s*\[?(\d{1,3})\s*/\s*100', chunk)
                score = score_match.group(1).strip() if score_match else "N/A"

            item['Tag'] = tag
            item['Hold_Period'] = hold_period
            item['Stop_Loss'] = stop_loss
            item['Score'] = score
            item['Daily_Pct'] = item.get('pct_chg', 0)
            chosen.append(item)

        log_file = "trade_history.csv"
        new_header = "Date,Ticker,Name,Tag,Industry,Close_Price,Amount,Daily_Pct,Hold_Period,Stop_Loss,Score\n"
        file_exists = os.path.exists(log_file) and os.path.getsize(log_file) > 0
        need_header = not file_exists

        if file_exists:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines and "Score" not in lines[0]:
                lines[0] = new_header
                with open(log_file, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                print("⚠️ 检测到旧版trade_history.csv缺少Score列，已自动升级表头")

        # ── 写账前过滤：剔除已被标记为 Forced_Exit / Trap_Warning / Stop_Loss_Hit / Period_Matured 的 ticker ──
        # 只查 trade_history.csv 本身，不依赖阶段0返回值，确保跨进程重启也能正确过滤。
        # 原有历史行（含买入价、卖出价）保持不动，供 review.py / evolve.py 计算胜率。
        frozen_tickers: set = set()
        FROZEN_TAGS = {'Forced_Exit', 'Trap_Warning', 'Stop_Loss_Hit', 'Period_Matured'}
        if file_exists:
            try:
                df_hist_check = pd.read_csv(log_file, on_bad_lines='skip')
                if 'Tag' in df_hist_check.columns and 'Ticker' in df_hist_check.columns:
                    frozen_tickers = set(
                        df_hist_check.loc[df_hist_check['Tag'].isin(FROZEN_TAGS), 'Ticker'].astype(str)
                    )
                    if frozen_tickers:
                        print(f"🔒 写账过滤：检测到 {len(frozen_tickers)} 只冻结标的 {frozen_tickers}，本次不追加新行（历史买卖价保留）")
            except Exception as e:
                print(f"⚠️ 写账过滤读取 trade_history.csv 失败，不执行过滤: {e}")

        chosen_to_write = [i for i in chosen if str(i['Ticker']) not in frozen_tickers]
        skipped_frozen = len(chosen) - len(chosen_to_write)
        if skipped_frozen > 0:
            print(f"⏭️ 已跳过 {skipped_frozen} 只冻结标的，不写入新追踪记录。")

        with open(log_file, "a", encoding="utf-8") as f:
            if need_header:
                f.write(new_header)
            ts_date = get_bj_time().strftime('%Y-%m-%d')
            for i in chosen_to_write:
                f.write(f"{ts_date},{i['Ticker']},{i['Name']},{i['Tag']},{i.get('Industry','未知')},{i['Close']},{i['Amount']},{i['Daily_Pct']},{i['Hold_Period']},{i['Stop_Loss']},{i.get('Score','N/A')}\n")

        print(f"✅ 共安全记账 {len(chosen_to_write)} 条核心数据（冻结过滤后）。")
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(full_html)
        send_emails(full_html)
    else:
        print("⚠️ 数据池为空，跳过执行。")
        # 兜底：即使本次主选股流程因数据问题中止，只要阶段0a/0b产生了卖出信号，
        # 也要单独发一封邮件通知，避免"今天该卖的股票"被悄悄吞掉。
        if sell_signal_card_html:
            fallback_html = f"""
<div class="header-card">
    <h2>⚠️ 今日选股流程未完成，仅推送卖出信号</h2>
    <p>本次资金池数据拉取失败，AI选股报告未生成；但持仓卖出信号检测不依赖资金池数据，照常推送如下。</p>
</div>
{sell_signal_card_html}
"""
            send_emails(build_email(fallback_html))
