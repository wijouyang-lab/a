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
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

if get_bj_time().weekday() >= 5:
    print("🚨 周末休市，退出复盘。")
    import sys; sys.exit(0)

TARGET_MODEL = 'gemini-3.1-pro-preview'
print("🔍 启动 A 股盘后复盘引擎 (Review Engine)...")

# ==========================================
# 1. 读取账本 (包含最新的持股周期和止损价)
# ==========================================
log_file = "trade_history.csv"
if not os.path.exists(log_file):
    print("⚠️ 尚未生成交易账本，跳过复盘。")
    import sys; sys.exit(0)

try:
    df = pd.read_csv(log_file)
    df['Date'] = pd.to_datetime(df['Date'])
    # 只看最近 7 天的票，符合波段操作逻辑
    cutoff_date = get_bj_time() - datetime.timedelta(days=7)
    recent_picks = df[df['Date'] >= cutoff_date.replace(tzinfo=None)].copy()
    if recent_picks.empty:
        print("⚠️ 近期无操作记录，跳过。")
        import sys; sys.exit(0)
except Exception as e:
    print(f"⚠️ 账本读取失败: {e}")
    import sys; sys.exit(1)

# ==========================================
# 2. 获取 A 股最新现价 (Tushare)
# ==========================================
import tushare as ts
ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()

trade_date = get_bj_time().strftime('%Y%m%d')
df_daily = pro.daily(trade_date=trade_date)
if df_daily is None or df_daily.empty:
    # 遇到节假日或尚未出数据，取前一交易日
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    df_daily = pro.daily(trade_date=trade_date)

price_map = dict(zip(df_daily['ts_code'], df_daily['close']))

review_data = []
for index, row in recent_picks.iterrows():
    ticker = row['Ticker']
    rec_price = float(row['Close_Price'])
    cur_price = price_map.get(ticker)
    
    if cur_price:
        pnl_pct = ((cur_price - rec_price) / rec_price) * 100
        # 计算持仓天数
        days_held = (get_bj_time().replace(tzinfo=None) - row['Date']).days
        
        review_data.append({
            "推荐日期": row['Date'].strftime('%Y-%m-%d'),
            "代码": ticker,
            "名称": row['Name'],
            "标签": row['Tag'],
            "持股周期": str(row.get('Hold_Period', 'N/A')),
            "止损价": str(row.get('Stop_Loss', 'N/A')),
            "已持仓天数": days_held,
            "推荐价": rec_price,
            "现价": cur_price,
            "盈亏": round(pnl_pct, 2)
        })

if not review_data:
    print("⚠️ 未匹配到最新价格数据，退出。")
    import sys; sys.exit(0)

# ==========================================
# 3. Gemini 纪律审判
# ==========================================
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
prompt = f"""
你是顶级量化风控总监。以下是系统近几日推荐的 A 股标的及当前真实数据（包含预计持股周期和止损价）：
{review_data}

请严格按以下 HTML 骨架输出复盘报告（直出HTML，禁加markdown框，看涨/盈利标红，看跌/亏损标绿）：

<div style="background: #eceff1; border-left: 6px solid #455a64; padding: 20px; margin-bottom: 25px; border-radius: 8px;">
    <h3 style="margin-top: 0; color: #263238;">⚖️ 盘后总体风控审查</h3>
    <p>(根据整体盈亏和时间窗口，总结近期策略执行情况，指出是否受到大盘 Beta 拖累)</p>
</div>

<h2 style="color: #37474f; border-bottom: 2px solid #cfd8dc; padding-bottom: 5px;">📊 持仓风控纪律核对单</h2>
<div style="background: #fff; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
    <h3 style="margin: 0 0 10px 0;">[推演日期] | [股票名称] ([代码])</h3>
    <p><b>预计周期:</b> [持股周期] (已持仓 [已持仓天数] 天) | <b>止损位:</b> [止损价]</p>
    <p><b>推荐价:</b> ¥[价格] ➔ <b>现价:</b> ¥[价格] | <b>实际盈亏:</b> <span style="font-weight: bold; color: [盈利填#d32f2f, 亏损填#388e3c];">[PnL]%</span></p>
    <p><span style="background: #607d8b; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">风控动作指令</span> (基于现价是否跌破止损位，或者已持仓天数是否超过预期，给出最冷血的纪律应对：如“触发止损无条件出局”、“持股周期内正常洗盘”、“达到预期建议止盈”)</p>
</div>
"""
try:
    res = client.models.generate_content(model=TARGET_MODEL, contents=prompt, config=types.GenerateContentConfig(temperature=0.1))
    ai_html = res.text.replace("```html", "").replace("```", "").strip()
except Exception as e:
    ai_html = f"<p>复盘生成失败: {e}</p>"

# ==========================================
# 4. 发送绝密邮件
# ==========================================
style = "body{font-family:sans-serif; background:#f4f6f9; padding:20px; color:#333; line-height:1.6} .container{max-width:900px; margin:0 auto; background:#fff; padding:30px; border-radius:10px; box-shadow:0 4px 15px rgba(0,0,0,0.05)}"
full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{style}</style></head><body><div class='container'><h1 style='color:#37474f; text-align:center;'>🛡️ Alpha 雷达 A股盘后复盘</h1>{ai_html}</div></body></html>"

def send_mail():
    acc, pwd = os.environ.get("EMAIL_ACCOUNT"), os.environ.get("EMAIL_PASSWORD")
    email_list_str = os.environ.get("TARGET_EMAILS")
    if not acc or not email_list_str: return
    targets = [e.strip() for e in email_list_str.split(",")]
    
    msg = MIMEMultipart(); msg['From'] = acc; msg['Subject'] = f"🛡️【盘后清算】A股风控纪律与复盘 ({get_bj_time().strftime('%Y-%m-%d')})"
    msg.attach(MIMEText(full_html, 'html'))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(acc, pwd)
            s.sendmail(acc, targets, msg.as_string())
            print("✅ 复盘报告密送成功！")
    except Exception as e: print(f"❌ 发送失败: {e}")

send_mail()
