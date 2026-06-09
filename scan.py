# 自动进化版本 | 时间: 2026-06-09 10:21 | 触发胜率: 22.5%

# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import json
import re
import smtplib
import tushare as ts
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

# 诊断信息
print(f"当前UTC时间: {datetime.datetime.utcnow()}")
print(f"当前北京时间: {get_bj_time()}")
print(f"星期: {get_bj_time().weekday()} (0=周一 6=周日)")
print(f"小时: {get_bj_time().hour}")

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

# ========== 量化筛选参数（核心调优区） ==========
MAX_BIAS_PCT = 12.0        # 乖离率上限（MA20），超过视为超买
MAX_RSI = 70.0             # RSI上限，超过视为超买
MAX_5D_GAIN = 20.0         # 近5日最大涨幅，排除连板追高
MIN_AMOUNT = 50000.0       # 最小成交额（万元），排除流动性差的
CANDIDATE_POOL_SIZE = 30   # 传入AI的候选池大小
DEFAULT_STOP_LOSS_PCT = -5.0  # 默认止损百分比

# 初始化 Tushare
ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()

def get_latest_macro_news():
    """盘前雷达：获取最新全球宏观与财经快讯"""
    print("📡 正在抓取盘前最新全球宏观与财经快讯...")
    try:
        # 获取新浪财经最新15条滚动新闻
        df_news = pro.news(src='sina', limit=15)
        if df_news is not None and not df_news.empty:
            news_lines = []
            for _, row in df_news.iterrows():
                # 提取时间与标题
                news_lines.append(f"- {row['datetime'][11:16]}: {row['title']}")
            print("✅ 盘前快讯抓取成功！")
            return "\n".join(news_lines)
    except Exception as e:
        print(f"⚠️ 盘前新闻拉取受限: {e}")
    return "暂无实时盘前新闻，请基于昨收盘及底层产业逻辑进行推演。"


def get_a_share_data():
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    print(f"正在拉取 {trade_date} 的A股数据...")
    
    df_daily = pro.daily(trade_date=trade_date)
    if df_daily is None or df_daily.empty:
        trade_date = (get_bj_time() - datetime.timedelta(days=2)).strftime('%Y%m%d')
        print(f"昨日数据为空，尝试 {trade_date}...")
        df_daily = pro.daily(trade_date=trade_date)
        if df_daily is None or df_daily.empty:
            print("数据拉取失败，返回空。")
            return []

    print(f"成功拉取 {len(df_daily)} 条数据")
    basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
    name_map = dict(zip(basic['ts_code'], basic['name']))
    industry_map = dict(zip(basic['ts_code'], basic.get('industry', ['核心资产'] * len(basic))))
    
    # 第一步：按成交额取Top100作为基础池
    df_sorted = df_daily.sort_values(by='amount', ascending=False).head(100)
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
            "Daily_Pct": row['pct_chg']
        }
            
    # 第二步：拉取历史数据计算技术指标
    try:
        start_hist = (get_bj_time() - datetime.timedelta(days=120)).strftime('%Y%m%d')
        df_hist = pro.daily(ts_code=",".join(codes), start_date=start_hist, end_date=trade_date).sort_values(['ts_code', 'trade_date'])
        print(f"历史K线拉取成功，共 {len(df_hist)} 条")
        
        for code in list(full_pool.keys()):
            stock_data = df_hist[df_hist['ts_code'] == code].copy()
            if len(stock_data) >= 30:
                close_px = stock_data['close']
                
                # MA均线
                ma5 = close_px.rolling(window=5).mean().iloc[-1]
                ma10 = close_px.rolling(window=10).mean().iloc[-1]
                ma20 = close_px.rolling(window=20).mean().iloc[-1]
                current_close = full_pool[code]["Close"]
                
                full_pool[code]["MA5"] = round(ma5, 2)
                full_pool[code]["MA10"] = round(ma10, 2)
                full_pool[code]["MA20"] = round(ma20, 2)
                full_pool[code]["乖离率(%)"] = round(((current_close - ma20) / ma20) * 100, 2)
                
                # 均线多头排列判断
                full_pool[code]["均线多头"] = (current_close > ma5 > ma10 > ma20)
                
                # 价格在MA20之上
                full_pool[code]["价格在MA20上"] = (current_close >= ma20)
                
                # MACD
                exp1 = close_px.ewm(span=12, adjust=False).mean()
                exp2 = close_px.ewm(span=26, adjust=False).mean()
                macd_line = exp1 - exp2
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                macd_hist = (macd_line - signal_line) * 2 
                full_pool[code]["MACD今日柱"] = round(macd_hist.iloc[-1], 3)
                full_pool[code]["MACD昨日柱"] = round(macd_hist.iloc[-2], 3)
                
                # MACD趋势改善：今日柱>昨日柱（绿柱缩短或红柱放大）
                full_pool[code]["MACD改善"] = (macd_hist.iloc[-1] > macd_hist.iloc[-2])
                
                # RSI
                delta = close_px.diff()
                gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
                loss = (-1 * delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))
                full_pool[code]["RSI"] = round(rsi.iloc[-1], 2)
                
                # 近5日涨幅
                if len(close_px) >= 6:
                    pct_5d = ((close_px.iloc[-1] / close_px.iloc[-6]) - 1) * 100
                    full_pool[code]["近5日涨幅(%)"] = round(pct_5d, 2)
                else:
                    full_pool[code]["近5日涨幅(%)"] = 0.0
            else:
                full_pool[code]["乖离率(%)"] = 99.0  # 数据不足标记为不合格
                full_pool[code]["RSI"] = 99.0
                full_pool[code]["MACD改善"] = False
                full_pool[code]["近5日涨幅(%)"] = 99.0
                full_pool[code]["价格在MA20上"] = False
                full_pool[code]["均线多头"] = False
                
    except Exception as e: 
        print(f"⚠️ 指标拉取受限: {e}")
        return list(full_pool.values())[:CANDIDATE_POOL_SIZE]
    
    # ========== 第三步：硬性量化预筛 ==========
    qualified_pool = []
    rejected_pool = []
    
    for code, item in full_pool.items():
        bias = item.get("乖离率(%)", 99.0)
        rsi_val = item.get("RSI", 99.0)
        macd_improving = item.get("MACD改善", False)
        gain_5d = item.get("近5日涨幅(%)", 99.0)
        above_ma20 = item.get("价格在MA20上", False)
        amount = item.get("Amount", 0)
        
        # 硬性过滤条件
        if (isinstance(bias, (int, float)) and bias <= MAX_BIAS_PCT and
            isinstance(rsi_val, (int, float)) and rsi_val <= MAX_RSI and
            macd_improving and
            isinstance(gain_5d, (int, float)) and gain_5d <= MAX_5D_GAIN and
            above_ma20 and
            amount >= MIN_AMOUNT):
            qualified_pool.append(item)
        else:
            # 记录被淘汰的原因，供诱多组使用
            reasons = []
            if isinstance(bias, (int, float)) and bias > MAX_BIAS_PCT:
                reasons.append(f"乖离率{bias}%过高")
            if isinstance(rsi_val, (int, float)) and rsi_val > MAX_RSI:
                reasons.append(f"RSI={rsi_val}超买")
            if not macd_improving:
                reasons.append("MACD走弱")
            if isinstance(gain_5d, (int, float)) and gain_5d > MAX_5D_GAIN:
                reasons.append(f"近5日涨{gain_5d}%追高风险")
            if not above_ma20:
                reasons.append("跌破MA20")
            item["淘汰原因"] = "；".join(reasons) if reasons else "数据不足"
            rejected_pool.append(item)
    
    print(f"量化预筛通过: {len(qualified_pool)} 只，淘汰: {len(rejected_pool)} 只")
    
    # 对通过的池子按"综合评分"排序：均线多头+MACD改善幅度+RSI适中
    for item in qualified_pool:
        score = 0
        # 均线多头排列加分
        if item.get("均线多头", False):
            score += 30
        # MACD改善幅度加分
        macd_today = item.get("MACD今日柱", 0)
        macd_yesterday = item.get("MACD昨日柱", 0)
        if isinstance(macd_today, (int, float)) and isinstance(macd_yesterday, (int, float)):
            macd_delta = macd_today - macd_yesterday
            score += min(macd_delta * 5, 20)  # 上限20分
        # RSI在40-60区间最佳
        rsi_val = item.get("RSI", 50)
        if isinstance(rsi_val, (int, float)):
            if 40 <= rsi_val <= 60:
                score += 20
            elif 30 <= rsi_val <= 70:
                score += 10
        # 乖离率越小越好
        bias = item.get("乖离率(%)", 0)
        if isinstance(bias, (int, float)):
            score += max(0, (12 - abs(bias)) * 2)
        item["综合评分"] = round(score, 2)
    
    qualified_pool.sort(key=lambda x: x.get("综合评分", 0), reverse=True)
    
    # 取Top候选 + 部分淘汰池供诱多组参考
    final_candidates = qualified_pool[:CANDIDATE_POOL_SIZE]
    trap_candidates = sorted(rejected_pool, key=lambda x: x.get("Amount", 0), reverse=True)[:6]
    
    # 标记用途
    for item in final_candidates:
        item["_pool_type"] = "candidate"
    for item in trap_candidates:
        item["_pool_type"] = "trap_reference"
    
    combined = final_candidates + trap_candidates
    print(f"最终传入AI: {len(final_candidates)} 只候选 + {len(trap_candidates)} 只诱多参考")
    return combined


def generate_ai_report(pool_data, macro_news_text):
    print("开始调用 AI 生成报告...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )
    today_str = get_bj_time().strftime('%Y年%m月%d日')
    
    # 分离候选池和诱多参考池
    candidates = [x for x in pool_data if x.get("_pool_type") == "candidate"]
    traps = [x for x in pool_data if x.get("_pool_type") == "trap_reference"]
    
    prompt = f'''
    你是一个顶级私募策略总监。今天是{today_str}。
    
    【🔴 盘前宏观与全球重大快讯（必须阅读）】：
    {macro_news_text}
    *防错指令：必须结合上述最新消息（如美股昨夜大盘情绪、地缘局势最新动态）来定调开盘情绪。如果出现表面停火协议，请同时客观评估底层博弈（如霍尔木兹海峡实际依然处于封锁状态对资源航运的支撑）。严禁在隔夜外围大涨或重大利好下无脑推荐做空，也严禁盲目追高。*

    【已通过量化预筛的候选池】（均满足：乖离率≤12%、RSI≤70、MACD改善、价格在MA20之上）：
    {json.dumps(candidates, ensure_ascii=False, default=str)}
    
    【被量化淘汰的诱多参考池】（技术面恶化，仅供诱多组使用）：
    {json.dumps(traps, ensure_ascii=False, default=str)}
    
    【核心任务】：从候选池中选出最优标的。
    
    【硬性约束——违反任何一条则整份报告作废】：
    1. 核心双龙和梯队先锋只能从"候选池"中选取，绝对禁止选入乖离率>12%或RSI>70的标的
    2. 诱多对照组只能从"诱多参考池"中选取
    3. 一封报告内，同一只股票绝对不能在双龙、先锋、筛落组、诱多组中重复出现
    4. 优先选择「均线多头排列」（MA5>MA10>MA20）且「综合评分」靠前的标的
    5. 止损位必须设在MA20或近期支撑位，且止损幅度控制在-3%到-7%之间
    6. 持仓周期：核心双龙5-12天，梯队先锋3-7天
    
    严格复制以下HTML骨架并填空（必须保留emoji和span标签）：
    
    <div class="header-card">
        <h2>🌍 全局 Alpha 情报中心</h2>
        <p><b>执行时间：</b>{today_str} 盘前</p>
        <p><b>宏观驱动：</b>(必须结合上述盘前快讯，深度穿透外围走势和地缘实况的影响，不少于150字)</p>
    </div>
    
    <div class="market-section">
        <div class="market-title">🇨🇳 A股主战场</div>
        
        <div class="card core-card">
            <h3>[核心双龙] 1. [名称] ([代码])</h3>
            <p><span class="tag bg-red">🔥 宏观情报与起爆逻辑:</span> (结合盘前新闻阐述资金炒作意图)</p>
            <p><span class="tag bg-blue">📈 技术面多周期共振:</span> (必须引用乖离率、MACD柱值、RSI、MA5/MA10/MA20真实数据)</p>
            <p><span class="tag bg-purple">📊 EV估值与筹码测算:</span> (分析筹码集中度或估值优势)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[5-12天] | 止损:[具体价格，基于MA20或支撑位]</p>
        </div>
        <div class="card core-card">
            <h3>[核心双龙] 2. [名称] ([代码])</h3>
            <p><span class="tag bg-red">🔥 宏观情报与起爆逻辑:</span> (阐述主力炒作意图)</p>
            <p><span class="tag bg-blue">📈 技术面多周期共振:</span> (必须引用真实数据)</p>
            <p><span class="tag bg-purple">📊 EV估值与筹码测算:</span> (分析筹码或估值)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[5-12天] | 止损:[具体价格]</p>
        </div>
        
        <div class="card sub-card">
            <h3>[梯队先锋] 3. [名称] ([代码])</h3>
            <p><span class="tag bg-gray">📉 均线与周期:</span> (结合MA5/MA10/MA20多头排列状态)</p>
            <p><span class="tag bg-green">⚔️ 事件驱动与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[3-7天] | 止损:[具体价格]</p>
        </div>
        <div class="card sub-card">
            <h3>[梯队先锋] 4. [名称] ([代码])</h3>
            <p><span class="tag bg-gray">📉 均线与周期:</span> (结合中期趋势)</p>
            <p><span class="tag bg-green">⚔️ 事件驱动与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[3-7天] | 止损:[具体价格]</p>
        </div>
        
        <div class="card obs-card">
            <h3>[筛落组] ⚠️ 观察池诊断 (Rank 5-10)</h3>
            <ul>
                <li><b>5. [名称] ([代码]):</b> (说明其不如前4名的原因) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[X-Y天或观望] | 止损:[具体价格]</li>
                <li><b>6. [名称] ([代码]):</b> (说明其硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[X-Y天或观望] | 止损:[具体价格]</li>
                <li><b>7. [名称] ([代码]):</b> (说明其硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[X-Y天或观望] | 止损:[具体价格]</li>
                <li><b>8. [名称] ([代码]):</b> (说明其硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[X-Y天或观望] | 止损:[具体价格]</li>
                <li><b>9. [名称] ([代码]):</b> (说明其硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[X-Y天或观望] | 止损:[具体价格]</li>
                <li><b>10. [名称] ([代码]):</b> (说明其硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[X-Y天或观望] | 止损:[具体价格]</li>
            </ul>
        </div>
    </div>
    
    <div class="card trap-card">
        <h3>🚨 诱多对照组（严禁接盘）</h3>
        <ul>
            <li><b>11. [名称] ([代码]) | <span class="bear-text">诊断：看跌</span></b><br>❌ 诱多技术面：(引用其被淘汰原因中的数据)<br>⚠️ 致命硬伤：...</li>
            <li><b>12. [名称] ([代码]) | <span class="bear-text">诊断：看跌</span></b><br>❌ 诱多技术面：...<br>⚠️ 致命硬伤：...</li>
        </ul>
    </div>
    '''

    ai_html = ""
    with client.messages.stream(
        model=TARGET_MODEL,
        max_tokens=4096,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            ai_html += text

    print("AI 报告生成完毕")
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
        print("⚠️ 邮箱配置或收件人名单缺失，跳过发送。")
        return
        
    msg = MIMEMultipart()
    msg['Subject'], msg['From'] = "【波段内参】A股雷达核心打分榜单", f"Alpha Radar <{acc}>"
    msg.attach(MIMEText(html_content, 'html'))
    targets = [e.strip() for e in email_list_str.split(",")]
    
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(acc, pwd)
        server.sendmail(acc, targets, msg.as_string())
        server.quit()
        print(f"✅ 邮件密送成功至 {len(targets)} 个节点！")
    except Exception as e: 
        print(f"🚨 邮件发送失败: {e}")


if __name__ == "__main__":
    # 1. 抓取盘前宏观新闻
    macro_news = get_latest_macro_news()
    
    # 2. 拉取量化基础数据
    raw_pool = get_a_share_data()
    
    if raw_pool:
        # 3. 传入新闻与数据，生成 AI 战报
        ai_html = generate_ai_report(raw_pool, macro_news)
        full_html = build_email(ai_html)
        
        # ========== 改进后的标签解析与入库逻辑 ==========
        chosen = []
        clean_html = re.sub(r'<[^>]+>', ' ', ai_html)
        clean_html = re.sub(r'\s+', ' ', clean_html)

        for item in raw_pool:
            if item.get("_pool_type") != "candidate":
                continue
                
            ticker_str = str(item['Name'])
            idx = clean_html.find(ticker_str)
            if idx == -1:
                continue

            chunk = clean_html[idx:idx+800]

            tag = None
            pre_chunk = clean_html[max(0, idx-300):idx]
            post_chunk = chunk[:200]
            context = pre_chunk + post_chunk
            
            if "核心双龙" in context:
                tag = "Core_Double_Dragon"
            elif "梯队先锋" in context:
                tag = "Sub_Pioneer"
            elif "筛落组" in context or "观察池" in context:
                tag = "Observation"
            elif "诱多" in context or "严禁接盘" in context:
                tag = "Trap_Warning"
            
            # 只入库核心双龙和梯队先锋
            if tag not in ("Core_Double_Dragon", "Sub_Pioneer"):
                continue

            period_match = re.search(r'周期\s*[:：]\s*\[?(\d+[-~]\d+天|\d+天)', chunk)
            if period_match:
                hold_period = period_match.group(1).strip()
            else:
                hold_period = "5-12天" if tag == "Core_Double_Dragon" else "3-7天"

            sl_match = re.search(r'止损\s*[:：]\s*\[?(\d+\.?\d*元?%?|-\d+\.?\d*%?)', chunk)
            if sl_match:
                stop_loss = sl_match.group(1).strip()
            else:
                default_sl = round(item['Close'] * (1 + DEFAULT_STOP_LOSS_PCT / 100), 2)
                stop_loss = f"{default_sl}元"

            item['Tag'] = tag
            item['Hold_Period'] = hold_period
            item['Stop_Loss'] = stop_loss
            chosen.append(item)

        log_file = "trade_history.csv"
        need_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0
        
        with open(log_file, "a", encoding="utf-8") as f:
            if need_header:
                f.write("Date,Ticker,Name,Tag,Industry,Close_Price,Amount,Daily_Pct,Hold_Period,Stop_Loss\n")
            ts_date = get_bj_time().strftime('%Y-%m-%d')
            for i in chosen: 
                f.write(f"{ts_date},{i['Ticker']},{i['Name']},{i['Tag']},{i.get('Industry','未知')},{i['Close']},{i['Amount']},{i['Daily_Pct']},{i['Hold_Period']},{i['Stop_Loss']}\n")
        
        print(f"✅ 共入库 {len(chosen)} 条记录（仅Core_Double_Dragon和Sub_Pioneer）")
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(full_html)
            
        send_emails(full_html)
    else:
        print("⚠️ 数据池为空，跳过AI生成和发送。")
