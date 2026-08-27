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
import email.utils

# ==========================================
# 启动前置校验：AI 凭证
# ==========================================
_missing_env = [k for k in ("CLAWSOCKET_API_KEY", "CLAWSOCKET_BASE_URL") if not os.environ.get(k)]
if _missing_env:
    print(f"致命错误：未检测到环境变量 {', '.join(_missing_env)}！")
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
        print(f"⚠️ 版本标记读取失败: {e}")
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
        print(f"📌 检测到 scan.py 变化，新版本起始日期: {today_str}")
    else:
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            version_date = existing.split(",")[1] if "," in existing else "未知"
            print(f"📌 scan.py 版本未变，起始日期: {version_date}")
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

TARGET_MODEL = 'claude-opus-4-8'
DEFAULT_STOP_LOSS_PCT = -5.0
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
        raw = ""
        with client.messages.stream(
            model=TARGET_MODEL,
            max_tokens=80000,
            messages=[{"role": "user", "content": review_prompt}]
        ) as stream:
            for text in stream.text_stream:
                raw += text
        raw = raw.strip()
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
# 0b. 规则驱动卖出信号检测（已废弃，由 review.py 统一处理，此处保留空壳）
# ==========================================
def check_rule_based_sell_signals(price_map, exclude_tickers=None):
    # 此函数不再执行任何操作，所有卖出信号由 review.py 处理
    return [], []

# ==========================================
# 0c. 统一渲染"今日卖出信号"卡片（由 review.py 生成，此处仅保留空壳）
# ==========================================
def build_sell_signal_card(macro_removed_tickers, rule_sell_signals):
    return ""

def build_current_holdings_card(latest_price_map):
    return ""

# ==========================================
# 0d. 【新增】读取昨日止损标的，生成联动警告
# ==========================================
def get_stop_loss_hit_warning():
    log_file = "trade_history.csv"
    if not os.path.exists(log_file):
        return ""

    try:
        df = pd.read_csv(log_file, keep_default_na=False)
        if 'Tag' not in df.columns or 'Exit_Date' not in df.columns:
            return ""

        hit_df = df[df['Tag'].astype(str).str.strip() == 'Stop_Loss_Hit'].copy()
        if hit_df.empty:
            return ""

        hit_df = hit_df.sort_values('Exit_Date', ascending=False).head(5)
        tickers = hit_df['Ticker'].unique().tolist()
        if not tickers:
            return ""

        details = []
        for _, row in hit_df.iterrows():
            name = row.get('Name', row['Ticker'])
            exit_date = row.get('Exit_Date', '未知日期')
            details.append(f"{name}({row['Ticker']}) @ {exit_date}")

        return (
            f"\n⚠️ 【昨日/近期止损风控联动警告】：以下标的在最近交易中被系统标记为「止损触发清仓」（Stop_Loss_Hit），"
            f"今日选股严禁将其列入 Top 1-5 核心推荐，仅允许在「诱多对照组」中作为反面案例提及。\n"
            f"涉及标的：{', '.join(details)}\n"
        )
    except Exception as e:
        print(f"⚠️ 读取止损警告失败: {e}")
        return ""

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

    try:
        df_mf = pro.moneyflow(trade_date=trade_date)
        if df_mf is not None and not df_mf.empty:
            df_mf['main_net'] = df_mf.get('lg_amount', 0) + df_mf.get('net_mf_amount', 0)
            mf_map = dict(zip(df_mf['ts_code'], df_mf['main_net']))
            for ts_code in full_pool:
                full_pool[ts_code]['主力净流入(万元)'] = mf_map.get(ts_code, 0)
            sector_flow = {}
            for ts_code, data in full_pool.items():
                sector = data.get('Industry', '其他')
                sector_flow[sector] = sector_flow.get(sector, 0) + data.get('主力净流入(万元)', 0)
            top5_sectors = sorted(sector_flow.items(), key=lambda x: x[1], reverse=True)[:5]
            hot_sectors = {s[0] for s in top5_sectors if s[1] > 0}
            for ts_code in full_pool:
                full_pool[ts_code]['热点板块'] = full_pool[ts_code].get('Industry', '其他') in hot_sectors
            print(f"✅ 板块资金流向计算完成，热点板块: {list(hot_sectors)}")
    except Exception as e:
        print(f"⚠️ 板块资金流向获取失败: {e}")

    return full_pool, codes, trade_date

# ==========================================
# 2. 宏观新闻采集
# ==========================================
def _parse_rss_date(date_str: str):
    if not date_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(BEIJING_TZ)
    except Exception:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(date_str.strip()[:25], fmt)
            return dt.replace(tzinfo=BEIJING_TZ)
        except Exception:
            pass
    return None

def _get_macro_news_time_tag(dt_obj):
    if dt_obj is None:
        return "[📑前日-低权重]", "20% → 5分"
    now_bj = get_bj_time()
    delta = now_bj - dt_obj
    hours = delta.total_seconds() / 3600
    if hours <= 6:
        return "[🔥今日最新-权重最高]", "满分25分"
    elif delta.days == 0 or (delta.days == 1 and hours <= 24):
        return "[📰今日-高权重]", "80% → 20分"
    elif delta.days <= 2 or (delta.days == 2 and hours <= 48):
        return "[📄昨日-中等权重]", "50% → 12分"
    elif delta.days <= 3 or (delta.days == 3 and hours <= 72):
        return "[📑前日-低权重]", "20% → 5分"
    else:
        return "[📑前日-低权重]", "20% → 5分"

def get_free_macro_news():
    print("📡 [阶段2] 正在抓取全球财经与A股新闻...")
    news_entries = []
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
            items = root.findall('.//item')[:20]
            for item in items:
                title = item.find('title')
                pub_date = item.find('pubDate')
                if title is not None:
                    time_str = pub_date.text[:25] if pub_date is not None else ""
                    dt_obj = _parse_rss_date(time_str)
                    if dt_obj is None:
                        continue
                    if (get_bj_time() - dt_obj).days > 7:
                        continue
                    tag, weight_desc = _get_macro_news_time_tag(dt_obj)
                    news_entries.append((dt_obj, source_name, time_str, title.text, tag, weight_desc))
            print(f"   ✅ {source_name} 节点抓取成功")
        except Exception as e:
            print(f"   ⚠️ {source_name} 节点抓取失败: {e}")

    if not news_entries:
        return "暂无实时新闻，请基于昨日收盘及底层产业逻辑推演。"

    news_entries.sort(key=lambda x: x[0], reverse=True)
    today_str = get_bj_time().strftime('%Y-%m-%d')
    yesterday_str = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    two_days_ago_str = (get_bj_time() - datetime.timedelta(days=2)).strftime('%Y-%m-%d')

    grouped = {"今日最新": [], "今日": [], "昨日": [], "前日": []}
    for dt_obj, source_name, time_str, title_text, tag, weight_desc in news_entries:
        date_key = dt_obj.strftime('%Y-%m-%d')
        if date_key == today_str:
            if "🔥" in tag:
                bucket = "今日最新"
            else:
                bucket = "今日"
        elif date_key == yesterday_str:
            bucket = "昨日"
        elif date_key == two_days_ago_str:
            bucket = "前日"
        else:
            bucket = "前日"
        grouped[bucket].append(f"{tag} [{source_name}] {time_str} - {title.text}")

    output_lines = []
    for bucket in ["今日最新", "今日", "昨日", "前日"]:
        if grouped[bucket]:
            output_lines.append(f"\n【{bucket}】")
            output_lines.extend(grouped[bucket])

    news_lines = "\n".join(output_lines).strip()
    print(f"✅ 盘前新闻矩阵组装完毕，共 {len(news_entries)} 条，已按时效递减排序。")
    return news_lines

# ==========================================
# 3. 获取国际宏观大宗数据
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
# 4. 昨日美股板块表现 → A股联动封禁
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
# 5. 个股新闻抓取
# ==========================================
def get_stock_news(ticker_code: str, ticker_name: str, max_items: int = 5) -> list[str]:
    news_entries = []
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
            try:
                notice_dt = datetime.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
                if (get_bj_time() - notice_dt).days > 3:
                    continue
                tag = _get_stock_news_time_tag(notice_dt)
                if tag is None:
                    continue
            except Exception:
                tag = "[📑前日]"
            if title:
                news_entries.append((notice_dt, f"{tag}[东财公告][{date}] {title}"))
    except Exception:
        pass

    # Yahoo Finance 新闻（A股用后缀）
    try:
        if ticker_code.upper().endswith('.SH'):
            yahoo_ticker = code + '.SS'
        else:
            yahoo_ticker = code + '.SZ'
        cutoff_ts = time.time() - 3 * 86400
        raw = yf.Ticker(yahoo_ticker).news or []
        for item in raw:
            pub_ts = item.get('providerPublishTime', 0)
            if pub_ts < cutoff_ts:
                continue
            title     = str(item.get('title', '')).strip()
            publisher = str(item.get('publisher', 'Yahoo'))
            pub_dt    = datetime.datetime.fromtimestamp(pub_ts, tz=BEIJING_TZ)
            tag       = _get_stock_news_time_tag(pub_dt)
            if tag is None:
                continue
            date_str  = pub_dt.strftime('%m-%d %H:%M')
            if title:
                news_entries.append((pub_dt, f"{tag}[Yahoo/{publisher}][{date_str}] {title}"))
    except Exception:
        pass

    # 新浪财经新闻
    try:
        sina_url = (f"https://feed.mix.sina.com.cn/api/roll/get"
                    f"?pageid=153&lid=2512&k={code}&num={max_items}&page=1")
        req = urllib.request.Request(sina_url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=6) as resp:
            sina_content = resp.read().decode('utf-8')
        sina_data = json.loads(sina_content)
        _JUNK_KEYWORDS = ('盘口', '亚盘', '竞彩', '让球', '胜负彩', '比分',
                          '欧冠', '英超', '西甲', '中超', 'NBA', 'CBA', '足彩', '首发阵容', '让分')
        for item in sina_data.get('result', {}).get('data', []):
            title = str(item.get('title', '')).strip()
            ctime = str(item.get('ctime', ''))[:10]
            try:
                if ctime.isdigit() and len(ctime) == 10:
                    ctime_dt = datetime.datetime.fromtimestamp(int(ctime), tz=BEIJING_TZ)
                else:
                    ctime_dt = datetime.datetime.strptime(ctime, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
                if (get_bj_time() - ctime_dt).days > 3:
                    continue
                tag = _get_stock_news_time_tag(ctime_dt)
                if tag is None:
                    continue
            except Exception:
                tag = "[📑前日]"
            media = str(item.get('media_name', '新浪财经'))
            is_relevant = bool(title) and (code in title or ticker_name[:2] in title)
            is_junk = any(kw in title for kw in _JUNK_KEYWORDS)
            if is_relevant and not is_junk:
                news_entries.append((ctime_dt, f"{tag}[新浪/{media}][{ctime}] {title}"))
    except Exception:
        pass

    news_entries.sort(key=lambda x: x[0], reverse=True)
    return [entry[1] for entry in news_entries[:max_items]]

def _get_stock_news_time_tag(dt_obj):
    if dt_obj is None:
        return "[📑前日]"
    now_bj = get_bj_time()
    delta = now_bj - dt_obj
    hours = delta.total_seconds() / 3600
    if hours <= 6:
        return "[🔥最新]"
    elif delta.days == 0 or (delta.days == 1 and hours <= 24):
        return "[📰今日]"
    elif delta.days <= 2 or (delta.days == 2 and hours <= 48):
        return "[📄昨日]"
    elif delta.days <= 3 or (delta.days == 3 and hours <= 72):
        return "[📑前日]"
    else:
        return None

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
# 6. 定向计算技术指标
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
        ("MACD_V型反转", False), ("周线MACD_V型反转", False),
        ("周线共振", False),
        ("KDJ_J", 50.0), ("KDJ_J回升", False), ("KDJ_J超卖", False),
        ("量能放大", False), ("量比", 1.0), ("看涨形态", []),
        ("ATR", 0.0), ("ATR_Pct", 5.0),
    ]

    for code in list(full_pool.keys()):
        weekly_bullish = False
        weekly_macd_rising = False
        weekly_macd_v_reverse = False
        if not df_weekly.empty and code in df_weekly['ts_code'].values:
            wk = df_weekly[df_weekly['ts_code'] == code].sort_values('trade_date')
            if len(wk) >= 12:
                wc     = wk['close'].values.astype(float)
                wma5   = float(pd.Series(wc).rolling(5).mean().iloc[-1])
                wma10  = float(pd.Series(wc).rolling(10).mean().iloc[-1])
                w_exp1 = pd.Series(wc).ewm(span=12, adjust=False).mean()
                w_exp2 = pd.Series(wc).ewm(span=26, adjust=False).mean()
                w_hist = (w_exp1 - w_exp2 - (w_exp1 - w_exp2).ewm(span=9, adjust=False).mean()) * 2
                weekly_macd_rising = float(w_hist.iloc[-1]) > float(w_hist.iloc[-2])
                w_hist_last  = float(w_hist.iloc[-1])
                w_hist_prev  = float(w_hist.iloc[-2])
                w_hist_prev2 = float(w_hist.iloc[-3]) if len(w_hist) >= 3 else w_hist_prev
                weekly_macd_v_reverse = (len(w_hist) >= 3) and (w_hist_prev2 > w_hist_prev) and (w_hist_prev < w_hist_last)
                weekly_bullish = bool(wma5 > wma10 and weekly_macd_rising)
        full_pool[code]["周线共振"] = weekly_bullish
        full_pool[code]["周线MACD上升"] = weekly_macd_rising
        full_pool[code]["周线MACD_V型反转"] = weekly_macd_v_reverse
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
        full_pool[code]["MACD_V型反转"] = bool(len(hist) >= 3 and h_prev2 > h_prev and h_prev < h_last)

        full_pool[code]["日线MACD上升"]   = h_last > h_prev
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
# 7. AI 事件推演选股（含联动警告）
# ==========================================
def check_period_resonance(stock):
    daily_rising = stock.get("日线MACD上升", False)
    weekly_rising = stock.get("周线MACD上升", False)
    if not daily_rising or not weekly_rising:
        return False, []
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
        is_resonance, resonance_patterns = check_period_resonance(stock)
        stock["周期共振"] = is_resonance
        stock["共振形态"] = resonance_patterns

        h_last = stock.get("MACD_HIST_LAST", 0)
        if stock.get("MACD金叉"):
            if h_last < -0.5:
                tech_score += 20
                tech_reasons.append(f"MACD零轴下金叉底背离({h_last:.2f})(+20)")
            elif abs(h_last) <= 0.5:
                tech_score += 15
                tech_reasons.append(f"MACD零轴附近金叉({h_last:.2f})(+15)")
            else:
                tech_score += 5
                tech_reasons.append(f"⚠️MACD高位金叉({h_last:.2f})(+5)")
        elif stock.get("MACD绿柱缩短"):
            h_prev = stock.get("MACD_HIST_PREV", 0)
            pts = 12 if (h_last < 0 and abs(h_last) < abs(h_prev) * 0.85) else 8
            tech_score += pts
            tech_reasons.append(f"MACD绿柱收敛({h_last:.2f})(+{pts})")
        elif stock.get("MACD趋势") == "走强" and h_last > 0:
            if h_last > 3:
                tech_score += 2
                tech_reasons.append(f"⚠️MACD红柱高位({h_last:.2f})(+2)")
            else:
                tech_score += 4
                tech_reasons.append(f"MACD红柱走强({h_last:.2f})(+4)")
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

        if stock.get("MACD_V型反转", False):
            tech_score += 8
            tech_reasons.append("MACD柱线V型反转(+8)")
        if stock.get("周线MACD_V型反转", False):
            tech_score += 8
            tech_reasons.append("周线MACD柱线V型反转(+8)")

        stock["技术评分"] = min(tech_score, 40)

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

    # 【新增】读取昨日止损联动警告
    stop_loss_warning = get_stop_loss_hit_warning()

    prompt = f'''
    你是顶级A股事件驱动型游资操盘手，擅长从全球宏观事件、美债大宗异动推演底层传导链条，并结合个股新闻做三重交叉验证。

    今天是{today_str}。

    {removed_notice}

    {stop_loss_warning}

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

    【新闻时效权重规则——必须严格遵守】：
    每条宏观新闻和个股新闻已自动打上时效标签，你必须根据标签调整消息面评分权重：
    - 宏观新闻：
      · [🔥今日最新-权重最高] ≤6小时 → 消息面评分可给满分25分，可作为主线核心依据
      · [📰今日-高权重] ≤24小时 → 给80%权重（20分），可作为主线核心依据
      · [📄昨日-中等权重] ≤48小时 → 给50%权重（12分），需确认对应板块未大涨才能用
      · [📑前日-低权重] ≤72小时 → 给20%权重（5分），仅辅助参考，绝对不能当主线
    - 个股新闻：
      · [🔥最新] ≤6小时 → 消息面评分可给满分
      · [📰今日] ≤24小时 → 给80%权重
      · [📄昨日] ≤48小时 → 给50%权重，需确认个股未大涨
      · [📑前日] ≤72小时 → 给20%权重，仅辅助参考
    - 超过72小时的新闻已被系统自动丢弃，不会出现在输入中。
    - 严禁将[📑前日-低权重]的新闻作为主线推荐依据，例如"上周的疫苗新闻"只能给5分消息分，不得排进Top1-5主线。
    - 今天早上刚出的重大新闻（如黄金暴涨、政策发布）是[🔥今日最新-权重最高]，应给满分25分，优先排进主线。

    【核心工作流程】（基于回测验证，严格执行）：
    第一步（新闻定方向）：从宏观新闻中提炼出今日1-2条最强产业链主线。必须优先使用[🔥今日最新]和[📰今日]标签的新闻作为主线依据，[📄昨日]新闻仅作为辅助验证，[📑前日]新闻不得作为主线依据。
    第二步（板块锁定）：只在你提炼出的主线板块中寻找标的。严禁跳出主线去买"技术面好但没新闻"的票。
    第三步（技术选个股）：在主线板块内，**必须优先选择【周期共振】为 True 的标的**（即代码已自动标记满足：日线MACD↑ + 周线MACD↑ + 看涨吞没/启明星/刺穿线/锤子线）。
    第四步（评分确认）：如果存在周期共振标的，直接将其排入Top1-5，除非该标的有重大负面新闻。若无共振标的，再退而求其次选择技术评分≥20的票，但须在报告中明确警示"无共振信号"。
    第五步（新闻权重校验）：对每只入选Top1-5的标的，检查其个股新闻的时效标签。如果主要利好来自[📑前日]或[📄昨日]且个股已大涨，必须降级至观察池或排除。

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

    try:
        ai_html = ""
        with client.messages.stream(
            model=TARGET_MODEL,
            max_tokens=80000,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text in stream.text_stream:
                ai_html += text
        ai_html = ai_html.strip()
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

    core_cards = [clean_fragment(c) for c in re.split(r'(?=<div[^>]*class="[^"]*core-card[^"]*")', core_zone_raw) if 'core-card' in c]
    obs_items = [clean_fragment(c) for c in re.split(r'(?=<div[^>]*class="[^"]*obs-card[^"]*")', obs_zone_raw) if c.strip().startswith("<li>")]
    trap_items = [clean_fragment(c) for c in re.split(r'(?=<div[^>]*class="[^"]*trap-card[^"]*")', trap_zone_raw) if c.strip().startswith("<li>")]

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
        item['Open_Price'] = item.get('Open', item['Close'])
        item['ATR_Pct'] = item.get('ATR_Pct', '')
        item['周期共振'] = item.get('周期共振', False)
        chosen.append(item)

    return chosen

# ==========================================
# 主程序入口
# ==========================================
if __name__ == "__main__":
    macro_news = get_free_macro_news()
    macro_data_text = get_global_macro_data()
    latest_price_map = get_latest_price_map()

    removed_tickers_macro = pre_scan_portfolio_review(macro_news, macro_data_text, latest_price_map)
    # 规则卖出信号已废弃，由 review.py 统一处理
    rule_sell_signals, removed_tickers_rule = check_rule_based_sell_signals(
        latest_price_map, exclude_tickers=removed_tickers_macro
    )

    removed_tickers = removed_tickers_macro + removed_tickers_rule

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

    # 注入卡片（已废弃，但保留空壳）
    sell_signal_card_html = build_sell_signal_card(removed_tickers_macro, rule_sell_signals)
    current_holdings_card_html = build_current_holdings_card(latest_price_map)
    insertion_point = ai_report_html.find('<div class="market-section">')
    if insertion_point != -1:
        ai_report_html = ai_report_html[:insertion_point] + sell_signal_card_html + current_holdings_card_html + ai_report_html[insertion_point:]
    else:
        ai_report_html = sell_signal_card_html + current_holdings_card_html + ai_report_html

    chosen = match_pool_to_report(pool_with_news, ai_report_html, DEFAULT_STOP_LOSS_PCT)

    # ============================================================
    # 【修复】生成 pending 文件，由 review.py 盘后补充
    # ============================================================
    if chosen:
        # 过滤已在持仓中的标的
        if os.path.exists("trade_history.csv"):
            try:
                df_old = pd.read_csv("trade_history.csv", keep_default_na=False)
                _active_tags = {'Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon'}
                _active_tickers = set(df_old[df_old['Tag'].isin(_active_tags)]['Ticker'].unique())
                _before_filter = len(chosen)
                chosen = [item for item in chosen if item['Ticker'] not in _active_tickers]
                _skipped = _before_filter - len(chosen)
                if _skipped > 0:
                    print(f"📋 跳过 {_skipped} 只已在持仓中的标的，不重复追加")
            except Exception as e:
                print(f"⚠️ 持仓去重过滤失败: {e}")

        if chosen:
            pending_file = f"ashare_stocks_pending_{get_bj_time().strftime('%Y%m%d')}.csv"

            # 准备待确认数据
            pending_cols = ['Ticker', 'Name', 'Industry', 'Tag', 'Amount', 'Daily_Pct', 'Hold_Period', 'Stop_Loss', 'Score', 'ATR_Pct', '周期共振']
            df_pending = pd.DataFrame(chosen)

            # 确保列存在
            for c in pending_cols:
                if c not in df_pending.columns:
                    df_pending[c] = ''
            df_pending = df_pending[pending_cols]

            # 添加 Scan_Ref_Price（盘前参考价）
            if 'Open_Price' in pd.DataFrame(chosen).columns:
                df_pending['Scan_Ref_Price'] = pd.DataFrame(chosen)['Open_Price']
            elif 'Close' in pd.DataFrame(chosen).columns:
                df_pending['Scan_Ref_Price'] = pd.DataFrame(chosen)['Close']
            else:
                df_pending['Scan_Ref_Price'] = 0

            # 强制字符串类型
            for col in df_pending.columns:
                df_pending[col] = df_pending[col].astype(str)

            # 写入 pending 文件
            df_pending.to_csv(pending_file, index=False, encoding='utf-8')
            print(f"✅ 已生成 {len(df_pending)} 条A股待确认记录（{pending_file}）")
            print(f"⏳ 盘后 review.py 将补充开盘价/收盘价后写入 trade_history.csv")
        else:
            print("📋 今日无新入选标的（全部已在持仓中），跳过写账。")
    else:
        print("⚠️ match_pool_to_report 未解析出任何标的，跳过写账。")

    final_email_html = build_email(ai_report_html)
    send_emails(final_email_html)
    print("🎉 今日早盘扫描与逻辑推演全部完成！")
