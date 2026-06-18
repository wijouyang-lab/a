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

bj_hour = get_bj_time().hour
if bj_hour < 15:
    print(f"现在是北京时间 {bj_hour} 点，A股尚未收盘，跳过复盘。")
    import sys; sys.exit(0)

TARGET_MODEL = 'claude-opus-4-8'
print("启动 A 股盘后复盘引擎...")

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

import tushare as ts
import re
ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()

start_hist = (get_bj_time() - datetime.timedelta(days=60)).strftime('%Y%m%d')
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

trade_date = get_bj_time().strftime('%Y%m%d')
df_today = pro.daily(trade_date=trade_date)
if df_today is None or df_today.empty:
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    df_today = pro.daily(trade_date=trade_date)
price_map_today = dict(zip(df_today['ts_code'], df_today['close']))


def parse_hold_days(hold_period_str):
    if not hold_period_str or hold_period_str in ['N/A', 'nan', '坚决空仓', '观望']:
        return None
    nums = re.findall(r'\d+', str(hold_period_str))
    if nums:
        return int(nums[-1])
    return None


def get_price_on_date(ticker, target_date_str):
    if df_hist_all.empty:
        return None
    ticker_data = df_hist_all[df_hist_all['ts_code'] == ticker].copy()
    if ticker_data.empty:
        return None
    ticker_data['trade_date'] = pd.to_datetime(ticker_data['trade_date'])
    target_date = pd.to_datetime(target_date_str)
    valid = ticker_data[ticker_data['trade_date'] <= target_date]
    if valid.empty:
        return None
    return float(valid.iloc[-1]['close'])


active_list = []
expired_list = []

for ticker, group in recent_picks.groupby('Ticker'):
    group = group.sort_values('Date')
    first_row = group.iloc[0]
    latest_row = group.iloc[-1]
    days_held = (get_bj_time().replace(tzinfo=None) - first_row['Date']).days

    latest_tag = str(latest_row.get('Tag', '')).strip()
    if latest_tag == 'Trap_Warning':
        print(f"跳过 Trap_Warning: {ticker}")
        continue

    hold_period_str = 'N/A'
    stop_loss = 'N/A'
    score_str = 'N/A'
    for _, r in group.iterrows():
        if str(r.get('Hold_Period', 'N/A')).strip() not in ['N/A', 'nan', '', '坚决空仓']:
            hold_period_str = r['Hold_Period']
            break
    for _, r in group.iterrows():
        if str(r.get('Stop_Loss', 'N/A')).strip() not in ['N/A', 'nan', '', '坚决空仓', '绝对规避', '观望']:
            stop_loss = r['Stop_Loss']
            break
    for _, r in group.iterrows():
        if str(r.get('Score', 'N/A')).strip() not in ['N/A', 'nan', '']:
            score_str = r['Score']
            break

    hold_days = parse_hold_days(hold_period_str)
    if hold_days is None:
        print(f"跳过无持仓周期: {ticker}")
        continue

    rec_price = float(first_row['Close_Price'])
    maturity_date_dt = first_row['Date'] + datetime.timedelta(days=hold_days)
    maturity_date = maturity_date_dt.strftime('%Y-%m-%d')

    if maturity_date_dt.replace(tzinfo=None) <= get_bj_time().replace(tzinfo=None):
        maturity_price = get_price_on_date(ticker, maturity_date)
        maturity_pnl = round(((maturity_price - rec_price) / rec_price) * 100, 2) if maturity_price else None

        expired_list.append({
            "代码": ticker,
            "名称": first_row['Name'],
            "标签": latest_tag,
            "推荐评分": score_str,
            "持股周期建议": hold_period_str,
            "止损价": stop_loss,
            "首次推荐日": first_row['Date'].strftime('%Y-%m-%d'),
            "首次推荐价": rec_price,
            "期满日": maturity_date,
            "期满日价格": maturity_price if maturity_price else "无数据",
            "期满日盈亏(%)": maturity_pnl if maturity_pnl is not None else "无数据",
            "持仓天数": days_held,
            "系统连续推荐次数": len(group),
        })
    else:
        cur_price = price_map_today.get(ticker)
        if not cur_price:
            continue

        cur_pnl = round(((cur_price - rec_price) / rec_price) * 100, 2)
        remaining = (maturity_date_dt.replace(tzinfo=None) - get_bj_time().replace(tzinfo=None)).days

        active_list.append({
            "代码": ticker,
            "名称": first_row['Name'],
            "标签": latest_tag,
            "推荐评分": score_str,
            "持股周期建议": hold_period_str,
            "止损价": stop_loss,
            "首次推荐日": first_row['Date'].strftime('%Y-%m-%d'),
            "首次推荐价": rec_price,
            "现价": cur_price,
            "持仓天数": days_held,
            "剩余天数": remaining,
            "当前盈亏(%)": cur_pnl,
            "系统连续推荐次数": len(group),
        })

print(f"✅ 持仓中: {len(active_list)} 只 | 已超期(本次归档): {len(expired_list)} 只")

if not active_list and not expired_list:
    print("⚠️ 无需复盘的标的，退出。")
    import sys; sys.exit(0)

client = anthropic.Anthropic(
    api_key=os.environ.get("CLAWSOCKET_API_KEY"),
    base_url=os.environ.get("CLAWSOCKET_BASE_URL")
)

prompt = f"""
你是顶级量化风控总监。以下是今日需要复盘的 A 股标的数据：

【持仓中（周期内，需要给出风控指令）】：
{active_list}

【已超期（本次归档，只做策略复盘评价，不需要风控指令）】：
{expired_list}

字段说明：
- 推荐评分：选股引擎当初给出的1-100信心分数（N/A表示该批次还未启用评分系统）
- 首次推荐价：买入成本基准，后续重复推荐不改变此基准
- 持股周期建议：第一次推荐时固定，不被后续推荐覆盖
- 止损价：第一次推荐时设定的具体价格止损位
- 剩余天数：距离期满还有多少天（持仓中才有）
- 期满日盈亏(%)：持股周期到期那天的真实盈亏（已超期才有，这才是策略真实表现）
- 系统连续推荐次数：次数越多说明系统持续看好

在风控判断或策略复盘时，请结合推荐评分进行验证：高分票（80分以上）如果出现明显亏损，需要特别指出"高信心预期未兑现"；低分票（60分以下）如果反而盈利良好，也需要指出"评分体系可能过于保守"。这类反差信息对优化评分标准很有价值。

请严格按以下 HTML 骨架输出复盘报告（直出HTML，禁加markdown框，盈利标红，亏损标绿）：

<div style="background: #eceff1; border-left: 6px solid #455a64; padding: 20px; margin-bottom: 25px; border-radius: 8px;">
    <h3 style="margin-top: 0; color: #263238;">⚖️ 盘后总体风控审查</h3>
    <p>(总结持仓中标的整体盈亏状况，以及已超期标的的策略胜率评估，特别指出评分与实际表现是否存在明显反差)</p>
</div>

<h2 style="color: #1565c0; border-bottom: 2px solid #1565c0; padding-bottom: 5px;">📊 持仓中 - 风控纪律核对单</h2>
<div style="background: #fff; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
    <h3 style="margin: 0 0 10px 0;">[首次推荐日] | [股票名称] ([代码]) | 评分[推荐评分]/100 | 系统连续推荐[N]次 | 还剩[剩余天数]天到期</h3>
    <p><b>持股周期建议:</b> [持股周期建议] | <b>止损位:</b> [止损价]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] ➔ <b>现价:</b> ¥[现价] | <b>当前盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[当前盈亏(%)]%</span></p>
    <p><span style="background: #607d8b; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">风控动作指令</span>
    (判断：现价是否跌破止损位？系统是否仍在持续推荐？当初评分是否与现状吻合？给出持有/止损/减仓指令)</p>
</div>

<h2 style="color: #37474f; border-bottom: 2px solid #cfd8dc; padding-bottom: 5px; margin-top: 40px;">📁 已超期归档 - 策略复盘评价</h2>
<div style="background: #f5f5f5; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0;">
    <h3 style="margin: 0 0 10px 0;">[首次推荐日] | [股票名称] ([代码]) | 评分[推荐评分]/100 | 期满日:[期满日]</h3>
    <p><b>持股周期建议:</b> [持股周期建议] | <b>止损位:</b> [止损价]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] → <b>期满日价格:</b> ¥[期满日价格] | <b>策略实际盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[期满日盈亏(%)]%</span></p>
    <p><span style="background: #455a64; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">策略复盘</span>
    (评价这次策略是否成功，归因分析盈亏原因，明确点评评分与实际结果是否吻合，此票不再追踪)</p>
</div>
"""

ai_html = ""
with client.messages.stream(
    model=TARGET_MODEL,
    max_tokens=4000,
    temperature=0.1,
    messages=[{"role": "user", "content": prompt}]
) as stream:
    for text in stream.text_stream:
        ai_html += text

ai_html = ai_html.replace("```html", "").replace("```", "").strip()

# ==========================================
# 写入 review_history.csv（含 Score 列自动迁移）
# ==========================================
review_log = "review_history.csv"
new_header = "Review_Date,Ticker,Name,Tag,Rec_Date,Rec_Price,Cur_Price,Days_Held,PnL_Pct,Maturity_PnL,Hold_Period,Stop_Loss,Rec_Count,Status,Score\n"
review_file_exists = os.path.exists(review_log) and os.path.getsize(review_log) > 0
review_need_header = not review_file_exists

if review_file_exists:
    with open(review_log, "r", encoding="utf-8") as f:
        review_lines = f.readlines()
    if review_lines and "Score" not in review_lines[0]:
        review_lines[0] = new_header
        with open(review_log, "w", encoding="utf-8") as f:
            f.writelines(review_lines)
        print("⚠️ 检测到旧版review_history.csv缺少Score列，已自动升级表头（历史行Score将显示为空，不影响读取）")

try:
    with open(review_log, "a", encoding="utf-8") as f:
        if review_need_header:
            f.write(new_header)
        review_date = get_bj_time().strftime('%Y-%m-%d')

        for item in active_list:
            f.write(f"{review_date},{item['代码']},{item['名称']},{item['标签']},{item['首次推荐日']},{item['首次推荐价']},{item['现价']},{item['持仓天数']},{item['当前盈亏(%)']},,{item['持股周期建议']},{item['止损价']},{item['系统连续推荐次数']},持仓中,{item['推荐评分']}\n")

        for item in expired_list:
            maturity_pnl = item['期满日盈亏(%)'] if item['期满日盈亏(%)'] != "无数据" else ""
            f.write(f"{review_date},{item['代码']},{item['名称']},{item['标签']},{item['首次推荐日']},{item['首次推荐价']},{item['期满日价格']},{item['持仓天数']},{maturity_pnl},{maturity_pnl},{item['持股周期建议']},{item['止损价']},{item['系统连续推荐次数']},已超期归档,{item['推荐评分']}\n")

    print("✅ 复盘结果已写入 review_history.csv")
except Exception as e:
    print(f"⚠️ 复盘写入失败: {e}")

# ==========================================
# 发送邮件（只发给仓库主人，读取 OWNER_EMAIL Secret）
# ==========================================
style = "body{font-family:sans-serif; background:#f4f6f9; padding:20px; color:#333; line-height:1.6} .container{max-width:900px; margin:0 auto; background:#fff; padding:30px; border-radius:10px; box-shadow:0 4px 15px rgba(0,0,0,0.05)}"
full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{style}</style></head><body><div class='container'><h1 style='color:#37474f; text-align:center;'>Alpha 雷达 A股盘后复盘</h1>{ai_html}</div></body></html>"


def send_mail():
    acc = os.environ.get("EMAIL_ACCOUNT")
    pwd = os.environ.get("EMAIL_PASSWORD")
    owner_email = os.environ.get("OWNER_EMAIL")
    if not acc or not pwd or not owner_email:
        print("⚠️ 邮箱配置缺失（需要 EMAIL_ACCOUNT、EMAIL_PASSWORD、OWNER_EMAIL），跳过发送。")
        return

    msg = MIMEMultipart()
    msg['From'] = acc
    msg['To'] = owner_email
    msg['Subject'] = f"【盘后清算】A股风控纪律与复盘 ({get_bj_time().strftime('%Y-%m-%d')})"
    msg.attach(MIMEText(full_html, 'html'))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(acc, pwd)
            s.sendmail(acc, [owner_email], msg.as_string())
            print(f"✅ 复盘报告已私密发送至仓库主人！")
    except Exception as e:
        print(f"❌ 发送失败: {e}")


send_mail()
