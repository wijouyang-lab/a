# 消息+逻辑推演驱动版 | 事件→产业链→受益标的
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

TARGET_MODEL = 'claude-opus-4-8'
DEFAULT_STOP_LOSS_PCT = -5.0

ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()


# ==========================================
# 1. 获取交易额 Top 300（轻量级圈定主力池）
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

    # 交易额 Top 300，资金已在此聚集
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
# 2. 多源免费新闻采集（彻底避开 Tushare 权限）
# ==========================================
def get_free_macro_news():
    print("📡 [阶段2] 正在跨过 Tushare，从免费公网节点抓取全球财经与A股新闻...")
    news_lines = []
    
    # 🔒 安全锁1：获取当前年份，用于剔除穿越的旧新闻
    current_year = str(get_bj_time().year)

    # 使用国内外免费且稳定的 RSS 接口
    sources = [
        ("新浪A股热点", "https://rss.sina.com.cn/roll/finance/hot_roll.xml"),
        ("华尔街日报(宏观)", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
        ("CNBC(宏观)", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ]
    
    for source_name, url in sources:
        try:
            # 伪装浏览器请求头，防止被反爬屏蔽
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
            root = ET.fromstring(xml_data)
            items = root.findall('.//item')[:8] # 每个源取前8条
            for item in items:
                title = item.find('title')
                pub_date = item.find('pubDate')
                if title is not None:
                    time_str = pub_date.text[:25] if pub_date is not None else ""
                    # 🔒 安全锁1执行：如果时间戳里没有今年的年份，直接视为脏数据丢弃！
                    if current_year not in time_str:
                        continue
                    news_lines.append(f"[{source_name}] {time_str} - {title.text}")
            print(f"   ✅ {source_name} 节点抓取成功")
        except Exception as e:
            print(f"   ⚠️ {source_name} 节点抓取失败: {e}")

    if news_lines:
        print(f"✅ 盘前免费新闻矩阵组装完毕，共 {len(news_lines)} 条新鲜资讯。")
        return "\n".join(news_lines)
    return "暂无实时新闻，请基于昨日收盘及底层产业逻辑推演。"


# ==========================================
# 3. 定向计算技术指标（仅针对这 300 只票）
# ==========================================
def calc_tech_indicators(full_pool, codes):
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    print("⚙️ [阶段3] 正在回头定向拉取 Top 300 的历史K线，计算风控技术指标...")
    
    try:
        start_hist = (get_bj_time() - datetime.timedelta(days=120)).strftime('%Y%m%d')
        # 仅请求这300只股票的历史数据，大幅节约接口性能
        df_hist = pro.daily(
            ts_code=",".join(codes),
            start_date=start_hist,
            end_date=trade_date
        ).sort_values(['ts_code', 'trade_date'])

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
                del full_pool[code] # 剔除次新股等数据不足的标的

    except Exception as e:
        print(f"🚨 指标拉取受限: {e}")

    final_pool = sorted(list(full_pool.values()), key=lambda x: x.get("Amount", 0), reverse=True)
    print(f"✅ 技术指标运算完成，最终 {len(final_pool)} 只标的打包装车。")
    return final_pool


# ==========================================
# 4. Claude 事件逻辑推演选股
# ==========================================
def generate_ai_report(pool_data, macro_news_text):
    print("🧠 [阶段4] 召唤 AI 大脑（执行：事件→产业链→受益标的逻辑推演）...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )
    today_str = get_bj_time().strftime('%Y年%m月%d日')

    compact_pool = [
        {
            "名称": d["Name"],
            "代码": d["Ticker"],
            "行业": d["Industry"],
            "收盘价": d["Close"],
            "今日涨跌(%)": d.get("pct_chg", 0),
            "乖离率(%)": d.get("乖离率(%)", "N/A"),
            "RSI": d.get("RSI", "N/A"),
            "MACD": d.get("MACD趋势", "N/A"),
        }
        for d in pool_data
    ]

    prompt = f'''
你是顶级A股事件驱动型游资操盘手，擅长从宏观事件推演产业链受益逻辑，精准找到资金聚集的受益标的。

今天是{today_str}。

【今日全球宏观与A股消息面】：
{macro_news_text}

【今日A股交易额 Top 100（资金已在此聚集）】：
{json.dumps(compact_pool[:100], ensure_ascii=False)} 
*(注：为保证专注力，仅向你展示前100只最核心活跃池)*

技术数据字段说明（仅作风控参考，不作选股依据）：
- 今日涨跌(%)：今日市场情绪
- 乖离率(%)：偏离20日均线，>20%视为短期极度透支
- RSI：>85为极度超买危险区
- MACD：走强/走弱，辅助判断动能

【你的核心工作流程】：

━━━━━━━━━━━━━━━━━━━━━━
第一步：事件识别与逻辑推演
━━━━━━━━━━━━━━━━━━━━━━
仔细阅读上方所有新闻，识别出今日最重要的2-3个事件。
对每个事件做完整的产业链推演，例如：

事件：中国限制钨粉出口
→ 逻辑链：中国是全球最大钨资源国 → 出口限制导致全球钨粉供应收紧
→ 直接受益：中国国内钨矿开采和冶炼企业（拥有资源定价权）
→ 间接受益：六氟化钨（芯片制造原料，钨的下游）出口企业
→ 间接受损：依赖进口钨粉的欧美半导体厂
→ 在A股Top 100中寻找：钨矿、钨加工、六氟化钨相关企业

━━━━━━━━━━━━━━━━━━━━━━
第二步：从核心池中匹配受益标的
━━━━━━━━━━━━━━━━━━━━━━
基于你推演出的逻辑，在提供的池子中找出行业最直接契合的标的。
优先选择：逻辑链最短（直接受益）> 逻辑链较长（间接受益）。

━━━━━━━━━━━━━━━━━━━━━━
第三步：技术面风控兜底（非选股依据）
━━━━━━━━━━━━━━━━━━━━━━
技术面只做两件事：
① 确认止损位（基于收盘价和乖离率设定合理止损）
② 排除极端透支（乖离率>20% 且 RSI>85 才进诱多组，二者必须同时满足）
技术面不影响选股，但影响风控底线的设定。

━━━━━━━━━━━━━━━━━━━━━━
第四步：输出精选10只
━━━━━━━━━━━━━━━━━━━━━━
【硬性纪律】：
1. 每只推荐必须写清楚完整的逻辑链：事件→传导机制→为什么这只股票受益。
2. 同一只股票绝对不能重复出现。
3. 风控底线格式：周期:[X-Y天] | 止损:[XX.XX元]（止损必须是具体价格加"元"）。
4. 如果今日新闻中找不到足够强的事件逻辑，宁可少选，不要凑数推荐。
5. 严格按以下HTML骨架输出，不加markdown外框：

<div class="header-card">
    <h2>🌍 今日事件逻辑推演中心</h2>
    <p><b>执行时间：</b>{today_str} 盘前</p>
    <div style="background:#fff3e0;border-left:4px solid #ff9800;padding:15px;margin-top:10px;border-radius:4px;">
        <b>📋 今日核心事件与完整逻辑链：</b>
        <p><b>事件1：</b>[事件标题] → [完整推演：为什么这个事件利好/利空哪个产业链，受益逻辑是什么]</p>
        <p><b>事件2：</b>[事件标题] → [完整推演]</p>
        <p><b>受损预警：</b>[哪些行业/标的因今日事件受损，需回避]</p>
    </div>
</div>

<div class="market-section">
    <div class="market-title">🇨🇳 A股事件驱动精选</div>

    <div class="card core-card">
        <h3>[核心双龙] 1. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 事件逻辑链：</span>[具体事件] → [传导机制，1-2句话说清楚] → [该企业为什么是直接受益方，不能只说"行业受益"]</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>今日属于巨量核心池标的，涨跌[X]%，资金已在关注此方向</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>乖离率[X]%，RSI[X]，[是否存在透支风险的简短判断]</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元]</p>
    </div>

    <div class="card core-card">
        <h3>[核心双龙] 2. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 事件逻辑链：</span>(...)</p>
        <p><span class="tag bg-blue">💰 资金验证：</span>(...)</p>
        <p><span class="tag bg-gray">📈 技术风控：</span>(...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[5-12天] | 止损:[XX.XX元]</p>
    </div>

    <div class="card sub-card">
        <h3>[梯队先锋] 3. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 事件逻辑链：</span>(...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[3-7天] | 止损:[XX.XX元]</p>
    </div>

    <div class="card sub-card">
        <h3>[梯队先锋] 4. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 事件逻辑链：</span>(...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[3-7天] | 止损:[XX.XX元]</p>
    </div>

    <div class="card sub-card">
        <h3>[梯队先锋] 5. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 事件逻辑链：</span>(...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[3-7天] | 止损:[XX.XX元]</p>
    </div>

    <div class="card sub-card">
        <h3>[梯队先锋] 6. [名称] ([代码]) | [行业]</h3>
        <p><span class="tag bg-red">🔗 事件逻辑链：</span>(...)</p>
        <p><span class="tag bg-orange">⚠️ 风控底线：</span>周期:[3-7天] | 止损:[XX.XX元]</p>
    </div>

    <div class="card obs-card">
        <h3>[观察池] ⚠️ 逻辑待确认或次级受益 (Rank 7-10)</h3>
        <ul>
            <li><b>7. [名称] ([代码]) | [行业]：</b>[说明逻辑链较弱或事件尚未确认的原因] <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            <li><b>8. [名称] ([代码]) | [行业]：</b>(...) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            <li><b>9. [名称] ([代码]) | [行业]：</b>(...) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
            <li><b>10. [名称] ([代码]) | [行业]：</b>(...) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[观望] | 止损:[观望]</li>
        </ul>
    </div>
</div>

<div class="card trap-card">
    <h3>🚨 事件逻辑受损组（严禁接盘）</h3>
    <ul>
        <li><b>[名称] ([代码]) | <span class="bear-text">逻辑受损</span></b><br>❌ 受损逻辑：[具体说明哪个事件导致该标的基本面受损，传导链是什么]<br>⚠️ 回避理由：...</li>
        <li><b>[名称] ([代码]) | <span class="bear-text">逻辑受损</span></b><br>❌ 受损逻辑：...<br>⚠️ 回避理由：...</li>
    </ul>
</div>
'''

    ai_html = ""
    with client.messages.stream(
        model=TARGET_MODEL,
        max_tokens=4096,
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
        .bg-orange{background:#e64a19}
        .bg-gray{background:#607d8b}
        .bg-green{background:#37474f}
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
    msg['Subject'], msg['From'] = "【事件驱动】A股逻辑推演精选", f"Alpha Radar <{acc}>"
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
    # 步骤 1：寻找全场最热的 300 只股票
    full_pool, codes = get_top_300_pool()

    if full_pool:
        # 步骤 2：去公网白嫖全量新闻（规避 Tushare 接口报错）
        macro_news = get_free_macro_news()
        
        # 步骤 3：带着前 300 的名单，回头去算风控技术面
        final_pool = calc_tech_indicators(full_pool, codes)

        # 🔒 安全锁2执行：防止数据枯竭导致AI生成空列表报错
        if len(final_pool) < 10:
            print("🚨 触发安全熔断：清洗后有效标的不足10只，终止 AI 调用防崩溃。请检查接口额度！")
            import sys; sys.exit(0)

        # 步骤 4：送交 AI 大脑推演
        ai_html = generate_ai_report(final_pool, macro_news)
        full_html = build_email(ai_html)

        # ====== 账本清算逻辑 ======
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

            if "核心双龙" in context: tag = "Core_Double_Dragon"
            elif "梯队先锋" in context: tag = "Sub_Pioneer"
            elif "观察池" in context: tag = "Observation"
            elif "逻辑受损" in context or "坚决回避" in context: tag = "Trap_Warning"

            if tag is None or tag == "Trap_Warning":
                continue

            period_match = re.search(r'周期\s*[:：]\s*\[?(\d+[-~]\d+天|\d+天|观望)', chunk)

            if tag == "Observation":
                hold_period, stop_loss = "观望", "观望"
            else:
                hold_period = period_match.group(1).strip() if period_match else ("5-12天" if tag == "Core_Double_Dragon" else "3-7天")
                sl_match = re.search(r'止损\s*[:：]\s*\[?(\d{1,5}\.\d{1,2}元)', chunk)
                stop_loss = sl_match.group(1).strip() if sl_match else f"{round(item['Close'] * (1 + DEFAULT_STOP_LOSS_PCT / 100), 2)}元"

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
