# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import json
import re
import smtplib
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
print(f"TUSHARE_TOKEN 是否存在: {bool(os.environ.get('TUSHARE_TOKEN'))}")
print(f"CLAWSOCKET_API_KEY 是否存在: {bool(os.environ.get('CLAWSOCKET_API_KEY'))}")
print(f"CLAWSOCKET_BASE_URL 是否存在: {bool(os.environ.get('CLAWSOCKET_BASE_URL'))}")
print(f"EMAIL_ACCOUNT 是否存在: {bool(os.environ.get('EMAIL_ACCOUNT'))}")
print(f"TARGET_EMAILS 是否存在: {bool(os.environ.get('TARGET_EMAILS'))}")

TARGET_MODEL = 'claude-opus-4-8'

def get_a_share_data():
    import tushare as ts
    ts.set_token(os.environ.get("TUSHARE_TOKEN"))
    pro = ts.pro_api()
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
    
    final_pool = {}
    df_sorted = df_daily.sort_values(by='amount', ascending=False).head(100)
    codes = [row['ts_code'] for _, row in df_sorted.iterrows()]
    
    for _, row in df_sorted.iterrows():
        ts_code = row['ts_code']
        final_pool[ts_code] = {
            "Ticker": ts_code, 
            "Name": name_map.get(ts_code, ts_code),
            "Industry": industry_map.get(ts_code, "未知"), 
            "Close": row['close'], 
            "Amount": row['amount'], 
            "Daily_Pct": row['pct_chg']
        }
            
    try:
        start_hist = (get_bj_time() - datetime.timedelta(days=100)).strftime('%Y%m%d')
        df_hist = pro.daily(ts_code=",".join(codes), start_date=start_hist, end_date=trade_date).sort_values(['ts_code', 'trade_date'])
        print(f"历史K线拉取成功，共 {len(df_hist)} 条")
        for code in final_pool:
            stock_data = df_hist[df_hist['ts_code'] == code].copy()
            if len(stock_data) >= 30:
                close_px = stock_data['close']
                ma20 = close_px.rolling(window=20).mean().iloc[-1]
                final_pool[code]["乖离率(%)"] = round(((final_pool[code]["Close"] - ma20) / ma20) * 100, 2)
                exp1 = close_px.ewm(span=12, adjust=False).mean()
                exp2 = close_px.ewm(span=26, adjust=False).mean()
                macd_hist = ((exp1 - exp2) - (exp1 - exp2).ewm(span=9, adjust=False).mean()) * 2 
                final_pool[code]["MACD今日柱"] = round(macd_hist.iloc[-1], 3)
                final_pool[code]["MACD昨日柱"] = round(macd_hist.iloc[-2], 3)
                delta = close_px.diff()
                rsi = 100 - (100 / (1 + (delta.clip(lower=0).ewm(com=13, adjust=False).mean() / -1 * delta.clip(upper=0).ewm(com=13, adjust=False).mean())))
                final_pool[code]["RSI"] = round(rsi.iloc[-1], 2)
            else:
                final_pool[code]["乖离率(%)"] = "数据不足"
    except Exception as e: 
        print(f"⚠️ 指标拉取受限: {e}")
        
    print(f"数据池准备完毕，共 {len(final_pool)} 只标的")
    return list(final_pool.values())

def generate_ai_report(pool_data):
    print("开始调用 AI 生成报告...")
    client = anthropic.Anthropic(
        api_key=os.environ.get("CLAWSOCKET_API_KEY"),
        base_url=os.environ.get("CLAWSOCKET_BASE_URL")
    )
    today_str = get_bj_time().strftime('%Y年%m月%d日')
    
    prompt = f'''
    你是一个顶级私募策略总监。今天是{today_str}。基于以下A股数据池：
    {json.dumps(pool_data, ensure_ascii=False)}
    
    【核心任务】：选出全市场最顶尖的标的！不要顾虑之前是否推荐过，只要数据目前是最优的，就选它。
    1. 必须选出MACD绿柱缩短（或金叉）且乖离率<15%的标的。
    2. 【排他性红线】：一封报告内，同一只股票绝对不能在双龙、先锋、筛落组、诱多组中重复出现！
    
    严格复制以下HTML骨架并填空（必须保留emoji和span标签）：
    
    <div class="header-card">
        <h2>🌍 全局 Alpha 情报中心</h2>
        <p><b>执行时间：</b>{today_str} 盘前</p>
        <p><b>宏观驱动：</b>(结合地缘和产业叙事，不少于100字)</p>
    </div>
    
    <div class="market-section">
        <div class="market-title">🇨🇳 A股主战场</div>
        
        <div class="card core-card">
            <h3>[核心双龙] 1. [名称] ([代码])</h3>
            <p><span class="tag bg-red">🔥 宏观情报与起爆逻辑:</span> (阐述主力炒作意图)</p>
            <p><span class="tag bg-blue">📈 技术面多周期共振:</span> (引用乖离率、MACD、RSI真实数据)</p>
            <p><span class="tag bg-purple">📊 EV估值与筹码测算:</span> (分析筹码集中度或估值优势)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[X-Y天] | 止损:[具体价格或百分比]</p>
        </div>
        <div class="card core-card">
            <h3>[核心双龙] 2. [名称] ([代码])</h3>
            <p><span class="tag bg-red">🔥 宏观情报与起爆逻辑:</span> (阐述主力炒作意图)</p>
            <p><span class="tag bg-blue">📈 技术面多周期共振:</span> (引用真实数据)</p>
            <p><span class="tag bg-purple">📊 EV估值与筹码测算:</span> (分析筹码或估值)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[X-Y天] | 止损:[具体价格或百分比]</p>
        </div>
        
        <div class="card sub-card">
            <h3>[梯队先锋] 3. [名称] ([代码])</h3>
            <p><span class="tag bg-gray">📉 均线与周期:</span> (结合中期趋势)</p>
            <p><span class="tag bg-green">⚔️ 事件驱动与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[X-Y天] | 止损:[具体价格或百分比]</p>
        </div>
        <div class="card sub-card">
            <h3>[梯队先锋] 4. [名称] ([代码])</h3>
            <p><span class="tag bg-gray">📉 均线与周期:</span> (结合中期趋势)</p>
            <p><span class="tag bg-green">⚔️ 事件驱动与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> 周期:[X-Y天] | 止损:[具体价格或百分比]</p>
        </div>
        
        <div class="card obs-card">
            <h3>[筛落组] ⚠️ 观察池诊断 (Rank 5-10)</h3>
            <ul>
                <li><b>5. [名称] ([代码]):</b> (说明其硬伤) <br><span class="tag bg-orange">⚠️ 风控:</span> 周期:[X-Y天或观望] | 止损:[具体价格]</li>
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
            <li><b>11. [名称] ([代码]) | <span class="bear-text">诊断：看跌</span></b><br>❌ 诱多技术面：...<br>⚠️ 致命硬伤：...</li>
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
    raw_pool = get_a_share_data()
    if raw_pool:
        ai_html = generate_ai_report(raw_pool)
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

            tag = "Trap_Warning"
            pre_chunk = clean_html[max(0, idx-200):idx]
            if "[核心双龙]" in pre_chunk or "[核心双龙]" in chunk:
                tag = "Core_Double_Dragon"
            elif "[梯队先锋]" in pre_chunk or "[梯队先锋]" in chunk:
                tag = "Sub_Pioneer"
            elif "[筛落组]" in pre_chunk or "筛落组" in pre_chunk:
                tag = "Observation"
            elif "诱多对照组" in pre_chunk or "严禁接盘" in pre_chunk:
                tag = "Trap_Warning"

            if tag == "Trap_Warning":
                item['Tag'] = tag
                item['Hold_Period'] = "坚决空仓"
                item['Stop_Loss'] = "绝对规避"
                chosen.append(item)
                continue

            period_match = re.search(r'周期\s*[:：]\s*\[?(\d+[-~]\d+天|\d+天|观望)', chunk)
            sl_match = re.search(r'止损\s*[:：]\s*\[?(\d+\.?\d*元?%?|-\d+\.?\d*%?)', chunk)

            item['Tag'] = tag
            item['Hold_Period'] = period_match.group(1).strip() if period_match else "N/A"
            item['Stop_Loss'] = sl_match.group(1).strip() if sl_match else "N/A"
            chosen.append(item)

        log_file = "trade_history.csv"
        need_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0
        
        with open(log_file, "a", encoding="utf-8") as f:
            if need_header:
                f.write("Date,Ticker,Name,Tag,Industry,Close_Price,Amount,Daily_Pct,Hold_Period,Stop_Loss\n")
            ts = get_bj_time().strftime('%Y-%m-%d')
            for i in chosen: 
                f.write(f"{ts},{i['Ticker']},{i['Name']},{i['Tag']},{i.get('Industry','未知')},{i['Close']},{i['Amount']},{i['Daily_Pct']},{i['Hold_Period']},{i['Stop_Loss']}\n")
        
        print(f"✅ 共入库 {len(chosen)} 条记录")
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(full_html)
            
        send_emails(full_html)
    else:
        print("⚠️ 数据池为空，跳过AI生成和发送。")
