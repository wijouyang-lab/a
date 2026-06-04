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

# 时间检查：只在北京时间 15:00 之后执行复盘
bj_hour = get_bj_time().hour
if bj_hour < 15:
    print(f"现在是北京时间 {bj_hour} 点，A股尚未收盘，跳过复盘。")
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
    cutoff_date = get_bj_time() - datetime.timedelta(days=30)
    recent_picks = df[df['Date'] >= cutoff_date.replace(tzinfo=None)].copy()
    if recent_picks.empty:
        print("⚠️ 近期无操作记录，跳过。")
        import sys; sys.exit(0)
except Exception as e:
    print(f"⚠️ 账本读取失败: {e}")
    import sys; sys.exit(1)

# ==========================================
# 2. 按票聚合，提取持股周期
# ==========================================
import tushare as ts
ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()

# 拉取最近30天的历史价格用于查询期满日价格
start_hist = (get_bj_time() - datetime.timedelta(days=35)).strftime('%Y%m%d')
end_hist = get_bj_time().strftime('%Y%m%d')
all_tickers = recent_picks['Ticker'].unique().tolist()

try:
    df_hist_all = pro.daily(
        ts_code=",".join(all_tickers),
        start_date=start_hist,
        end_date=end_hist
    ).sort_values(['ts_code', 'trade_date'])
except Exception as e:
    print(f"⚠️ 历史价格拉取失败: {e}")
    df_hist_all = pd.DataFrame()

# 获取今日现价
trade_date = get_bj_time().strftime('%Y%m%d')
df_today = pro.daily(trade_date=trade_date)
if df_today is None or df_today.empty:
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    df_today = pro.daily(trade_date=trade_date)
price_map_today = dict(zip(df_today['ts_code'], df_today['close']))

def parse_hold_days(hold_period_str):
    """从持股周期字符串提取最大天数，如'5-12天'返回12，'10天'返回10"""
    if not hold_period_str or hold_period_str in ['N/A', 'nan', '坚决空仓', '观望']:
        return None
    import re
    nums = re.findall(r'\d+', str(hold_period_str))
    if nums:
        return int(nums[-1])  # 取最大值
    return None

def get_price_on_date(ticker, target_date_str):
    """获取某只票在指定日期或最近交易日的收盘价"""
    if df_hist_all.empty:
        return None
    ticker_data = df_hist_all[df_hist_all['ts_code'] == ticker].copy()
    if ticker_data.empty:
        return None
    ticker_data['trade_date'] = pd.to_datetime(ticker_data['trade_date'])
    target_date = pd.to_datetime(target_date_str)
    # 找到目标日期或之前最近的交易日
    valid = ticker_data[ticker_data['trade_date'] <= target_date]
    if valid.empty:
        return None
    return float(valid.iloc[-1]['close'])

summary_list = []
for ticker, group in recent_picks.groupby('Ticker'):
    group = group.sort_values('Date')
    first_row = group.iloc[0]
    latest_row = group.iloc[-1]
    days_held = (get_bj_time().replace(tzinfo=None) - first_row['Date']).days

    # 优先取非 N/A 的周期和止损
    hold_period_str = 'N/A'
    stop_loss = 'N/A'
    for _, r in group.iterrows():
        if str(r.get('Hold_Period', 'N/A')).strip() not in ['N/A', 'nan', '']:
            hold_period_str = r['Hold_Period']
        if str(r.get('Stop_Loss', 'N/A')).strip() not in ['N/A', 'nan', '', '坚决空仓', '绝对规避']:
            stop_loss = r['Stop_Loss']

    rec_price = float(first_row['Close_Price'])
    cur_price = price_map_today.get(ticker)
    if not cur_price:
        continue

    # 计算当前盈亏
    cur_pnl = round(((cur_price - rec_price) / rec_price) * 100, 2)

    # 计算持股周期到期日价格和盈亏
    hold_days = parse_hold_days(hold_period_str)
    maturity_date = None
    maturity_price = None
    maturity_pnl = None
    period_status = "持仓中"

    if hold_days:
        maturity_date = (first_row['Date'] + datetime.timedelta(days=hold_days)).strftime('%Y-%m-%d')
        maturity_date_dt = first_row['Date'] + datetime.timedelta(days=hold_days)

        if maturity_date_dt.replace(tzinfo=None) <= get_bj_time().replace(tzinfo=None):
            # 已过期满日
            maturity_price = get_price_on_date(ticker, maturity_date)
            if maturity_price:
                maturity_pnl = round(((maturity_price - rec_price) / rec_price) * 100, 2)
            period_status = f"已超期({days_held}天/{hold_days}天)"
        else:
            remaining = (maturity_date_dt.replace(tzinfo=None) - get_bj_time().replace(tzinfo=None)).days
            period_status = f"持仓中(还剩{remaining}天)"

    summary_list.append({
        "代码": ticker,
        "名称": first_row['Name'],
        "标签": latest_row['Tag'],
        "持股周期建议": hold_period_str,
        "止损价": stop_loss,
        "首次推荐日": first_row['Date'].strftime('%Y-%m-%d'),
        "首次推荐价": rec_price,
        "现价": cur_price,
        "持仓天数": days_held,
        "周期状态": period_status,
        "当前盈亏(%)": cur_pnl,
        "期满日": maturity_date if maturity_date else "未到期",
        "期满日价格": maturity_price if maturity_price else "未到期",
        "期满日盈亏(%)": maturity_pnl if maturity_pnl is not None else "未到期",
        "系统连续推荐次数": len(group),
    })

if not summary_list:
    print("⚠️ 聚合后无数据，退出。")
    import sys; sys.exit(0)

print(f"✅ 共复盘 {len(summary_list)} 只标的")

# ==========================================
# 3. Claude 纪律审判
# ==========================================
client = anthropic.Anthropic(
    api_key=os.environ.get("CLAWSOCKET_API_KEY"),
    base_url=os.environ.get("CLAWSOCKET_BASE_URL")
)

prompt = f"""
你是顶级量化风控总监。以下是系统近30天推荐的 A 股标的持仓摘要及当前真实盈亏数据：
{summary_list}

字段说明：
- 首次推荐价：系统第一次推荐时的价格，即买入成本基准
- 持仓天数：从首次推荐到今天的实际天数
- 持股周期建议：当时系统建议的持仓时间窗口
- 周期状态：当前是否在周期内，还剩多少天，或已超期多少天
- 期满日价格：持股周期到期那天的实际收盘价（如已过期）
- 期满日盈亏(%)：持股周期到期时的实际盈亏（如已过期，这才是策略的真实表现）
- 当前盈亏(%)：今日现价对比买入成本的盈亏
- 系统连续推荐次数：这只票被系统连续选中的天数

请严格按以下 HTML 骨架输出复盘报告（直出HTML，禁加markdown框，盈利标红，亏损标绿）：

<div style="background: #eceff1; border-left: 6px solid #455a64; padding: 20px; margin-bottom: 25px; border-radius: 8px;">
    <h3 style="margin-top: 0; color: #263238;">⚖️ 盘后总体风控审查</h3>
    <p>(总结整体胜率，分别统计周期内和已超期的票各自的胜率，指出是否受大盘Beta拖累)</p>
</div>

<h2 style="color: #37474f; border-bottom: 2px solid #cfd8dc; padding-bottom: 5px;">📊 持仓风控纪律核对单</h2>
<div style="background: #fff; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
    <h3 style="margin: 0 0 10px 0;">[首次推荐日] | [股票名称] ([代码]) | 系统连续推荐[N]次 | [周期状态]</h3>
    <p><b>持股周期建议:</b> [持股周期建议] | <b>止损位:</b> [止损价]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] ➔ <b>现价:</b> ¥[现价] | <b>当前盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[当前盈亏(%)]%</span></p>
    <p>（如已过期）<b>期满日:</b> [期满日] | <b>期满价:</b> ¥[期满日价格] | <b>策略实际盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[期满日盈亏(%)]%</span></p>
    <p><span style="background: #607d8b; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">风控动作指令</span>
    (综合判断：
    1. 如周期内：现价是否跌破止损位？系统是否仍在持续推荐？给出持有/止损/观望指令
    2. 如已超期：期满日盈亏是否达标？当前是否应该已出场？给出复盘评价和后续建议)</p>
</div>
"""

try:
    message = client.messages.create(
        model=TARGET_MODEL,
        max_tokens=4000,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}]
    )
    ai_html = message.content[0].text.replace("```html", "").replace("```", "").strip()
except Exception as e:
    ai_html = f"<p>复盘生成失败: {e}</p>"

# ==========================================
# 4. 复盘结果写入 review_history.csv
# ==========================================
review_log = "review_history.csv"
need_header = not os.path.exists(review_log) or os.path.getsize(review_log) == 0
try:
    with open(review_log, "a", encoding="utf-8") as f:
        if need_header:
            f.write("Review_Date,Ticker,Name,Tag,Rec_Date,Rec_Price,Cur_Price,Days_Held,PnL_Pct,Maturity_PnL,Hold_Period,Stop_Loss,Rec_Count,Period_Status\n")
        review_date = get_bj_time().strftime('%Y-%m-%d')
        for item in summary_list:
            maturity_pnl = item['期满日盈亏(%)'] if item['期满日盈亏(%)'] != "未到期" else ""
            f.write(f"{review_date},{item['代码']},{item['名称']},{item['标签']},{item['首次推荐日']},{item['首次推荐价']},{item['现价']},{item['持仓天数']},{item['当前盈亏(%)']},{maturity_pnl},{item['持股周期建议']},{item['止损价']},{item['系统连续推荐次数']},{item['周期状态']}\n")
    print("✅ 复盘结果已写入 review_history.csv")
except Exception as e:
    print(f"⚠️ 复盘写入失败: {e}")

# ==========================================
# 5. 发送邮件
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
