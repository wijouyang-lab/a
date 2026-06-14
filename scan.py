# 自动进化版本 | 时间: 2026-06-13 02:03 | 触发胜率: 21.4%

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

TARGET_MODEL = 'claude-fable-5'

# ===== 核心参数（优化后） =====
# 止损参数
STOP_LOSS_CORE = -4.0        # 核心双龙止损 (原-5%)
STOP_LOSS_SUB = -3.0         # 梯队先锋止损 (原-5%)
DEFAULT_STOP_LOSS_PCT = -3.5  # 默认止损 (原-5%)

# 技术面硬性门槛（代码层预筛选）
MAX_BIAS_ALLOW = 10.0        # 乖离率绝对上限 (原12%)
MAX_RSI_ALLOW = 68.0         # RSI绝对上限 (原75)
MAX_BIAS_CORE = 8.0          # 核心推荐乖离率上限
MAX_RSI_CORE = 65.0          # 核心推荐RSI上限
MAX_BIAS_SUB = 10.0          # 梯队推荐乖离率上限
MAX_RSI_SUB = 68.0           # 梯队推荐RSI上限

# 安全评分最低门槛
MIN_SAFETY_SCORE = 60        # 低于60分不进入AI推荐池

# 量比阈值
VOL_RATIO_MIN = 0.8          # 量比下限，低于此为缩量衰退
VOL_RATIO_STRONG = 1.3       # 量比强势确认

ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()


def get_latest_macro_news():
    print("正在抓取 CNBC/Reuters 英文财经快讯...")
    sources = [
        ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
        ("Reuters", "https://feeds.reuters.com/reuters/businessNews"),
        ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ]
    news_lines = []
    for source_name, url in sources:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
            root = ET.fromstring(xml_data)
            items = root.findall('.//item')[:5]
            for item in items:
                title = item.find('title')
                pub_date = item.find('pubDate')
                if title is not None:
                    time_str = pub_date.text[:16] if pub_date is not None else ""
                    news_lines.append(f"[{source_name}] {time_str} - {title.text}")
        except Exception as e:
            print(f"⚠️ {source_name} 抓取失败: {e}")

    if news_lines:
        print(f"✅ 成功抓取 {len(news_lines)} 条财经快讯")
        return "\n".join(news_lines)
    return "暂无实时财经新闻，请基于昨收盘及底层产业逻辑进行推演。"


def calculate_safety_score(stock_info):
    """
    五维安全评分系统 (0-100分)
    维度：乖离率(25分)、RSI(25分)、MACD(20分)、均线排列(15分)、量比(15分)
    """
    score = 0

    # 1. 乖离率评分 (25分) - 越低越安全
    bias = stock_info.get("乖离率(%)", 0)
    if bias <= 3:
        score += 25
    elif bias <= 5:
        score += 20
    elif bias <= 8:
        score += 15
    elif bias <= 10:
        score += 8
    else:
        score += 0

    # 2. RSI评分 (25分) - 40-60最健康
    rsi = stock_info.get("RSI", 50)
    if 40 <= rsi <= 55:
        score += 25
    elif 55 < rsi <= 60:
        score += 20
    elif 60 < rsi <= 65:
        score += 15
    elif 65 < rsi <= 68:
        score += 8
    elif rsi > 68:
        score += 0
    elif 30 <= rsi < 40:
        score += 15  # 超卖可能反弹
    else:
        score += 5

    # 3. MACD评分 (20分)
    macd_trend = stock_info.get("MACD趋势", "走弱")
    macd_hist_val = stock_info.get("MACD柱值", 0)
    macd_above_zero = stock_info.get("MACD零轴上方", False)
    macd_golden = stock_info.get("MACD金叉", False)

    if macd_trend == "走强" and macd_above_zero:
        score += 20
    elif macd_trend == "走强" and macd_golden:
        score += 18
    elif macd_trend == "走强":
        score += 14
    elif macd_trend == "走弱" and macd_above_zero:
        score += 8
    else:
        score += 0

    # 4. 均线排列评分 (15分)
    ma_alignment = stock_info.get("均线多头", False)
    price_above_ma5 = stock_info.get("价格站上MA5", False)

    if ma_alignment and price_above_ma5:
        score += 15
    elif ma_alignment:
        score += 12
    elif price_above_ma5:
        score += 8
    else:
        score += 0

    # 5. 量比评分 (15分)
    vol_ratio = stock_info.get("量比", 1.0)
    if VOL_RATIO_STRONG <= vol_ratio <= 2.5:
        score += 15
    elif 1.0 <= vol_ratio < VOL_RATIO_STRONG:
        score += 12
    elif VOL_RATIO_MIN <= vol_ratio < 1.0:
        score += 8
    elif vol_ratio > 2.5:
        score += 5  # 过度放量可能是出货
    else:
        score += 0

    return score


def get_a_share_data():
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    print(f"正在拉取 {trade_date} 的A股主力资金活跃数据...")

    df_daily = pro.daily(trade_date=trade_date)
    if df_daily is None or df_daily.empty:
        trade_date = (get_bj_time() - datetime.timedelta(days=2)).strftime('%Y%m%d')
        print(f"昨日数据为空，尝试 {trade_date}...")
        df_daily = pro.daily(trade_date=trade_date)
        if df_daily is None or df_daily.empty:
            print("数据拉取失败，返回空。")
            return []

    basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
    name_map = dict(zip(basic['ts_code'], basic['name']))
    industry_map = dict(zip(basic['ts_code'], basic.get('industry', ['核心资产'] * len(basic))))

    df_sorted = df_daily.sort_values(by='amount', ascending=False).head(80)
    codes = [row['ts_code'] for _, row in df_sorted.iterrows()]

    full_pool = {}
    forows():
        ts_code = row['ts_code']
        full_pool[ts_code] = {
            "Ticker": ts_code,
            "Name": name_map.get(ts_code, ts_code),
            "Industry": industry_map.get(ts_code, "未知"),
            "Close": row['close'],
            "Amount": row['amount'],
            "pct_chg": row.get('pct_chg', 0),
        }

    try:
        start_hist = (get_bj_time() - datetime.timedelta(days=120)).strftime('%Y%m%d')
        df_hist = pro.daily(ts_code=",".join(codes), start_date=start_hist, end_date=trade_date).sort_values(['ts_code', 'trade_date'])

        for code in list(full_pool.keys()):
            stock_data = df_hist[df_hist['ts_code'] == code].copy()
            if len(stock_data) >= 30:
                close_px = stock_data['close']
                vol_series = stock_data['vol']
                current_close = full_pool[code]["Close"]

                # MA计算
                ma5 = close_px.rolling(window=5).mean().iloc[-1]
                ma10 = close_px.rolling(window=10).mean().iloc[-1]
                ma20 = close_px.rolling(window=20).mean().iloc[-1]

                # 乖离率
                bias = round(((current_close - ma20) / ma20) * 100, 2)
                full_pool[code]["乖离率(%)"] = bias

                # 均线多头排列判断
                ma_bullish = (ma5 > ma10 > ma20)
                full_pool[code]["均线多头"] = ma_bullish
                full_pool[code]["价格站上MA5"] = (current_close >= ma5 * 0.99)

                # MACD计算（增强版）
                exp1 = close_px.ewm(span=12, adjust=False).mean()
                exp2 = close_px.ewm(span=26, adjust=False).mean()
                macd_line = exp1 - exp2
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                macd_hist = (macd_line - signal_line) * 2

                macd_hist_current = macd_hist.iloc[-1]
                macd_hist_prev = macd_hist.iloc[-2]
                macd_line_current = macd_line.iloc[-1]

                full_pool[code]["MACD趋势"] = "走强" if macd_hist_current > macd_hist_prev else "走弱"
                full_pool[code]["MACD柱值"] = round(macd_hist_current, 3)
                full_pool[code]["MACD零轴上方"] = (macd_line_current > 0)
                # 金叉：MACD柱由负转正，或前天为负今天为正
                full_pool[code]["MACD金叉"] = (macd_hist_current > 0 and macd_hist_prev <= 0) or \
                                              (macd_hist_current > macd_hist_prev and macd_hist_prev > macd_hist.iloc[-3])

                # RSI计算
                delta = close_px.diff()
                gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
                loss = (-1 * delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
                rs = gain / loss
                rsi_val = round((100 - (100 / (1 + rs))).iloc[-1], 2)
                full_pool[code]["RSI"] = rsi_val

                # 量比计算：近5日均量 / 近20日均量
                vol_ma5 = vol_series.rolling(window=5).mean().iloc[-1]
                vol_ma20 = vol_series.rolling(window=20).mean().iloc[-1]
                vol_ratio = round(vol_ma5 / vol_ma20, 2) if vol_ma20 > 0 else 1.0
                full_pool[code]["量比"] = vol_ratio

                # ===== 硬性预筛选：代码层直接淘汰 =====
                # 乖离率超限直接排除
                if bias > MAX_BIAS_ALLOW:
                    full_pool[code]["_eliminated"] = True
                    full_pool[code]["_reason"] = f"乖离率{bias}%超限(>{MAX_BIAS_ALLOW}%)"
                    continue
                # RSI超限直接排除
                if rsi_val > MAX_RSI_ALLOW:
                    full_pool[code]["_eliminated"] = True
                    full_pool[code]["_reason"] = f"RSI={rsi_val}超买(>{MAX_RSI_ALLOW})"
                    continue
                # MACD走弱+乖离偏高 排除
                if full_pool[code]["MACD趋势"] == "走弱" and bias > 5:
                    full_pool[code]["_eliminated"] = T]["_reason"] = f"MACD走弱且乖离率{bias}%偏高"
                    continue
                # 缩量衰退排除
                if vol_ratio < VOL_RATIO_MIN:
                    full_pool[code]["_eliminated"ol[code]["_reason"] = f"量比{vol_ratio}严重缩量(<{VOL_RATIO_MIN})"
                    continue

                full_pool[code]["_eliminated"] = False

            else:
                full_pool[c   full_pool[code]["_reason"] = "历史数据不足30天"

    except Exception as e:
        print(f"⚠️ 指标拉取受限: {e}")

    # 计算安全评分并排序
    qualified_pool = []
    eliminated_pool = []

    for code, info in full_pool.items():
        if info.get("_eliminated", True):
            eliminated_pool.append(info)
            continue

        safety_score = calculate_safety_score(info)
        info["安全评分"] = safety_score

        if safety_score >= MIN_SAFETY_SCORE:
            qualified_pool.append(info)
        else:
            info["_reason"] = f"安全评分{safety_score}不达标(<{MIN_SAFETY_SCORE})"
            eliminated_pool.append(info)

    # 按安全评分*成交额加权排序（不再纯粹按成交额）
    qualified_pool.sort(key=lambda x: x.get("安全评分", 0) * (x.get("Amount", 0) ** 0.3), reverse=True)
    final_pool = qualified_pool[:30]

    # 从淘汰池中选出最危险的作为诱多对照组
    eliminated_pool.sort(key=lambda x: x.get("Amount", 0), reverse=True)
    trap_pool = eliminated_pool[:10]

    print(f"✅ 资金活跃池准备完毕，合格标的 {len(final_pool)} 只，淘汰 {len(eliminated_pool)} 只。")
    print(f"   安全评分分布: 最高{max([x.get('安全评分',0) for x in final_pool], default=0)}, "
          f"最低{min([x.get('安全评分',0) for x in final_pool], default=0)}, "
          f"平均{round(sum([x.get('安全评分',0) for x in final_pool])/max(len(final_pool),1), 1)}")

    return final_pool, trap_pool


def generate_ai_report(pool_data, trap_data, macro_news_text):
    print("🧠 开始调用 AI 大脑（宏观先行，技术风控，安全评分加持）...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )
    today_str = get_bj_time().strftime('%Y年%m月%d日')

    # 传入安全评分和完整技术数据
    compact_pool = [
        {
            "名称": d["Name"],
            "代码": d["Ticker"],
            "行业": d["Industry"],
            "收盘价": d["Close"],
            "涨跌幅": d.get("pct_chg", 0),
            "乖离率": d.get("乖离率(%)", "N/A"),
            "RSI": d.get("RSI", "N/A"),
            "MACD趋势": d.get("MACD趋势", "N/A"),
            "MACD柱值": d.get("MACD柱值", "N/A"),
            "MACD零轴上方": d.get("MACD零轴上方", "N/A"),
            "MACD金叉": d.get("MACD金叉", False),
            "均线多头": d.get("均线多头", False),
            "量比": d.get("量比", "N/A"),
            "安全评分": d.get("安全评分", 0),
        }
        for d in pool_data
    ]

    compact_trap = [
        {
            "名称": d["Name"],
            "代码": d["Ticker"],
            "收盘价": d["Close"],
            "乖离率": d.get("乖离率(%)", "N/A"),
            "RSI": d.get("RSI", "N/A"),
            "MACD趋势": d.get("MACD趋势", "N/A"),
            "淘汰原因": d.get("_reason", "技术面危险"),
        }
        for d in trap_data
    ]

    prompt = f'''
    你是华尔街顶级游资主力量化操盘手。你的交易哲学是：【宏观定方向，产业定主线，技术定买卖，安全评分定仓位】。
    今天是{today_str}。

    【盘前宏观与全球重大快讯（最高优先级）】：
    {macro_news_text}

    【今日通过五维安全评分筛选的合格标的池（已淘汰高风险标的）】：
    {json.dumps(compact_pool, ensure_ascii=False)}

    【已被代码层淘汰的高危标的（诱多对照组素材）】：
    {json.dumps(compact_trap, ensure_ascii=False)}

    【核心推演任务】：
    第一步（宏观选将）：深刻阅读盘前新闻，判断今日的主线逻辑。从合格池中挑出与主线最契合的标的。
    第二步（技术风控 - 极度严格）：
    - 【核心双龙】必须同时满足：安全评分≥75，乖离率<{MAX_BIAS_CORE}%，RSI<{MAX_RSI_CORE}，MACD走强或金叉，均线多头排列。
    - 【梯队先锋】必须同时满足：安全评分≥65，乖离率<{MAX_BIAS_SUB}%，RSI<{MAX_RSI_SUB}，MACD非持续走弱。
    - 如果合格池中没有完全满足核心双龙条件的标的，宁缺毋滥，将最好的两只放在梯队先锋，核心双龙空缺并说明原因。

    第三步（诱多对照组）：直接引用已淘汰标的数据，说明其技术面为何危险。

    【硬性纪律】：
    1. 同一只股票绝对不能在报告中重复出现。
    2. 核心双龙止损按收盘价×{1+STOP_LOSS_CORE/100:.3f}计算，梯队先锋止损按收盘价×{1+STOP_LOSS_SUB/100:.3f}计算。止损必须是具体价格数字加"元"。
    3. 必须引用传入的"安全评分"数据，作为推荐理由的核心依据。
    4. 严格复制以下HTML骨架并填空（不要 Markdown 外框，必须保留 emoji 和 span 标签）：

    <div class="header-card">
        <h2>🌍 全局 Alpha 情报中心</h2>
        <p><b>执行时间：</b>{today_str} 盘前</p>
        <p><b>宏观驱动：</b>(结合盘前快讯，深度穿透外围走势和地缘实况，明确指出今日应该进攻的产业主线和必须回避的雷区，不少于150字)</p>
    </div>

    <div class="market-section">
        <div class="market-title">🇨🇳 A股主战场</div>

        <div class="card core-card">
            <h3>[核心双龙] 1. [名称] ([代码]) | 安全评分:[XX]分</h3>
            <p><span class="tag bg-red">🔥 宏观驱动与逻辑:</span> (说明为什么它最契合今天的盘前宏观主线)</p>
            <p><span class="tag bg-blue">📈 技术面五维诊断:</span> 乖离率[X]% | RSI=[X] | MACD[走强/金叉] | 均线[多头/空头] | 量比[X]</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[5-12天] | 止损:[XX.XX元]</p>
        </div>
        <div class="card core-card">
            <h3>[核心双龙] 2. [名称] ([代码]) | 安全评分:[XX]分</h3>
            <p><span class="tag bg-red">🔥 宏观驱动与逻辑:</span> (说明主线契合度)</p>
            <p><span class="tag bg-blue">📈 技术面五维诊断:</span> 乖离率[X]% | RSI=[X] | MACD[走强/金叉] | 均线[多头/空头] | 量比[X]</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[5-12天] | 止损:[XX.XX元]</p>
        </div>

        <div class="card sub-card">
            <h3>[梯队先锋] 3. [名称] ([代码]) | 安全评分:[XX]分</tag bg-green">⚔️ 产业事件与资金:</span> (分析其行业催化剂)</p>
            <p><span class="tag bg-gray">📉 技术面五维诊断:</span> 乖离率[X]% | RSI=[X] | MACD[状态] | 均线[状态] | 量比[X]</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[3-7天] | 止损:[XX.XX元]</p>
        </div>
        <div class="card sub-card">
            <h3>[梯队先锋] 4. [名称] ([代码]) | 安全评分:[XX]分</h3>
            <p><span class="tag bg-green">⚔️ 产业事件与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-gray">📉 技术面五维诊断:</span> 乖离率[X]% | RSI=[X] | MACD[状态] | 均线[状态] | 量比[X]</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[3-7天] | 止损:[XX.XX元]</p>
        </dobs-card">
            <h3>[筛落组] ⚠️ 观察池诊断 (Rank 5-10)</h3>
            <ul>
                <li><b>5. [名称] ([代码]) | 安全评分:[XX]分:</b> (说明差在哪一维度) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>6. [名称] ([代码]) | 安全评分:[XX]分:</b> (说明不足) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>7. [名称] ([代码]) | 安全评分:[XX]分:</b> (说明不足) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>8. [名称] ([代码]) | 安全评分:[XX]分:</b> (说明不足) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>9. [名称] ([代码]) | 安全评分:[XX]分:</b> (说明不足) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>10. [名称] ([代码]) | 安全评分:[XX]分:</b> (说明不足) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            </ul>
        </div>
    </div>

    <div class="card trap-card">
        <h3>🚨 诱多对照组（严禁接盘 - 已被五维评分系统淘汰）</h3>
        <ul>
            <li><b>11. [名称] ([代码]) | <span class="bear-text">诊断：坚决回避</span></b><br>❌ 淘汰原因：(引用系统淘汰理由和具体数值)<br>⚠️ 致命硬伤：...</li>
            <li><b>12. [名称] ([代码]) | <span class="bear-text">诊断：坚决回避</span></b><br>❌ 淘汰原因：...<br>⚠️ 致命硬伤：...</li>
        </ul>
    </div>
    '''

    ai_html = ""
    with client.messages.stream(
        model=TARGET_MODEL,
        max_tokens=4096,
        temperature=0.2,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            ai_html += text

    print("✅ AI 宏观穿透报告生成完毕")
    return ai_html.replace("html", "").replace("", "").strip()


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
        .bg-purple{background:#d84315}
        .bg-orange{background:#e64a19}
        .bg-gray{background:#607d8b}
        .bg-green{background:#37474f}
        .bear-text{color:#d32f2f;font-weight:bold}
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
    msg['Subject'], msg['From'] = "【宏观驱动】A股雷达核心打分榜单", f"Alpha Radar <{acc}>"
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
    macro_news = get_latest_macro_news()
    result = get_a_share_data()

    if result and result[0]:
        raw_pool, trap_pool = result
        ai_html = generate_ai_report(raw_pool, trap_pool, macro_news)
        full_html = build_email(ai_html)

        chosen = []
        clean_html = re.sub(r'<[^>]+>', ' ', ai_html)
        clean_html = re.sub(r'\s+', ' ', clean_html)

        for item in raw_pool:
            ticker_str = str(item['Name'])
            idx = clean_html.find(ticker_str)
            if idx == -1:
                continue

            chunk = clean_html[idx:idx+800]
            tag = None
            context = clean_html[max(0, idx-300):idx] + chunk[:200]

            if "核心双龙" in context:
                tag = "Core_Double_Dragon"
            elif "梯队先锋"Sub_Pioneer"
            elif "筛落组" in context or "观察池" in context:
                tag = "Observation"
            elif "诱多" in context or "坚决回避" in context:
                tag = "Trap_Warning"

            if tag is None:
                continue
            if tag == "Trap_Warning":
                continue

            period_match = re.search(r'周期\s*[:：]\s*\[?(\d+[-~]\d+天|\d+天|观望)', chunk)

            if tag == "Observation":
                hold_period = "观望"
                stop_loss = "观望"
            elif tag == "Core_Double_Dragon":
                hold_period = period_match.group(1).strip() if period_match else "5-12天"
                sl_match = re.search(r'止损\s*[:：]\s*\[?(\d{1,5}\.\d{1,2}元)', chunk)
                if sl_match:
                    stop_loss = sl_match.group(1).strip()
                else:
                    stop_loss = f"{round(item['Close'] * (1 + STOP_LOSS_CORE / 100), 2)}元"
            elif tag == "Sub_Pioneer":
                hold_period = period_match.group(1).strip() if period_match else "3-7天"
                sl_match = re.search(r'止损\s*[:：]\s*\[?(\d{1,5}\.\d{1,2}元s = sl_match.group(       stop_loss = f"{round(item['Close'] * (1 + STOP_LOSS_SUB / 100), 2)}元"
            else:
                hold_period = period_match.group(1).strip() if period_match else "3-7天"
                sl_match = re.search(r'止损\s*[:：]\s*\[?(\d{1,5}\.\d{1,2}元)', chunk)
                if sl_match:
                    stop_loss = sl_match.group(1).strip()
                else:
                    stop_loss = f"{round(item['Close'] * (1 + DEFAULT_STOP_LOSS_PCT / 100), 2)}元"

            item['Tag'] = tag
            item['Hold_Period'] = hold_period
            item['Stop_Loss'] = stop_loss
            item['Daily_Pct'] = item.get('pct_chg', 0)
            item['Safety_Score'] = item.get('安全评分', 0)
            chosen.append(item)

        log_file = "trade_history.csv"
        need_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0

        with open(log_file, "a", encoding="utf-8") as f:
            if need_header:
                f.write("Date,Ticker,Name,Tag,Industry,Close_Price,Amount,Daily_Pct,Hold_Period,Stop_Loss,Safety_Score\n")
            ts_date = get_bj_time().strftime('%Y-%m-%d')
            for i in chosen:
                f.write(f"{ts_date},{i['Ticker']},{i['Name']},{i['Tag']},{i.get('Industry','未知')},{i['Close']},{i['Amount']},{i['Daily_Pct']},{i['Hold_Period']},{i['Stop_Loss']},{i.get('Safety_Score',0)}\n")

        print(f"✅ 共安全记账 {len(chosen)} 条核心数据。")
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(full_html)
        send_emails(full_html)
    else:
        print("⚠️ 数据池为空，跳过执行。")