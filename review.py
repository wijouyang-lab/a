# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import types

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time(): return datetime.datetime.now(BEIJING_TZ)

if get_bj_time().weekday() >= 5: sys.exit(0)

def do_review():
    import tushare as ts
    ts.set_token(os.environ.get("TUSHARE_TOKEN"))
    pro = ts.pro_api()
    today_dash = get_bj_time().strftime('%Y-%m-%d')
    if not os.path.exists("trade_history.csv"): return None
    
    df = pd.read_csv("trade_history.csv")
    df_today = df[(df['Date'] == today_dash) & (df['Tag'].isin(['Core_Double_Dragon', 'Sub_Pioneer']))]
    if df_today.empty: return None
    
    codes_str = ",".join(df_today['Ticker'].tolist())
    df_actual = pro.daily(ts_code=codes_str, trade_date=get_bj_time().strftime('%Y%m%d'))
    if df_actual.empty: return None

    review_data = []
    for _, row in df_today.iterrows():
        actual_row = df_actual[df_actual['ts_code'] == row['Ticker']]
        pct = actual_row['pct_chg'].values[0] if not actual_row.empty else 0.0
        review_data.append({
            "股票": row['Name'], "代码": row['Ticker'], "板块": row['Industry'],
            "早盘前收盘价": row['Close_Price'], "今日收盘价": actual_row['close'].values[0] if not actual_row.empty else 0.0,
            "今日真实涨跌幅(%)": pct, "判定结果": "成功" if pct > 0 else "失败"
        })
    return review_data

def ai_reflect(data):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    prompt = f'''你是量化风控总监。今天是{get_bj_time().strftime('%Y年%m月%d日')}收盘。今日实盘结果：{data}
    请输出HTML复盘（严格按此骨架）：
    <div style="background:#2c3e50;color:white;padding:20px;border-radius:8px"><h2>🔬 盘后归因与进化报告</h2><p>今日战绩：...</p></div>
    <div style="background:#fff;padding:20px;border-left:6px solid #d32f2f;margin-top:20px"><h3>📈 核心胜率诊断</h3><p>...</p></div>
    <div style="background:#f1f8e9;padding:20px;border-left:6px solid #388e3c;margin-top:20px"><h3>🩸 败局深度反思</h3><p>...</p></div>
    <div style="background:#f9f0ff;padding:20px;border-left:6px solid #8e44ad;margin-top:20px"><h3>⚙️ 明日进化指南</h3><p>...</p></div>'''
    res = client.models.generate_content(model='gemini-3.1-pro-preview', contents=prompt, config=types.GenerateContentConfig(temperature=0.3))
    return res.text.replace("```html", "").replace("```", "").strip()

def send_review(html):
    acc, pwd = os.environ.get("EMAIL_ACCOUNT"), os.environ.get("EMAIL_PASSWORD")
    if not acc or not pwd: return
    msg = MIMEMultipart()
    msg['Subject'], msg['From'] = "【盘后反思】Alpha Radar 进化报告", f"Alpha Radar <{acc}>"
    msg.attach(MIMEText(html, 'html'))
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(acc, pwd)
        server.sendmail(acc, ["907359319@qq.com", "minyongoy@live.cn", "rudi25581148@163.com", "501158937@qq.com"], msg.as_string())
        server.quit()
    except Exception as e: pass

if __name__ == "__main__":
    data = do_review()
    if data: send_review(ai_reflect(data))
