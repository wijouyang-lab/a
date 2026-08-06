# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

# 启动前置校验：AI 凭证（缺失则立即报错退出，避免跑完前面的复盘数据整理逻辑后才在AI调用阶段崩溃）
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("致命错误：未检测到环境变量 ANTHROPIC_API_KEY！请检查 GitHub Actions 仓库的 Secrets 配置（Settings → Secrets and variables → Actions），并确认 workflow yml 中已通过 env: 正确传递。")
    import sys; sys.exit(1)

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

# ==========================================
# 【新增】补充A股成交记录（从盘前待确认文件）
# ==========================================
def supplement_ashare_stocks_from_pending():
    """
    ✅ 【改动】A股版review.py特有函数
    查找并读取盘前 scan_ashare.py 生成的A股待确认文件 ashare_stocks_pending_[YYYYMMDD].csv
    用盘后的完整行情数据补充写入 trade_history.csv
    
    原因：确保有完整的收盘数据再记账
    """
    log_file = "trade_history.csv"
    
    today_date_str = get_bj_time().strftime('%Y-%m-%d')
    today_date_file = get_bj_time().strftime('%Y%m%d')
    
    pending_file = f"ashare_stocks_pending_{today_date_file}.csv"
    
    if not os.path.exists(pending_file):
        print(f"📋 [盘后补充] 未发现A股待确认文件 {pending_file}，跳过A股补充。")
        return
    
    print(f"📋 [盘后补充] 发现A股待确认文件 {pending_file}，开始用盘后完整数据补充...")
    
    try:
        import tushare as ts
        
        # 读取待确认文件
        df_pending = pd.read_csv(pending_file)
        
        if df_pending.empty:
            print(f"⚠️ 待确认文件为空，跳过。")
            return
        
        print(f"📡 [盘后补充] 正在拉取A股盘后完整行情数据...")
        
        # 拉取盘后完整数据
        ts_pro = ts.pro_api()
        today_trade_date = get_bj_time().strftime('%Y%m%d')
        
        # 尝试获取今日数据，如果没有则获取前一交易日
        df_prices = None
        for offset in range(0, 5):
            try_date = (get_bj_time() - datetime.timedelta(days=offset)).strftime('%Y%m%d')
            try:
                df_prices = ts_pro.daily(trade_date=try_date)
                if df_prices is not None and not df_prices.empty:
                    today_trade_date = try_date
                    break
            except:
                pass
        
        if df_prices is None or df_prices.empty:
            print(f"⚠️ 无法获取行情数据，将使用推荐价格作为成交价")
            df_prices = pd.DataFrame()
        
        # 构建价格映射
        price_map = {}
        if not df_prices.empty:
            price_map = dict(zip(df_prices['ts_code'], df_prices['close']))
        
        # 读取现有账本
        df_existing = pd.DataFrame()
        if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
            df_existing = pd.read_csv(log_file, on_bad_lines='skip')
            df_existing['Date'] = pd.to_datetime(df_existing['Date'])
        
        new_ashare_records = []
        
        for _, row in df_pending.iterrows():
            ticker = row['Ticker']
            
            # 检查重复
            if not df_existing.empty:
                existing = df_existing[
                    (df_existing['Date'] == pd.to_datetime(today_date_str)) &
                    (df_existing['Ticker'] == ticker)
                ]
                if not existing.empty:
                    print(f"⏭️ {ticker} 已在账本中，跳过重复")
                    continue
            
            # 使用盘后收盘价
            close_price = price_map.get(ticker, row['Recommended_Price'])
            
            try:
                rec_price = float(row['Recommended_Price'])
                pct_chg = round((close_price - rec_price) / rec_price * 100, 2)
            except:
                pct_chg = 0.0
            
            new_ashare_records.append({
                'Date': today_date_str,
                'Ticker': ticker,
                'Name': row['Name'],
                'Tag': row['Tag'],
                'Industry': row['Industry'],
                'Close_Price': close_price,
                'Amount': row['Amount'],
                'Daily_Pct': pct_chg,
                'Hold_Period': row['Hold_Period'],
                'Stop_Loss': row['Stop_Loss'],
                'Score': row['Score']
            })
        
        if new_ashare_records:
            # 新建或追加写入
            new_header = "Date,Ticker,Name,Tag,Industry,Close_Price,Amount,Daily_Pct,Hold_Period,Stop_Loss,Score\n"
            need_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0
            
            with open(log_file, "a", encoding="utf-8") as f:
                if need_header:
                    f.write(new_header)
                for record in new_ashare_records:
                    f.write(f"{record['Date']},{record['Ticker']},{record['Name']},{record['Tag']},{record['Industry']},{record['Close_Price']},{record['Amount']},{record['Daily_Pct']},{record['Hold_Period']},{record['Stop_Loss']},{record['Score']}\n")
            
            print(f"✅ [盘后补充] 成功补充A股成交记录 {len(new_ashare_records)} 条（使用盘后收盘价）")
            
            # 备份待确认文件
            processed_file = f"{pending_file}.processed"
            try:
                os.rename(pending_file, processed_file)
                print(f"📦 待确认文件已备份为 {processed_file}")
            except:
                pass
        else:
            print(f"⚠️ 待确认文件中的A股都已在账本，无新增")
    
    except Exception as e:
        print(f"❌ 补充A股成交记录出错: {e}")

# 盘后程序启动时自动执行
supplement_ashare_stocks_from_pending()

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

# ── 版本过滤：改用 Hold_Period 区分新旧版本记录 ──
# 原来用 Score 判断，但 Score 解析曾经有正则bug（"评分:[74]/100"里的方括号没处理，
# 恒为N/A），会导致这一个月本该正常追踪的核心票被这里当成"旧版本"整批丢弃、
# 从复盘里消失（即便它们已经正常写进了 trade_history.csv）。Hold_Period 没被那个bug
# 影响过，用它来判断"是否是新版本完整记录"更可靠。
_INVALID = {'', 'n/a', 'nan', 'none'}
for _col in ['Hold_Period', 'Stop_Loss', 'Score']:
    if _col not in recent_picks.columns:
        recent_picks[_col] = ''

_schema_valid = recent_picks['Hold_Period'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID)
_dropped = (~_schema_valid).sum()
if _dropped > 0:
    print(f"🗂️ 版本过滤：剔除 {_dropped} 条 Hold_Period 缺失的旧版本/不完整记录，不纳入复盘。")
recent_picks = recent_picks[_schema_valid].copy()

# Score=N/A 的记录打印提示，但继续追踪（不剔除——很可能只是历史评分bug导致这一列是N/A，
# Hold_Period/Stop_Loss 仍然有效，不该被连带丢弃）
_no_score = recent_picks['Score'].astype(str).str.strip().str.lower().isin(_INVALID)
if _no_score.sum() > 0:
    tickers_no_score = recent_picks.loc[_no_score, 'Ticker'].tolist()
    print(f"⚠️ 以下 {_no_score.sum()} 条记录 Score=N/A（可能是历史评分bug所致），仍会继续追踪：{tickers_no_score[:10]}")

# Stop_Loss=N/A 的记录打印提示，但继续追踪（不剔除）
_no_stoploss = recent_picks['Stop_Loss'].astype(str).str.strip().str.lower().isin(_INVALID)
if _no_stoploss.sum() > 0:
    tickers_no_sl = recent_picks.loc[_no_stoploss, 'Ticker'].tolist()
    print(f"⚠️ 以下 {_no_stoploss.sum()} 条记录 Stop_Loss=N/A，将继续追踪但无法做止损价核查：{tickers_no_sl[:10]}")

if recent_picks.empty:
    print("⚠️ 过滤后无有效新版本记录，跳过复盘。")
    import sys; sys.exit(0)

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


def get_live_quote(ticker):
    """
    实时行情兜底：今日刚入账的新推荐，pro.daily 的全市场当日快照有时会因为
    数据尚未发布完整而查不到该标的，导致它在后面被 continue 跳过、从复盘
    报告里"消失"。这里对单个标的调用 tushare 实时行情接口兜底，取开盘价/最新价。
    与 scan.py 的 get_latest_price_map 用的是同一套实时行情接口，保持口径一致。
    """
    try:
        bare_code = ticker.split('.')[0] if '.' in ticker else ticker
        df_rt = ts.get_realtime_quotes(bare_code)
        if df_rt is None or df_rt.empty:
            return None, None
        row = df_rt.iloc[0]
        open_p, last_p = None, None
        try:
            v = float(row.get('open', 0))
            open_p = v if v > 0 else None
        except (ValueError, TypeError):
            pass
        try:
            v = float(row.get('price', 0))
            last_p = v if v > 0 else None
        except (ValueError, TypeError):
            pass
        return open_p, last_p
    except Exception as e:
        print(f"⚠️ 实时行情兜底查询失败 [{ticker}]: {e}")
        return None, None


already_archived = set()
review_log_path = "review_history.csv"
if os.path.exists(review_log_path) and os.path.getsize(review_log_path) > 0:
    try:
        existing_review = pd.read_csv(review_log_path, on_bad_lines='skip')
        if {'Status', 'Ticker', 'Rec_Date'}.issubset(existing_review.columns):
            archived_rows = existing_review[existing_review['Status'].isin(
                ['已超期归档', '突发清仓暂停', '止损触发清仓', '周期到期清仓']
            )]
            already_archived = set(zip(archived_rows['Ticker'].astype(str), archived_rows['Rec_Date'].astype(str)))
            print(f"📌 已读取历史归档记录，共 {len(already_archived)} 笔交易此前已处理，本次将跳过重复计算")
    except Exception as e:
        print(f"⚠️ 读取历史归档记录失败，将不做去重: {e}")


active_list = []
expired_list = []
skipped_duplicate = 0

for ticker, group in recent_picks.groupby('Ticker'):
    group = group.sort_values('Date')
    first_row = group.iloc[0]
    latest_row = group.iloc[-1]
    days_held = (get_bj_time().replace(tzinfo=None) - first_row['Date']).days

    latest_tag = str(latest_row.get('Tag', '')).strip()
    
    if latest_tag in ['Trap_Warning', 'Forced_Exit', 'Stop_Loss_Hit', 'Period_Matured']:
        print(f"⏸️ 暂停追踪标的（已被防守端处理过）: {ticker}")
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
        print(f"⏭️ {ticker} Hold_Period=N/A，按要求从复盘列表中剔除。")
        continue

    rec_price = float(first_row['Close_Price'])
    rec_date_str = first_row['Date'].strftime('%Y-%m-%d')
    maturity_date_dt = first_row['Date'] + datetime.timedelta(days=hold_days)
    maturity_date = maturity_date_dt.strftime('%Y-%m-%d')

    if maturity_date_dt.replace(tzinfo=None) <= get_bj_time().replace(tzinfo=None):
        if (str(ticker), rec_date_str) in already_archived:
            skipped_duplicate += 1
            continue

        maturity_price = get_price_on_date(ticker, maturity_date)
        maturity_pnl = round(((maturity_price - rec_price) / rec_price) * 100, 2) if maturity_price else None

        expired_list.append({
            "代码": ticker,
            "名称": first_row['Name'],
            "标签": latest_tag,
            "推荐评分": score_str,
            "持股周期建议": hold_period_str,
            "止损价": stop_loss,
            "首次推荐日": rec_date_str,
            "首次推荐价": rec_price,
            "期满日": maturity_date,
            "期满日价格": maturity_price if maturity_price else "无数据",
            "期满日盈亏(%)": maturity_pnl if maturity_pnl is not None else "无数据",
            "持仓天数": days_held,
            "系统连续推荐次数": len(group),
        })
    else:
        is_new_today = (rec_date_str == get_bj_time().strftime('%Y-%m-%d'))
        today_open_price = None

        cur_price = price_map_today.get(ticker)

        if not cur_price:
            cur_price = get_price_on_date(ticker, get_bj_time().strftime('%Y-%m-%d'))

        if not cur_price or is_new_today:
            # 全市场当日快照可能还没收录"今天"这只标的（新入账推荐尤其常见）：
            # 单独发起实时查询，拿到当天的开盘价/最新价兜底。
            live_open, live_last = get_live_quote(ticker)
            today_open_price = live_open
            if not cur_price:
                cur_price = live_last or live_open

        # 修复：trade_history.csv 里 Close_Price 列在盘前扫描时写入的是能拿到的最近一次
        # 收盘价（不是真正的"今天买入价"，因为扫描发生在开盘前）。今天新增的推荐，如果
        # 这里成功拿到了当天真实开盘价，就用它覆盖 rec_price——否则"首次推荐价"和盈亏
        # 都是拿旧收盘价当买入价在算，会算出跟实际持仓完全对不上的盈亏结果（比如深信服
        # 那种"显示涨14%实际跌了"的情况）。
        if is_new_today and today_open_price:
            rec_price = today_open_price

        if not cur_price:
            # 实时查询也失败，最后兜底用推荐价本身，保证该标的仍会出现在复盘报告里
            # （而不是被静默跳过），同时明确打印警告方便排查。
            print(f"⚠️ 标的 [{ticker}] 现价/开盘价均获取失败，暂用推荐价代替显示，盈亏将显示为 0%。")
            cur_price = rec_price

        cur_pnl = round(((cur_price - rec_price) / rec_price) * 100, 2) if rec_price > 0 else 0
        remaining = (maturity_date_dt.replace(tzinfo=None) - get_bj_time().replace(tzinfo=None)).days

        active_list.append({
            "代码": ticker,
            "名称": first_row['Name'],
            "标签": latest_tag,
            "推荐评分": score_str,
            "持股周期建议": hold_period_str,
            "止损价": stop_loss,
            "首次推荐日": rec_date_str,
            "首次推荐价": rec_price,
            "今日开盘价": round(today_open_price, 2) if today_open_price else ("N/A" if not is_new_today else round(cur_price, 2)),
            "现价": cur_price,
            "持仓天数": days_held,
            "剩余天数": remaining,
            "当前盈亏(%)": cur_pnl,
            "今日新增": "是" if is_new_today else "否",
            "系统连续推荐次数": len(group),
        })

if skipped_duplicate > 0:
    print(f"📌 跳过 {skipped_duplicate} 只已归档过的到期交易，避免重复计入统计")

print(f"✅ 持仓中: {len(active_list)} 只 | 已超期(本次新归档): {len(expired_list)} 只")

if not active_list and not expired_list:
    print("⚠️ 无需复盘的标的，退出。")
    import sys; sys.exit(0)

client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    base_url=os.environ.get("ANTHROPIC_BASE_URL")
)

prompt = f'''
你是顶级量化风控总监。以下是今日需要复盘的 A 股标的数据：

【持仓中（周期内，需要给出风控指令）】：
{active_list}

【已超期（本次新归档，只做策略复盘评价，不需要风控指令）】：
{expired_list}

在风控判断或策略复盘时，请结合推荐评分进行验证：高分票（80分以上）如果出现明显亏损，需要特别指出"高信心预期未兑现"；低分票（60分以下）如果反而盈利良好，也需要指出"评分体系可能过于保守"。

【今日新增标的特别说明】持仓列表中"今日新增"="是"的标的是当天刚生成的全新推荐，"现价"为当天的开盘价/实时价，尚未经历完整交易日，几乎不会有真实盈亏。这类标的请勿按亏损/止损逻辑给风控指令，只需确认开盘价已正确入账，风控动作指令统一给"新建仓，持有观察，明日起纳入正常止损监控"，摘要中也不要把它们的 0% 波动算作"高信心预期未兑现"。

请严格按以下 HTML 骨架输出复盘报告（直出HTML，禁加markdown框，盈利标红，亏损标绿）：

<div style="background: #eceff1; border-left: 6px solid #455a64; padding: 20px; margin-bottom: 25px; border-radius: 8px;">
    <h3 style="margin-top: 0; color: #263238;">⚖️ 盘后总体风控审查</h3>
    <p>(总结持仓中标的整体盈亏状况，以及本次新归档标的的策略胜率评估，特别指出评分与实际表现是否存在明显反差；若有今日新增标的，在此提一句今日共新增几只)</p>
</div>

<h2 style="color: #1565c0; border-bottom: 2px solid #1565c0; padding-bottom: 5px;">📊 持仓中 - 风控纪律核对单</h2>
<div style="background: #fff; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
    <h3 style="margin: 0 0 10px 0;">[若"今日新增"="是"则在最前面加一个 🆕今日新增 徽章] [首次推荐日] | [股票名称] ([代码]) | 评分[推荐评分]/100 | 系统连续推荐[N]次 | 还剩[剩余天数]天到期</h3>
    <p><b>持股周期建议:</b> [持股周期建议] | <b>止损位:</b> [止损价]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] ➔ <b>现价:</b> ¥[现价]（今日开盘价 ¥[今日开盘价]） | <b>当前盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[当前盈亏(%)]%</span></p>
    <p><span style="background: #607d8b; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">风控动作指令</span>
    (今日新增标的：给"新建仓，持有观察"；其余标的：判断现价是否跌破止损位，给出持有/止损/减仓指令)</p>
</div>

<h2 style="color: #37474f; border-bottom: 2px solid #cfd8dc; padding-bottom: 5px; margin-top: 40px;">📁 已超期归档 - 策略复盘评价</h2>
<div style="background: #f5f5f5; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0;">
    <h3 style="margin: 0 0 10px 0;">[首次推荐日] | [股票名称] ([代码]) | 评分[推荐评分]/100 | 期满日:[期满日]</h3>
    <p><b>持股周期建议:</b> [持股周期建议] | <b>止损位:</b> [止损价]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] → <b>期满日价格:</b> ¥[期满日价格] | <b>策略实际盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[期满日盈亏(%)]%</span></p>
    <p><span style="background: #455a64; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">策略复盘</span>
    (评价这次策略是否成功，归因分析盈亏原因)</p>
</div>

【极其重要】直接输出HTML代码，第一个字符必须是 < 符号，绝对不要输出任何思考过程。
'''

ai_html = ""
with client.messages.stream(
    model=TARGET_MODEL,
    max_tokens=30000,
    temperature=0.1,
    messages=[{"role": "user", "content": prompt}]
) as stream:
    for text in stream.text_stream:
        ai_html += text

ai_html = ai_html.replace("```html", "").replace("```", "").strip()

html_start = ai_html.find("<div")
if html_start > 0:
    print(f"⚠️ 检测到AI输出前置了 {html_start} 字符的非HTML内容，已自动截断丢弃")
    ai_html = ai_html[html_start:]

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
        print("⚠️ 表头已自动升级")

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

# ── 4. A股程序化 KPI 数据整合渲染 ──
historical_closed = []
_INVALID_H = {'', 'n/a', 'nan', 'none'}
if os.path.exists(review_log) and os.path.getsize(review_log) > 0:
    try:
        existing_review = pd.read_csv(review_log, on_bad_lines='skip')
        closed_rows = existing_review[existing_review['Status'].isin(['已超期归档', '突发清仓暂停', '止损触发清仓', '周期到期清仓'])]
        for _, r in closed_rows.iterrows():
            try:
                pnl_val = r['PnL_Pct']
                if pd.notna(pnl_val) and str(pnl_val).strip().lower() not in _INVALID_H:
                    pnl = float(pnl_val)
                else:
                    pnl_mat = r['Maturity_PnL']
                    if pd.notna(pnl_mat) and str(pnl_mat).strip().lower() not in _INVALID_H:
                        pnl = float(pnl_mat)
                    else:
                        continue
            except:
                continue
                
            prevented = 0.0
            try:
                sl_val = str(r.get('Stop_Loss', 'N/A')).strip()
                cur_val = str(r.get('Cur_Price', 'N/A')).strip()
                if sl_val not in _INVALID_H and cur_val not in _INVALID_H:
                    sl_price = float(sl_val)
                    cur_price = float(cur_val)
                    prevented = round((sl_price - cur_price) / sl_price * 100, 2) if sl_price > 0 else 0.0
            except:
                pass
                
            historical_closed.append({
                'ticker': r.get('Ticker', ''),
                'name': r.get('Name', ''),
                'pnl': pnl,
                'prevented': prevented,
                'status': r.get('Status', '已超期归档')
            })
    except Exception as e:
        print(f"读取历史归档失败: {e}")

# 合并当下到期以及历史已关闭的持仓
all_closed_trades = []
for h in historical_closed:
    all_closed_trades.append(h)
for item in expired_list:
    try:
        pnl = float(item['期满日盈亏(%)']) if item['期满日盈亏(%)'] != "无数据" else 0.0
    except:
        pnl = 0.0
    all_closed_trades.append({
        'ticker': item['代码'], 'name': item['名称'], 'pnl': pnl,
        'prevented': 0.0, 'status': '已超期归档'
    })

active_count = len(active_list)
closed_count = len(all_closed_trades)
total_count = active_count + closed_count

new_today_count = sum(1 for x in active_list if x.get('今日新增') == '是')
# 今日新增标的当天开盘即入账，几乎不会有真实盈亏，不计入胜率分母，避免拉低数据准确性
_win_rate_pool = [x for x in active_list if x.get('今日新增') != '是']
active_wins = sum(1 for x in _win_rate_pool if isinstance(x['当前盈亏(%)'], (int, float)) and x['当前盈亏(%)'] > 0)
active_win_rate = (active_wins / len(_win_rate_pool) * 100) if _win_rate_pool else 0.0

closed_wins = sum(1 for x in all_closed_trades if x['pnl'] > 0)
closed_win_rate = (closed_wins / closed_count * 100) if closed_count > 0 else 0.0

effective_risk = sum(1 for x in all_closed_trades if x['prevented'] >= -2.0)
risk_rate = (effective_risk / closed_count * 100) if closed_count > 0 else 0.0

# A股设定超级赢家阈值为 15% 
super_threshold = 15.0
all_pnl_list = [x['当前盈亏(%)'] for x in active_list if isinstance(x['当前盈亏(%)'], (int, float))] + [x['pnl'] for x in all_closed_trades]
super_winners = [p for p in all_pnl_list if p >= super_threshold]
super_winner_contribution = sum(super_winners)

other_winners = [p for p in all_pnl_list if 0.0 < p < super_threshold]
other_winner_avg = (sum(other_winners) / len(other_winners)) if other_winners else 0.0

losers = [p for p in all_pnl_list if p < 0.0]
loser_avg = (sum(losers) / len(losers)) if losers else 0.0

kpi_html = f"""
<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 20px;">
    <div style="background: #ffffff; border: 1px solid #eef2f5; border-radius: 10px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); border-top: 4px solid #1565c0;">
        <div style="font-size: 13px; color: #7f8c8d; margin-bottom: 5px;">📊 总推荐笔数</div>
        <div style="font-size: 24px; font-weight: bold; color: #2c3e50; margin-bottom: 5px;">{total_count}</div>
        <div style="font-size: 12px; color: #95a5a6;">活跃持仓 {active_count} 笔（含今日新增 {new_today_count} 笔） · 历史归档 {closed_count} 笔</div>
    </div>
    <div style="background: #ffffff; border: 1px solid #eef2f5; border-radius: 10px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); border-top: 4px solid #2ecc71;">
        <div style="font-size: 13px; color: #7f8c8d; margin-bottom: 5px;">📈 活跃持仓胜率</div>
        <div style="font-size: 24px; font-weight: bold; color: #2ecc71; margin-bottom: 5px;">{active_win_rate:.2f}%</div>
        <div style="font-size: 12px; color: #95a5a6;">{active_wins} 赢 / {len(_win_rate_pool) - active_wins} 亏（不含今日新增 {new_today_count} 笔）</div>
    </div>
    <div style="background: #ffffff; border: 1px solid #eef2f5; border-radius: 10px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); border-top: 4px solid #e67e22;">
        <div style="font-size: 13px; color: #7f8c8d; margin-bottom: 5px;">📉 已归档实现胜率</div>
        <div style="font-size: 24px; font-weight: bold; color: #e67e22; margin-bottom: 5px;">{closed_win_rate:.2f}%</div>
        <div style="font-size: 12px; color: #95a5a6;">{closed_wins} 赢 / {closed_count - closed_wins} 亏</div>
    </div>
    <div style="background: #ffffff; border: 1px solid #eef2f5; border-radius: 10px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); border-top: 4px solid #95a5a6;">
        <div style="font-size: 13px; color: #7f8c8d; margin-bottom: 5px;">🛡️ 风控拦截率</div>
        <div style="font-size: 24px; font-weight: bold; color: #95a5a6; margin-bottom: 5px;">{risk_rate:.2f}%</div>
        <div style="font-size: 12px; color: #95a5a6;">{effective_risk}/{closed_count} 次避险离场有效防范深度回撤</div>
    </div>
</div>
<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 25px;">
    <div style="background: #ffffff; border: 1px solid #eef2f5; border-radius: 10px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); border-top: 4px solid #9b59b6;">
        <div style="font-size: 13px; color: #7f8c8d; margin-bottom: 5px;">🏆 超级赢家贡献</div>
        <div style="font-size: 24px; font-weight: bold; color: #9b59b6; margin-bottom: 5px;">+{super_winner_contribution:.2f}%</div>
        <div style="font-size: 12px; color: #95a5a6;">超级赢家(>{super_threshold}%)累计涨幅</div>
    </div>
    <div style="background: #ffffff; border: 1px solid #eef2f5; border-radius: 10px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); border-top: 4px solid #1abc9c;">
        <div style="font-size: 13px; color: #7f8c8d; margin-bottom: 5px;">💰 其余盈利平均</div>
        <div style="font-size: 24px; font-weight: bold; color: #1abc9c; margin-bottom: 5px;">+{other_winner_avg:.2f}%</div>
        <div style="font-size: 12px; color: #95a5a6;">扣除超级赢家后的盈利均值</div>
    </div>
    <div style="background: #ffffff; border: 1px solid #eef2f5; border-radius: 10px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); border-top: 4px solid #e74c3c;">
        <div style="font-size: 13px; color: #7f8c8d; margin-bottom: 5px;">⚠️ 亏损标的平均</div>
        <div style="font-size: 24px; font-weight: bold; color: #e74c3c; margin-bottom: 5px;">{loser_avg:.2f}%</div>
        <div style="font-size: 12px; color: #95a5a6;">所有亏损标的的平均跌幅</div>
    </div>
</div>
"""

full_html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f4f6f8; padding: 20px; }}
    .card {{ background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); max-width: 1200px; margin: 0 auto; }}
</style></head>
<body>
    <div class='card'>
        <h2 style='color: #2c3e50; margin-top: 0; margin-bottom: 20px; font-size: 26px; border-bottom: 3px solid #1565c0; padding-bottom: 10px; display: flex; align-items: center; gap: 10px;'>
            <span>📊 A股盘后复盘与风控审查报告</span>
        </h2>
        {kpi_html}
        
        {ai_html}
    </div>
</body></html>"""


def send_mail():
    acc = os.environ.get("EMAIL_ACCOUNT")
    pwd = os.environ.get("EMAIL_PASSWORD")
    owner_email = os.environ.get("OWNER_EMAIL")
    if not acc or not pwd or not owner_email:
        print("⚠️ 邮箱配置缺失，跳过发送。")
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
            print(f"✅ 复盘报告已发送！")
    except Exception as e:
        print(f"❌ 发送失败: {e}")


send_mail()
