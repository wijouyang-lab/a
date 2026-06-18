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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic
import time

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

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

ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()


# ==========================================
# 1. 获取交易额 Top 300
# ==========================================
def get_top_300_pool():
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    print(f"🔍 [阶段1] 正在拉取 {trade_date} 的A股全市场数据，圈定 Top 300 主力资金池...")

    df_daily = pro.daily(trade_date=trade_date)
    if df_daily is None or df_daily.empty:
        trade_date = (get_bj_time() - datetime.timedelta(days=2)).strftime('%Y%m%d')
        print(f"   昨日数据为空，尝试 {trade_date}...")
        df_daily = pro.daily(trade_date=trade_date)
        if df_daily is None or df_daily.empty:
            print("🚨 数据拉取失败，返回空池。")
            return {}, []

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

    print(f"✅ 成功圈定 {len(full_pool)} 只核心活跃标的。")
    return full_pool, codes


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
# 3. 个股新闻抓取（保留原有逻辑，未改动）
# ==========================================
def get_stock_news(ticker_name, max_items=3):
    headlines = []
    try:
        pass
    except Exception:
        pass
    return headlines


def enrich_pool_with_news(pool_data):
    """
    为 Top 100 标的补充个股新闻。
    使用新浪财经 RSS，按股票名称关键词过滤。
    """
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
# 4. 定向计算技术指标（分批抓取 + 免死金牌，未改动）
# ==========================================
def calc_tech_indicators(full_pool, codes):
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
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

            # 免死金牌
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
# 5. Claude 事件逻辑推演选股（Top1-5详细分析 + 1-100评分）
# ==========================================
def generate_ai_report(pool_data, macro_news_text):
    print("🧠 [阶段4] 召唤 AI 大脑（事件→产业链→个股新闻三重交叉验证，Top5详细分析）...")
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

    prompt = f'''
你是顶级A股事件驱动型游资操盘手，擅长从宏观事件推演产业链受益逻辑，并结合个股新闻做三重交叉验证。

今天是{today_str}。

【今日全球宏观与A股消息面】：
{macro_news_text}

【今日A股交易额 Top 100（含个股最新新闻）】：
{json.dumps(compact_pool, ensure_ascii=False)}

字段说明：
- 今日涨跌(%)：今日市场情绪，已涨超8%的票消息可能被充分消化
- 乖离率(%)：偏离20日均线，>20%视为短期极度透支
- RSI：>85为极度超买危险区
- MACD：走强/走弱，辅助判断动能
- 个股新闻：该股票最近的相关新闻标题，无此字段表示暂无最新消息

【你的核心工作流程】：

━━━━━━━━━━━━━━━━━━━━━━
第一步：宏观事件识别与产业链推演
━━━━━━━━━━━━━━━━━━━━━━
仔细阅读上方所有宏观新闻，识别出今日最重要的2-3个事件。
对每个事件做完整的产业链推演，例如：

事件：中国限制钨粉出口
→ 逻辑链：中国是全球最大钨资源国 → 出口限制导致全球钨粉供应收紧
→ 直接受益：中国国内钨矿开采和冶炼企业（拥有资源定价权）
→ 间接受益：六氟化钨（芯片制造原料）出口企业
→ 在池子中寻找钨矿、钨加工、六氟化钨相关企业

━━━━━━━━━━━━━━━━━━━━━━
第二步：个股新闻交叉验证（关键步骤）
━━━━━━━━━━━━━━━━━━━━━━
对每只候选标的，必须检查其个股新闻字段：

✅ 加分情形（优先推荐）：
- 个股新闻与宏观主线高度吻合（如宏观是"半导体政策"，个股新闻也提到该公司获得订单/政策支持）
- 个股新闻显示公司有最新业绩预喜、重大合同、股权激励
- 个股新闻显示主力资金连续流入、机构调研

⚠️ 中性情形（正常分析）：
- 暂无个股新闻（纯靠宏观逻辑推演）：需在报告中注明"无最新个股消息，纯逻辑推演"

❌ 减分/排除情形（必须说明）：
- 个股新闻显示负面消息：监管调查、业绩预亏、大股东减持、核心高管离职
- 即使宏观逻辑再好，有以上负面新闻的票必须降级到观察池或受损组
- 个股新闻与宏观主线矛盾（如宏观利好AI，但该AI股个股新闻显示订单取消）

━━━━━━━━━━━━━━━━━━━━━━
第三步：技术面风控兜底
━━━━━━━━━━━━━━━━━━━━━━
乖离率>20% 且 RSI>85 才列入受损组（技术极度透支）。
其他技术状况不影响选股，但用于设定合理止损位。

━━━━━━━━━━━━━━━━━━━━━━
第四步：推荐评分（1-100分，核心要求）
━━━━━━━━━━━━━━━━━━━━━━
对每一只进入【核心精选】（Top 1-5）的标的，必须给出一个1-100的综合评分，评分依据：
- 事件逻辑链是否完整、直接（直接受益方通常80分以上，三四手受益方应低于60分）
- 个股新闻是否强力佐证（有正面新闻共振+10~15分，有负面新闻应直接降到观察池甚至不评分）
- 资金验证是否充分（巨量+明显异动应加分，温和放量应正常评分）
- 技术面是否健康（极度超买应扣分，正常区间不扣分）
评分必须客观区分质量差异，禁止5只全部给相近分数（如全部85分左右），必须体现你对不同标的确信程度的真实差异。

━━━━━━━━━━━━━━━━━━━━━━
第五步：输出详细报告
━━━━━━━━━━━━━━━━━━━━━━
【硬性纪律】：
1. 【核心精选】Top 1-5 每只都必须按完整模板逐项写满，不能因为排名靠后而简化，5只的详细程度必须一致。
2. 每只推荐必须写完整逻辑链 + 个股新闻验证结论 + 评分理由，三者缺一不可。
3. 同一只股票绝对不能重复出现。
4. 风控底线格式：周期:[X-Y天] | 止损:[XX.XX元]（止损必须是具体价格加"元"）。
5. 评分格式必须严格为：评分:[XX]/100（XX是1-100的整数，必须用这个精确格式，不要写成"XX分"或"XX/100分"等变体）。
6. 如果今日新闻中找不到足够强的事件逻辑，宁可少选（哪怕只有3只进入核心精选），不要凑数推荐到5只。
7. 严格按以下HTML骨架输出，不加markdown外框：

<div class="header-card">
    <h2>🌍 今日事件逻辑推演中心</h2>
    <p><b>执行时间：</b>{today_str} 盘前</p>
    <div style="background:#fff3e0;border-left:4px solid #ff9800;padding:15px;margin-top:10px;border-radius:4px;">
        <b>📋 今日核心事件与完整逻辑链：</b>
        <p><b>事件1：</b>[事件标题] → [完整推演：为什么这个事件利好/利空哪个产业链，受益逻辑是什么，预计持续多久]</p>
        <p><b>事件2：</b>[事件标题] → [完整推演]</p>
        <p><b>受损预警：</b>[哪些行业/标的因今日事件受损，需回避，说明传导机制]</p>
    </div>
</div>

<div class="market-section">
    <div class="market-title">🇨🇳 [核心精选] A股事件驱动 Top 1-5 详细分析</div>

    <div class="card core-card">
        <h3>[核心精选] 1. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>[具体事件] → [产业链传导机制，2-3句话说清楚] → [该企业为什么是直接受益方，说明公司在产业链中的具体位置和核心竞争力]</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>[列出该股相关个股新闻标题，说明是否与宏观主线形成共振；若无新闻则注明"暂无最新个股消息，纯宏观逻辑推演"]</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>今日交易额位于巨量核心池，涨跌[X]%，[分析资金行为：是主力吸筹、机构建仓还是散户追涨]</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>乖离率[X]%，RSI[X]，MACD[走强/走弱]，[给出技术面综合判断：当前位置是否安全，有无极度超买风险]</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — [一句话说明评分理由：逻辑链是否直接、新闻是否强力佐证、资金是否充分验证]</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | [说明止损价设定依据：基于哪个支撑位或均线]</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 2. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 3. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 4. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card core-card">
        <h3>[核心精选] 5. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 宏观事件逻辑链：</span>(同等详细程度)</p>
        <p><span class="tag bg-purple">📰 个股新闻验证：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-teal">⭐ 推荐评分：</span>评分:[XX]/100 — (...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元] | (...)</p>
    </div>

    <div class="card obs-card">
        <h3>[观察池] ⚠️ 逻辑待确认或个股新闻有瑕疵 (Rank 6-10)</h3>
        <ul>
            <li><b>6. [名称] ([代码]) | [行业]：</b>[说明逻辑链较弱、事件尚未确认、或个股新闻有负面信号的具体原因] <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
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
        <li><b>[名称] ([代码]) | <span class="bear-text">逻辑受损/新闻预警</span></b><br>❌ 受损逻辑：[具体说明是宏观事件传导受损，还是个股新闻有负面信号，传导链是什么]<br>⚠️ 回避理由：[说明风险持续时间和潜在下跌空间]</li>
        <li><b>[名称] ([代码]) | <span class="bear-text">逻辑受损/新闻预警</span></b><br>❌ 受损逻辑：...<br>⚠️ 回避理由：...</li>
    </ul>
</div>
'''

    ai_html = ""
    with client.messages.stream(
        model=TARGET_MODEL,
        max_tokens=8000,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            ai_html += text

    print("✅ AI 事件逻辑推演报告生成完毕")
    return ai_html.replace("```html", "").replace("```", "").strip()


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
        .bg-green{background:#37474f}
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
    msg['Subject'], msg['From'] = "【事件驱动】A股逻辑推演精选(Top5详细+评分)", f"Alpha Radar <{acc}>"
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


if __name__ == "__main__":
    full_pool, codes = get_top_300_pool()

    if full_pool:
        macro_news = get_free_macro_news()
        final_pool = calc_tech_indicators(full_pool, codes)

        if len(final_pool) < 10:
            print("🚨 触发安全熔断：清洗后有效标的不足10只，终止 AI 调用。")
            import sys; sys.exit(0)

        final_pool = enrich_pool_with_news(final_pool)

        ai_html = generate_ai_report(final_pool, macro_news)
        full_html = build_email(ai_html)

        chosen = []
        clean_html = re.sub(r'<[^>]+>', ' ', ai_html)
        clean_html = re.sub(r'\s+', ' ', clean_html)

        for item in final_pool:
            ticker_str = str(item['Name'])
            idx = clean_html.find(ticker_str)
            if idx == -1:
                continue

            chunk = clean_html[idx:idx+800]
            tag = None
            context = clean_html[max(0, idx-300):idx] + chunk[:200]

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
                stop_loss = sl_match.group(1).strip() if sl_match else f"{round(item['Close'] * (1 + DEFAULT_STOP_LOSS_PCT / 100), 2)}元"
                score_match = re.search(r'评分\s*[:：]\s*\[?(\d{1,3})\s*/\s*100', chunk)
                score = score_match.group(1).strip() if score_match else "N/A"

            item['Tag'] = tag
            item['Hold_Period'] = hold_period
            item['Stop_Loss'] = stop_loss
            item['Score'] = score
            item['Daily_Pct'] = item.get('pct_chg', 0)
            chosen.append(item)

        # ==========================================
        # 写入 trade_history.csv（含 Score 列自动迁移）
        # ==========================================
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
                print("⚠️ 检测到旧版trade_history.csv缺少Score列，已自动升级表头（历史行Score将显示为空，不影响读取）")

        with open(log_file, "a", encoding="utf-8") as f:
            if need_header:
                f.write(new_header)
            ts_date = get_bj_time().strftime('%Y-%m-%d')
            for i in chosen:
                f.write(f"{ts_date},{i['Ticker']},{i['Name']},{i['Tag']},{i.get('Industry','未知')},{i['Close']},{i['Amount']},{i['Daily_Pct']},{i['Hold_Period']},{i['Stop_Loss']},{i.get('Score','N/A')}\n")

        print(f"✅ 共安全记账 {len(chosen)} 条核心数据。")
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(full_html)
        send_emails(full_html)
    else:
        print("⚠️ 数据池为空，跳过执行。")
