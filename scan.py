# 自动进化版本 | 时间: 2026-06-09 | 架构: 宏观驱动为主，技术面为辅

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

print(f"当前UTC时间: {datetime.datetime.utcnow()}")
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

    df_sorted = df_daily.sort_values(by='amount', ascending=False).head(60)
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

    try:
        start_hist = (get_bj_time() - datetime.timedelta(days=120)).strftime('%Y%m%d')
        df_hist = pro.daily(ts_code=",".join(codes), start_date=start_hist, end_date=trade_date).sort_values(['ts_code', 'trade_date'])

        for code in list(full_pool.keys()):
            stock_data = df_hist[df_hist['ts_code'] == code].copy()
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
            else:
                del full_pool[code]

    except Exception as e:
        print(f"⚠️ 指标拉取受限: {e}")

    final_pool = sorted(list(full_pool.values()), key=lambda x: x.get("Amount", 0), reverse=True)[:40]
    print(f"✅ 资金活跃池准备完毕，共 {len(final_pool)} 只核心标的。")
    return final_pool


def generate_ai_report(pool_data, macro_news_text):
    print("🧠 开始调用 AI 大脑（宏观先行，技术风控）...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )
    today_str = get_bj_time().strftime('%Y年%m月%d日')

    prompt = f'''
    你是华尔街顶级游资主力量化操盘手。你的交易哲学是：【宏观定方向，产业定主线，技术定买卖】。
    今天是{today_str}。

    【🔴 盘前宏观与全球重大快讯（最高优先级）】：
    {macro_news_text}

    【💧 今日两市资金最活跃的 Top 100 标的池】（已附带行业与底层技术数据）：
    {json.dumps(pool_data, ensure_ascii=False, default=str)}

    【核心推演任务】：
    第一步（宏观选将）：深刻阅读盘前新闻，判断今日的主线逻辑。根据你推演出的【宏观主线】，从 Top 40 池子中挑出与之行业和逻辑最契合的标的。
    第二步（技术风控）：审查你挑出的标的。
    - 若其技术面安全（乖离率 < 12%，RSI < 75），将其列为【核心双龙】或【梯队先锋】。
    - 若其宏观逻辑极好，但技术面极度危险（乖离率 > 15%，严重超买），必须将其列入【诱多对照组】，严禁追高接盘！

    【硬性纪律】：
    1. 同一只股票绝对不能在报告中重复出现。
    2. 风控底线必须明确输出"周期:[X-Y天] | 止损:[具体A股价格，如18.50元]"，止损必须是具体价格数字加"元"，不能用百分比。
    3. 严格复制以下HTML骨架并填空（不要 Markdown 外框，必须保留 emoji 和 span 标签）：

    <div class="header-card">
        <h2>🌍 全局 Alpha 情报中心</h2>
        <p><b>执行时间：</b>{today_str} 盘前</p>
        <p><b>宏观驱动：</b>(结合盘前快讯，深度穿透外围走势和地缘实况，明确指出今日应该进攻的产业主线和必须回避的雷区，不少于150字)</p>
    </div>

    <div class="market-section">
        <div class="market-title">🇨🇳 A股主战场</div>

        <div class="card core-card">
            <h3>[核心双龙] 1. [名称] ([代码])</h3>
            <p><span class="tag bg-red">🔥 宏观驱动与逻辑:</span> (说明为什么它最契合今天的盘前宏观主线)</p>
            <p><span class="tag bg-blue">📈 技术面与安全垫:</span> (引用传入的乖离率、MACD、RSI数据说明买点)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[5-12天] | 止损:[XX.XX元]</p>
        </div>
        <div class="card core-card">
            <h3>[核心双龙] 2. [名称] ([代码])</h3>
            <p><span class="tag bg-red">🔥 宏观驱动与逻辑:</span> (说明主线契合度)</p>
            <p><span class="tag bg-blue">📈 技术面与安全垫:</span> (引用真实数据)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[5-12天] | 止损:[XX.XX元]</p>
        </div>

        <div class="card sub-card">
            <h3>[梯队先锋] 3. [名称] ([代码])</h3>
            <p><span class="tag bg-green">⚔️ 产业事件与资金:</span> (分析其行业催化剂)</p>
            <p><span class="tag bg-gray">📉 辅助风控点:</span> (分析技术面)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[3-7天] | 止损:[XX.XX元]</p>
        </div>
        <div class="card sub-card">
            <h3>[梯队先锋] 4. [名称] ([代码])</h3>
            <p><span class="tag bg-green">⚔️ 产业事件与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-gray">📉 辅助风控点:</span> (分析技术面)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[3-7天] | 止损:[XX.XX元]</p>
        </div>

        <div class="card obs-card">
            <h3>[筛落组] ⚠️ 观察池诊断 (Rank 5-10)</h3>
            <ul>
                <li><b>5. [名称] ([代码]):</b> (说明其宏观逻辑为何偏弱) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>6. [名称] ([代码]):</b> (说明逻辑硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>7. [名称] ([代码]):</b> (说明逻辑硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>8. [名称] ([代码]):</b> (说明逻辑硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>9. [名称] ([代码]):</b> (说明逻辑硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
                <li><b>10. [名称] ([代码]):</b> (说明逻辑硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            </ul>
        </div>
    </div>

    <div class="card trap-card">
        <h3>🚨 诱多对照组（严禁接盘）</h3>
        <ul>
            <li><b>11. [名称] ([代码]) | <span class="bear-text">诊断：坚决回避</span></b><br>❌ 宏观或技术硬伤：(说明为何不能碰)<br>⚠️ 致命硬伤：...</li>
            <li><b>12. [名称] ([代码]) | <span class="bear-text">诊断：坚决回避</span></b><br>❌ 宏观或技术硬伤：...<br>⚠️ 致命硬伤：...</li>
        </ul>
    </div>
    '''

    ai_html = ""
    with client.messages.stream(
        model=TARGET_MODEL,
        max_tokens=4096,
        temperature=0.25,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            ai_html += text

    print("✅ AI 宏观穿透报告生成完毕")
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
    raw_pool = get_a_share_data()

    if raw_pool:
        ai_html = generate_ai_report(raw_pool, macro_news)
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
            elif "梯队先锋" in context:
                tag = "Sub_Pioneer"
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
            else:
                hold_period = period_match.group(1).strip() if period_match else (
                    "5-12天" if tag == "Core_Double_Dragon" else "3-7天"
                )
                # 严格匹配 XX.XX元 格式，避免误抓其他数字
                sl_match = re.search(r'止损\s*[:：]\s*\[?(\d{1,5}\.\d{1,2}元)', chunk)
                if sl_match:
                    stop_loss = sl_match.group(1).strip()
                else:
                    # 默认止损：收盘价 × 95%
                    stop_loss = f"{round(item['Close'] * (1 + DEFAULT_STOP_LOSS_PCT / 100), 2)}元"

            item['Tag'] = tag
            item['Hold_Period'] = hold_period
            item['Stop_Loss'] = stop_loss
            item['Daily_Pct'] = item.get('pct_chg', 0)
            chosen.append(item)

        log_file = "trade_history.csv"
        need_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0

        with open(log_file, "a", encoding="utf-8") as f:
            if need_header:
                f.write("Date,Ticker,Name,Tag,Industry,Close_Price,Amount,Daily_Pct,Hold_Period,Stop_Loss\n")
            ts_date = get_bj_time().strftime('%Y-%m-%d')
            for i in chosen:
                f.write(f"{ts_date},{i['Ticker']},{i['Name']},{i['Tag']},{i.get('Industry','未知')},{i['Close']},{i['Amount']},{i['Daily_Pct']},{i['Hold_Period']},{i['Stop_Loss']}\n")

        print(f"✅ 共安全记账 {len(chosen)} 条核心数据。")
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(full_html)
        send_emails(full_html)
    else:
        print("⚠️ 数据池为空，跳过执行。")
