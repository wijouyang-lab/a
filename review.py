# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

if get_bj_time().weekday() >= 5:
    print("周末休市，退出复盘。")
    import sys; sys.exit(0)

TARGET_MODEL = 'claude-sonnet-4-6'
print("启动 A 股盘后复盘引擎...")

# ==========================================
# 1. 读取账本
# ==========================================
log_file = "trade_history.csv"
if not os.path.exists(log_file):
    print("⚠️ 尚未生成交易账本，跳过复盘。")
    import sys; sys.exit(0)

try:
    df = pd.read_csv(log_file)
    df['Date'] = pd.to_datetime(df['Date'])
    cutoff_date = get_bj_time() - datetime.timedelta(days=14)
    recent_picks = df[df['Date'] >= cutoff_date.replace(tzinfo=None)].copy()
    if recent_picks.empty:
        print("⚠️ 近期无操作记录，跳过。")
        import sys; sys.exit(0)
except Exception as e:
    print(f"⚠️ 账本读取失败: {e}")
    import sys; sys.exit(1)

# ==========================================
# 2. 按票聚合，保留完整持仓轨迹摘要
# ==========================================
summary_list = []
for ticker, group in recent_picks.groupby('Ticker'):
    group = group.sort_values('Date')
    first_row = group.iloc[0]   # 最早推荐（买入成本基准）
    latest_row = group.iloc[-1] # 最新状态
    days_held = (get_bj_time().replace(tzinfo=None) - first_row['Date']).days
    summary_list.append({
        "代码": ticker,
        "名称": first_row['Name'],
        "标签": latest_row['Tag'],
        "持股周期": str(latest_row.get('Hold_Period', 'N/A')),
        "止损价": str(latest_row.get('Stop_Loss', 'N/A')),
        "首次推荐日": first_row['Date'].strftime('%Y-%m-%d'),
        "首次推荐价": float(first_row['Close_Price']),
        "持仓天数": days_held,
        "系统连续推荐次数": len(group),
    })

if not summary_list:
    print("⚠️ 聚合后无数据，退出。")
    import sys; sys.exit(0)

summary_df = pd.DataFrame(summary_list)

# ==========================================
# 3. 获取 A 股最新现价 (Tushare)
# ==========================================
import tushare as ts
ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()

trade_date = get_bj_time().strftime('%Y%m%d')
df_daily = pro.daily(trade_date=trade_date)
if df_daily is None or df_daily.empty:
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    df_daily = pro.daily(trade_date=trade_date)

price_map = dict(zip(df_daily['ts_code'], df_daily['close']))

# ==========================================
# 4. 合并现价，计算真实盈亏
# ==========================================
review_data = []
for item in summary_list:
    ticker = item['代码']
    rec_price = item['首次推荐价']
    cur_price = price_map.get(ticker)

    if cur_price:
        pnl_pct = ((cur_price - rec_price) / rec_price) * 100
        review_data.append({
            "代码": ticker,
            "名称": item['名称'],
            "标签": item['标签'],
            "持股周期": item['持股周期'],
            "止损价": item['止损价'],
            "首次推荐日": item['首次推荐日'],
            "首次推荐价": rec_price,
            "现价": cur_price,
            "持仓天数": item['持仓天数'],
            "系统连续推荐次数": item['系统连续推荐次数'],
            "盈亏(%)": round(pnl_pct, 2)
        })

if not review_data:
    print("⚠️ 未匹配到最新价格数据，退出。")
    import sys; sys.exit(0)

print(f"✅ 共复盘 {len(review_data)} 只标的")

# ==========================================
# 5. Claude 纪律审判
# ==========================================
client = anthropic.Anthropic(
    api_key=os.environ.get("CLAWSOCKET_API_KEY"),
    base_url=os.environ.get("CLAWSOCKET_BASE_URL")
)

prompt = f"""
你是顶级量化风控总监。以下是系统近14天推荐的 A 股标的持仓摘要及当前真实盈亏数据：
{review_data}

字段说明：
- 首次推荐价：系统第一次推荐时的价格，即买入成本基准
- 持仓天数：从首次推荐到今天的实际天数
- 系统连续推荐次数：这只票被系统连续选中的天数，次数越多说明系统持续看好
- 止损价：当时设定的止损位
- 持股周期：建议的持仓时间窗口

请严格按以下 HTML 骨架输出复盘报告（直出HTML，禁加markdown框，看涨/盈利标红，看跌/亏损标绿）：

<div style="background: #eceff1; border-left: 6px solid #455a64; padding: 20px; margin-bottom: 25px; border-radius: 8px;">
    <h3 style="margin-top: 0; color: #263238;">⚖️ 盘后总体风控审查</h3>
    <p>(总结整体胜率和盈亏情况，指出是否受大盘Beta拖累，以及哪些票出现洗盘特征)</p>
</div>

<h2 style="color: #37474f; border-bottom: 2px solid #cfd8dc; padding-bottom: 5px;">📊 持仓风控纪律核对单</h2>
<div style="background: #fff; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
    <h3 style="margin: 0 0 10px 0;">[首次推荐日] | [股票名称] ([代码]) | 系统连续推荐[N]次</h3>
    <p><b>持股周期:</b> [持股周期] (已持仓 [持仓天数] 天) | <b>止损位:</b> [止损价]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] ➔ <b>现价:</b> ¥[现价] | <b>实际盈亏:</b> <span style="font-weight: bold; color: [盈利填#d32f2f, 亏损填#388e3c];">[盈亏]%</span></p>
    <p><span style="background: #607d8b; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">风控动作指令</span>
    (综合判断：1.现价是否跌破止损位 2.持仓天数是否超出周期 3.系统连续推荐次数是否说明仍有强度
    给出明确指令：如"触发止损无条件出局"、"持股周期内疑似洗盘可继续持有"、"超出持股周期建议止盈离场")</p>
</div>
"""

try:
    message = client.messages.create(
        model=TARGET_MODEL,
        max_tokens=3000,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}]
    )
    ai_html = message.content[0].text.replace("```html", "").replace("```", "").strip()
except Exception as e:
    ai_html = f"<p>复盘生成失败: {e}</p>"

# ==========================================
# 6. 复盘结果写入 review_history.csv
# ==========================================
review_log = "review_history.csv"
need_header = not os.path.exists(review_log) or os.path.getsize(review_log) == 0
try:
    with open(review_log, "a", encoding="utf-8") as f:
        if need_header:
            f.write("Review_Date,Ticker,Name,Tag,Rec_Date,Rec_Price,Cur_Price,Days_Held,PnL_Pct,Hold_Period,Stop_Loss,Rec_Count\n")
        review_date = get_bj_time().strftime('%Y-%m-%d')
        for item in review_data:
            f.write(f"{review_date},{item['代码']},{item['名称']},{item['标签']},{item['首次推荐日']},{item['首次推荐价']},{item['现价']},{item['持仓天数']},{item['盈亏(%)']},{item['持股周期']},{item['止损价']},{item['系统连续推荐次数']}\n")
    print("✅ 复盘结果已写入 review_history.csv")
except Exception as e:
    print(f"⚠️ 复盘写入失败: {e}")

# ==========================================
# 7. 发送邮件
# ==========================================
style = "body{font-family:sans-serif; background:#f4f6f9; padding:20px; color:#333; line-height:1.6} .container{max-width:900px; margin:0 auto; background:#fff; padding:30px; border-radius:10px; box-shadow:0 4px 15px rgba(0,0,0,0.05)}"
full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{style}</style></head><body><div class='container'><h1 style='color:#37474f; text-align:center;'>Alpha 雷达 A股盘后复盘</h1>{ai_html}</div></body></html>"

def send_mail():
    acc, pwd = os.environ.get("EMAIL_ACCOUNT"), os.environ.get("EMAIL_PASSWORD")
    email_list_str = os.environ.get("TARGET_EMAILS")
    if not acc or not email_list_str: return
    targets = [e.strip() for e in email_list_str.split(",")]

    msg = MIMEMultipart()
    msg['From'] = acc
    msg['Subject'] = f"【盘后清算】A股风控纪律与复盘 ({get_bj_time().strftime('%Y-%m-%d')})"
    msg.attach(MIMEText(full_html, 'html'))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(acc, pwd)
            s.sendmail(acc, targets, msg.as_string())
            print("✅ 复盘报告密送成功！")
    except Exception as e:
        print(f"❌ 发送失败: {e}")

send_mail()
