# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import json
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import types

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

today = get_bj_time().weekday()
if today >= 5:
    print("🚨 周末不开盘，退出早盘扫描。")
    import sys; sys.exit(0)

TARGET_MODEL = 'gemini-3.1-pro-preview'

def get_a_share_data():
    import tushare as ts
    ts.set_token(os.environ.get("TUSHARE_TOKEN"))
    pro = ts.pro_api()
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    
    df_daily = pro.daily(trade_date=trade_date)
    if df_daily is None or df_daily.empty:
        trade_date = (get_bj_time() - datetime.timedelta(days=2)).strftime('%Y%m%d')
        df_daily = pro.daily(trade_date=trade_date)
        if df_daily is None or df_daily.empty: return []

    basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
    name_map = dict(zip(basic['ts_code'], basic['name']))
    industry_map = dict(zip(basic['ts_code'], basic.get('industry', ['核心资产'] * len(basic))))
    
    final_pool = {}
    # 按成交额排序，提取全市场最活跃的 Top 100 资金主战场
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
        
    return list(final_pool.values())

def generate_ai_report(pool_data):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    today_str = get_bj_time().strftime('%Y年%m月%d日')
    
    prompt = f'''
    你是一个顶级私募策略总监。今天是{today_str}。基于以下A股数据池：
    {json.dumps(pool_data, ensure_ascii=False)}
    
    【核心任务】：选出全市场最顶尖的标的！不要顾虑之前是否推荐过，只要数据目前是最优的，就选它。
    1. 必须选出MACD绿柱缩短（或金叉）且乖离率<15%的标的。
    2. 【排他性红线】：一封报告内，同一只股票绝对不能在双龙、先锋、诱多组中重复出现！
    
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
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> (必须明确【预计持股周期(天数)】和【具体止损位(价格)】)</p>
        </div>
        <div class="card core-card">
            <h3>[核心双龙] 2. [名称] ([代码])</h3>
            <p><span class="tag bg-red">🔥 宏观情报与起爆逻辑:</span> (阐述主力炒作意图)</p>
            <p><span class="tag bg-blue">📈 技术面多周期共振:</span> (引用真实数据)</p>
            <p><span class="tag bg-purple">📊 EV估值与筹码测算:</span> (分析筹码或估值)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> (必须明确【预计持股周期(天数)】和【具体止损位(价格)】)</p>
        </div>
        
        <div class="card sub-card">
            <h3>[梯队先锋] 3. [名称] ([代码])</h3>
            <p><span class="tag bg-gray">📉 均线与周期:</span> (结合中期趋势)</p>
            <p><span class="tag bg-green">⚔️ 事件驱动与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> (必须明确【持股周期】及【止损位】)</p>
        </div>
        <div class="card sub-card">
            <h3>[梯队先锋] 4. [名称] ([代码])</h3>
            <p><span class="tag bg-gray">📉 均线与周期:</span> (结合中期趋势)</p>
            <p><span class="tag bg-green">⚔️ 事件驱动与资金:</span> (分析催化剂)</p>
            <p><span class="tag bg-orange">⚠️ 潜伏与风控底线:</span> (必须明确【持股周期】及【止损位】)</p>
        </div>
        
        <div class="card obs-card">
            <h3>⚠️ 筛落组诊断 (Rank 5-10)</h3>
            <ul>
                <li><b>5. [名称]:</b> (说明其硬伤)</li>
                <li><b>6. [名称]:</b> (说明其硬伤)</li>
                <li><b>7. [名称]:</b> (说明其硬伤)</li>
                <li><b>8. [名称]:</b> (说明其硬伤)</li>
                <li><b>9. [名称]:</b> (说明其硬伤)</li>
                <li><b>10. [名称]:</b> (说明其硬伤)</li>
            </ul>
        </div>
    </div>
    
    <div class="card trap-card">
        <h3>🚨 诱多对照组（严禁接盘）</h3>
        <ul>
            <li><b>11. [名称] | <span class="bear-text">诊断：看跌</span></b><br>❌ 诱多技术面：...<br>⚠️ 致命硬伤：...</li>
            <li><b>12. [名称] | <span class="bear-text">诊断：看跌</span></b><br>❌ 诱多技术面：...<br>⚠️ 致命硬伤：...</li>
        </ul>
    </div>
    '''
    
    response = client.models.generate_content(
        model=TARGET_MODEL, 
        contents=prompt, 
        config=types.GenerateContentConfig(temperature=0.1) # 降低温度，只求最精准的最优解
    )
    return response.text.replace("```html", "").replace("```", "").strip()

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
    msg['Subject'], msg['From'] = "【波段内参】全球跨市场 Alpha雷达扫描", f"Alpha Radar <{acc}>"
    msg.attach(MIMEText(html_content, 'html'))
    targets = email_list_str.split(",")
    
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(acc, pwd)
        server.sendmail(acc, targets, msg.as_string()) # Bcc 密送
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
        for item in raw_pool:
            if item['Name'] in ai_html:
                tag = "Avoid_Thunder"
                if re.search(r'\[核心双龙\][^<]*?' + re.escape(item['Name']), ai_html): tag = "Core_Double_Dragon"
                elif re.search(r'\[梯队先锋\][^<]*?' + re.escape(item['Name']), ai_html): tag = "Sub_Pioneer"
                elif re.search(r'(11\.|12\.)[^<]*?' + re.escape(item['Name']), ai_html): tag = "Trap_Warning"
                item['Tag'] = tag
                chosen.append(item)
        
        # 写入历史账本 csv
        log_file = "trade_history.csv"
        with open(log_file, "a", encoding="utf-8") as f:
            if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
                f.write("Date,Ticker,Name,Tag,Industry,Close_Price,Amount,Daily_Pct\n")
            ts = get_bj_time().strftime('%Y-%m-%d')
            for i in chosen: 
                f.write(f"{ts},{i['Ticker']},{i['Name']},{i['Tag']},{i.get('Industry','未知')},{i['Close']},{i['Amount']},{i['Daily_Pct']}\n")
        
        # 【新增】：顺手把发给邮件的 HTML 也在本地存一份
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(full_html)
            
        send_emails(full_html)
