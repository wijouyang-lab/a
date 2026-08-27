# -*- coding: utf-8 -*-
"""
A股盘后复盘与风控审查引擎（硬止损 + 触及即止损）
- 使用当日最低价（Low_Price）判断是否触发止损
- 自动升级表头，补充 Low_Price 列（安全升级，不丢数据）
- 止损优先于到期，结算价 = 止损价
- 与 scan.py 生成的 ashare_stocks_pending_*.csv 联动
- 今日新增标的纳入胜率统计
"""

import pandas as pd
import datetime
import os
import glob
import re
import smtplib
import csv
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic
import tushare as ts
import sys

# ==========================================
# 环境变量校验
# ==========================================
_missing_env = [k for k in ("CLAWSOCKET_API_KEY", "CLAWSOCKET_BASE_URL", "TUSHARE_TOKEN") if not os.environ.get(k)]
if _missing_env:
    print(f"致命错误：未检测到环境变量 {', '.join(_missing_env)}！请检查 GitHub Secrets 配置。")
    sys.exit(1)

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

if get_bj_time().weekday() >= 5:
    print("周末休市，退出复盘。")
    sys.exit(0)

bj_hour = get_bj_time().hour
if bj_hour < 15:
    print(f"现在是北京时间 {bj_hour} 点，A股尚未收盘，跳过复盘。")
    sys.exit(0)

TARGET_MODEL = 'claude-opus-4-8'
print("启动 A 股盘后复盘引擎（触及即止损版）...")

# 初始化 tushare
ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()

# ==========================================
# 1. 辅助函数：实时行情兜底、表头升级、止损校准
# ==========================================
def get_live_quote(ticker):
    """实时行情兜底（仅用于今日新增标的）"""
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

def _migrate_trade_history_add_columns(log_file):
    """
    安全升级 trade_history.csv 表头：
    增加 Open_Price, Low_Price, ATR_Pct 三列（如果缺失）。
    使用 csv 模块逐行处理，确保每行字段数严格与最终表头一致。
    """
    if not (os.path.exists(log_file) and os.path.getsize(log_file) > 0):
        return

    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if not lines:
        return

    header_line = lines[0].strip()
    old_cols = [c.strip() for c in header_line.split(",")]
    old_col_count = len(old_cols)

    needs_open = "Open_Price" not in old_cols
    needs_low = "Low_Price" not in old_cols
    needs_atr = "ATR_Pct" not in old_cols

    if not (needs_open or needs_low or needs_atr):
        return

    new_cols = old_cols[:]
    if needs_open:
        if "Close_Price" in new_cols:
            idx = new_cols.index("Close_Price")
        else:
            idx = len(new_cols)
        new_cols.insert(idx, "Open_Price")
    if needs_low:
        if "Close_Price" in new_cols:
            idx = new_cols.index("Close_Price") + 1
        else:
            idx = len(new_cols)
        new_cols.insert(idx, "Low_Price")
    if needs_atr:
        new_cols.append("ATR_Pct")

    new_col_count = len(new_cols)

    data_lines = lines[1:]
    fixed_data = []
    for line in data_lines:
        if not line.strip():
            continue
        reader = csv.reader([line])
        fields = next(reader)
        if len(fields) < old_col_count:
            fields += [""] * (old_col_count - len(fields))
        elif len(fields) > old_col_count:
            fields = fields[:old_col_count]

        new_fields = []
        for col in new_cols:
            if col in old_cols:
                idx_old = old_cols.index(col)
                new_fields.append(fields[idx_old] if idx_old < len(fields) else "")
            else:
                new_fields.append("")
        if len(new_fields) < new_col_count:
            new_fields += [""] * (new_col_count - len(new_fields))
        fixed_data.append(",".join(new_fields))

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(",".join(new_cols) + "\n")
        f.write("\n".join(fixed_data))
        if fixed_data:
            f.write("\n")

    added = []
    if needs_open: added.append("Open_Price")
    if needs_low: added.append("Low_Price")
    if needs_atr: added.append("ATR_Pct")
    print(f"⚠️ 账本表头升级：增加列 {added}，已安全迁移 {len(fixed_data)} 行数据。")

def _recalibrate_stop_loss_ashare(stop_loss_str, scan_ref_price, real_open_price):
    try:
        s = str(stop_loss_str).strip()
        if not s or s.lower() in ('n/a', 'nan') or s in ('坚决空仓', '绝对规避', '观望'):
            return stop_loss_str
        nums = re.findall(r'\d+\.?\d*', s)
        if not nums:
            return stop_loss_str
        old_val = float(nums[0])
        ref = float(scan_ref_price)
        new_open = float(real_open_price)
        if ref <= 0 or new_open <= 0 or old_val <= 0:
            return stop_loss_str
        new_val = round(old_val * (new_open / ref), 2)
        suffix = s[s.index(nums[0]) + len(nums[0]):]
        return f"{new_val}{suffix}"
    except (ValueError, TypeError, ZeroDivisionError):
        return stop_loss_str

# ==========================================
# 2. 补充待确认文件（盘后写入账本）
# ==========================================
def supplement_ashare_stocks_from_pending():
    log_file = "trade_history.csv"
    pending_files = sorted(
        f for f in glob.glob("ashare_stocks_pending_*.csv")
        if not f.endswith(".processed")
    )
    if not pending_files:
        print("📋 无待确认A股文件，跳过补充。")
        return

    print(f"📋 发现 {len(pending_files)} 份待确认文件：{pending_files}")

    _migrate_trade_history_add_columns(log_file)

    new_header_cols = [
        "Date", "Ticker", "Name", "Tag", "Industry",
        "Open_Price", "Low_Price", "Close_Price", "Amount", "Daily_Pct",
        "Hold_Period", "Stop_Loss", "Score", "ATR_Pct", "周期共振"
    ]
    new_header = ",".join(new_header_cols) + "\n"

    for pending_file in pending_files:
        m = re.search(r"ashare_stocks_pending_(\d{8})\.csv", pending_file)
        if not m:
            print(f"⚠️ 无法解析日期，跳过 {pending_file}")
            continue
        file_date_str = m.group(1)
        target_date_str = f"{file_date_str[:4]}-{file_date_str[4:6]}-{file_date_str[6:]}"
        is_today = (target_date_str == get_bj_time().strftime('%Y-%m-%d'))

        print(f"📡 处理 {pending_file}（交易日 {target_date_str}）...")

        try:
            df_pending = pd.read_csv(pending_file)
            if df_pending.empty:
                os.rename(pending_file, f"{pending_file}.processed")
                continue

            df_prices = None
            for offset in range(0, 5):
                try_date = (datetime.datetime.strptime(file_date_str, "%Y%m%d") - datetime.timedelta(days=offset)).strftime('%Y%m%d')
                try:
                    df_try = pro.daily(trade_date=try_date, fields='ts_code,open,high,low,close')
                    if df_try is not None and not df_try.empty:
                        df_prices = df_try
                        break
                except Exception as e:
                    print(f"⚠️ 拉取 {try_date} 全市场快照失败: {e}")

            open_map, low_map, close_map = {}, {}, {}
            if df_prices is not None and not df_prices.empty:
                open_map = dict(zip(df_prices['ts_code'], df_prices['open']))
                low_map = dict(zip(df_prices['ts_code'], df_prices['low']))
                close_map = dict(zip(df_prices['ts_code'], df_prices['close']))

            df_existing = pd.DataFrame()
            if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
                try:
                    df_existing = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='warn')
                    df_existing['Date'] = pd.to_datetime(df_existing['Date'])
                except Exception as e:
                    print(f"⚠️ 读取现有账本失败: {e}，将重新创建")

            new_records = []
            missing_price_tickers = []

            for _, row in df_pending.iterrows():
                ticker = row['Ticker']
                open_price = open_map.get(ticker)
                low_price = low_map.get(ticker)
                close_price = close_map.get(ticker)

                if open_price is None or low_price is None or close_price is None:
                    try:
                        df_single = pro.daily(ts_code=ticker, start_date=file_date_str, end_date=file_date_str,
                                              fields='ts_code,open,high,low,close')
                        if df_single is not None and not df_single.empty:
                            if open_price is None:
                                open_price = float(df_single.iloc[0]['open'])
                            if low_price is None:
                                low_price = float(df_single.iloc[0]['low'])
                            if close_price is None:
                                close_price = float(df_single.iloc[0]['close'])
                    except Exception as e:
                        print(f"⚠️ 单独查询 {ticker} 行情失败: {e}")

                if (open_price is None or low_price is None or close_price is None) and is_today:
                    live_open, live_last = get_live_quote(ticker)
                    if open_price is None:
                        open_price = live_open
                    if close_price is None:
                        close_price = live_last or live_open
                    if low_price is None and live_open is not None and live_last is not None:
                        low_price = min(live_open, live_last)

                if open_price is None or close_price is None:
                    missing_price_tickers.append(ticker)

                if open_price is not None and close_price is not None:
                    try:
                        pct_chg = round((float(close_price) - float(open_price)) / float(open_price) * 100, 2)
                    except:
                        pct_chg = row.get('Daily_Pct', '')
                else:
                    pct_chg = row.get('Daily_Pct', '')

                calibrated_stop_loss = row['Stop_Loss']
                if open_price is not None:
                    calibrated_stop_loss = _recalibrate_stop_loss_ashare(
                        row['Stop_Loss'], row.get('Scan_Ref_Price'), open_price
                    )

                if not df_existing.empty:
                    ticker_latest = df_existing[df_existing['Ticker'] == ticker].sort_values('Date', ascending=False)
                    if not ticker_latest.empty:
                        latest_tag = str(ticker_latest.iloc[0].get('Tag', '')).strip()
                        if latest_tag in {'Stop_Loss_Hit', 'Period_Matured', 'Forced_Exit', 'Dropped', 'Trap_Warning'}:
                            row['Tag'] = latest_tag
                            print(f"⏸️ {ticker} 在账本中已标记为 {latest_tag}，同步 pending 标签")

                new_records.append({
                    'Date': target_date_str,
                    'Ticker': ticker,
                    'Name': row['Name'],
                    'Tag': row['Tag'],
                    'Industry': row['Industry'],
                    'Open_Price': '' if open_price is None else open_price,
                    'Low_Price': '' if low_price is None else low_price,
                    'Close_Price': '' if close_price is None else close_price,
                    'Amount': row['Amount'],
                    'Daily_Pct': pct_chg,
                    'Hold_Period': row['Hold_Period'],
                    'Stop_Loss': calibrated_stop_loss,
                    'Score': row['Score'],
                    'ATR_Pct': row.get('ATR_Pct', ''),
                    '周期共振': row.get('周期共振', ''),
                })

            if missing_price_tickers:
                print(f"⚠️ 以下标的无完整价格数据，已按空值写入: {missing_price_tickers}")

            if new_records:
                need_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0
                with open(log_file, "a", encoding="utf-8") as f:
                    if need_header:
                        f.write(new_header)
                    for rec in new_records:
                        f.write(",".join(str(rec[c]) for c in new_header_cols) + "\n")
                print(f"✅ [盘后补充] {pending_file} 成功补充 {len(new_records)} 条A股成交记录")

            os.rename(pending_file, f"{pending_file}.processed")
            print(f"📦 {pending_file} 已标记为已处理")

        except Exception as e:
            print(f"❌ 处理 {pending_file} 失败，保留原文件: {e}")

supplement_ashare_stocks_from_pending()

# ==========================================
# 3. 加载账本，过滤有效持仓
# ==========================================
log_file = "trade_history.csv"
if not os.path.exists(log_file):
    print("⚠️ 交易账本不存在，退出。")
    sys.exit(0)

try:
    df = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='warn')
    df['Date'] = pd.to_datetime(df['Date'])
    cutoff_date = get_bj_time() - datetime.timedelta(days=30)
    recent_picks = df[df['Date'] >= cutoff_date.replace(tzinfo=None)].copy()
    if recent_picks.empty:
        print("⚠️ 最近30天无交易记录，退出。")
        sys.exit(0)
except Exception as e:
    print(f"⚠️ 读取账本失败: {e}")
    sys.exit(1)

_INVALID = {'', 'n/a', 'nan', 'none'}
for col in ['Hold_Period', 'Stop_Loss', 'Score']:
    if col not in recent_picks.columns:
        recent_picks[col] = ''

valid = recent_picks['Hold_Period'].astype(str).str.strip().str.lower().map(lambda v: v not in _INVALID)
dropped = (~valid).sum()
if dropped:
    print(f"🗂️ 过滤掉 {dropped} 条不完整记录（Hold_Period 缺失）")
recent_picks = recent_picks[valid].copy()

if recent_picks.empty:
    print("⚠️ 无有效持仓，退出。")
    sys.exit(0)

no_sl = recent_picks['Stop_Loss'].astype(str).str.strip().str.lower().isin(_INVALID)
if no_sl.sum():
    print(f"⚠️ {no_sl.sum()} 条记录 Stop_Loss 缺失，将继续追踪但无法做止损判断。")

# ==========================================
# 4. 获取历史行情（含 OHLC）
# ==========================================
start_hist = (get_bj_time() - datetime.timedelta(days=60)).strftime('%Y%m%d')
end_hist = get_bj_time().strftime('%Y%m%d')
all_tickers = recent_picks['Ticker'].unique().tolist()

try:
    df_hist_all = pro.daily(
        ts_code=",".join(all_tickers),
        start_date=start_hist,
        end_date=end_hist,
        fields='ts_code,trade_date,open,high,low,close'
    ).sort_values(['ts_code', 'trade_date'])
except Exception as e:
    print(f"⚠️ 历史数据拉取失败: {e}")
    df_hist_all = pd.DataFrame()

trade_date = get_bj_time().strftime('%Y%m%d')
df_today = pro.daily(trade_date=trade_date, fields='ts_code,close')
if df_today is None or df_today.empty:
    trade_date = (get_bj_time() - datetime.timedelta(days=1)).strftime('%Y%m%d')
    df_today = pro.daily(trade_date=trade_date, fields='ts_code,close')
price_map_today = dict(zip(df_today['ts_code'], df_today['close'])) if df_today is not None else {}

# ==========================================
# 5. 辅助函数
# ==========================================
def parse_hold_days(hold_period_str):
    if not hold_period_str or hold_period_str in ['N/A', 'nan', '坚决空仓', '观望']:
        return None
    nums = re.findall(r'\d+', str(hold_period_str))
    return int(nums[-1]) if nums else None

def get_price_on_date(ticker, target_date_str, field='close'):
    if df_hist_all.empty:
        return None
    ticker_data = df_hist_all[df_hist_all['ts_code'] == ticker].copy()
    if ticker_data.empty:
        return None
    ticker_data['trade_date'] = pd.to_datetime(ticker_data['trade_date'])
    target = pd.to_datetime(target_date_str)
    valid = ticker_data[ticker_data['trade_date'] <= target]
    if valid.empty:
        return None
    return float(valid.iloc[-1][field])

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
            print(f"📌 已加载历史归档 {len(already_archived)} 条")
    except Exception as e:
        print(f"⚠️ 读取历史归档失败: {e}")

# ==========================================
# 6. 遍历持仓，执行"触及即止损"
# ==========================================
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
        print(f"⏸️ {ticker} 已终止，跳过")
        continue

    hold_period_str = 'N/A'
    stop_loss_str = 'N/A'
    score_str = 'N/A'
    for _, r in group.iterrows():
        v = str(r.get('Hold_Period', 'N/A')).strip()
        if v not in ['N/A', 'nan', '', '坚决空仓']:
            hold_period_str = r['Hold_Period']
            break
    for _, r in group.iterrows():
        v = str(r.get('Stop_Loss', 'N/A')).strip()
        if v not in ['N/A', 'nan', '', '坚决空仓', '绝对规避', '观望']:
            stop_loss_str = r['Stop_Loss']
            break
    for _, r in group.iterrows():
        v = str(r.get('Score', 'N/A')).strip()
        if v not in ['N/A', 'nan', '']:
            score_str = r['Score']
            break

    hold_days = parse_hold_days(hold_period_str)
    if hold_days is None:
        print(f"⏭️ {ticker} Hold_Period 无效，跳过")
        continue

    try:
        rec_price = float(first_row.get('Open_Price', 0))
        if rec_price <= 0:
            rec_price = float(first_row.get('Close_Price', 0))
    except:
        rec_price = float(first_row.get('Close_Price', 0))
    rec_date_str = first_row['Date'].strftime('%Y-%m-%d')
    maturity_date_dt = first_row['Date'] + datetime.timedelta(days=hold_days)
    maturity_date = maturity_date_dt.strftime('%Y-%m-%d')

    today_low = None
    if 'Low_Price' in first_row and str(first_row['Low_Price']).strip():
        try:
            today_low = float(first_row['Low_Price'])
        except:
            pass

    if today_low is None:
        try:
            today_str = get_bj_time().strftime('%Y-%m-%d')
            low_val = get_price_on_date(ticker, today_str, field='low')
            if low_val is not None:
                today_low = low_val
        except:
            pass

    if today_low is None:
        try:
            live_open, live_last = get_live_quote(ticker)
            if live_open is not None and live_last is not None:
                today_low = min(live_open, live_last)
        except:
            pass

    if today_low is None:
        today_low = price_map_today.get(ticker, rec_price)

    stop_loss_val = None
    sl_text = str(stop_loss_str).strip()
    if sl_text and sl_text.lower() not in {'n/a', 'nan', 'none'} and sl_text not in {'坚决空仓', '绝对规避', '观望'}:
        nums = re.findall(r'\d+\.?\d*', sl_text)
        if nums:
            stop_loss_val = float(nums[0])

    if stop_loss_val is not None and today_low is not None and float(today_low) <= stop_loss_val:
        if (str(ticker), rec_date_str) in already_archived:
            skipped_duplicate += 1
            continue

        exit_price = stop_loss_val
        pnl = round(((exit_price - rec_price) / rec_price) * 100, 2) if rec_price > 0 else 0.0
        stop_days = max(0, (get_bj_time().replace(tzinfo=None) - first_row['Date']).days)

        try:
            df_stop = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='warn')
            for col in ['Exit_Price', 'Exit_Date']:
                if col in df_stop.columns:
                    df_stop[col] = df_stop[col].astype(object)
            terminal_tags = {'Stop_Loss_Hit', 'Period_Matured', 'Forced_Exit', 'Dropped', 'Trap_Warning'}
            mask = (df_stop['Ticker'].astype(str) == ticker) & (~df_stop['Tag'].astype(str).isin(terminal_tags))
            df_stop.loc[mask, 'Tag'] = 'Stop_Loss_Hit'
            if 'Exit_Price' in df_stop.columns and 'Exit_Date' in df_stop.columns:
                df_stop.loc[mask, 'Exit_Price'] = exit_price
                df_stop.loc[mask, 'Exit_Date'] = get_bj_time().strftime('%Y-%m-%d')
            df_stop.to_csv(log_file, index=False)
            print(f"🛑 [触及止损] {ticker} 最低价 {today_low} <= 止损 {stop_loss_val}，按止损价 {exit_price} 结算")
        except Exception as e:
            print(f"⚠️ 更新 {ticker} 状态失败: {e}")

        expired_list.append({
            "代码": ticker,
            "名称": first_row.get('Name', ticker),
            "标签": "Stop_Loss_Hit",
            "推荐评分": score_str,
            "持股周期建议": hold_period_str,
            "止损价": stop_loss_str,
            "首次推荐日": rec_date_str,
            "首次推荐价": rec_price,
            "期满日": "",
            "期满日价格": exit_price,
            "期满日盈亏(%)": pnl,
            "持仓天数": stop_days,
            "系统连续推荐次数": len(group),
            "结算类型": "止损触发清仓",
        })
        continue

    if maturity_date_dt.replace(tzinfo=None) <= get_bj_time().replace(tzinfo=None):
        if (str(ticker), rec_date_str) in already_archived:
            skipped_duplicate += 1
            continue

        maturity_price = get_price_on_date(ticker, maturity_date, field='close')
        pnl = round(((maturity_price - rec_price) / rec_price) * 100, 2) if maturity_price else None

        expired_list.append({
            "代码": ticker,
            "名称": first_row.get('Name', ticker),
            "标签": latest_tag,
            "推荐评分": score_str,
            "持股周期建议": hold_period_str,
            "止损价": stop_loss_str,
            "首次推荐日": rec_date_str,
            "首次推荐价": rec_price,
            "期满日": maturity_date,
            "期满日价格": maturity_price if maturity_price else "无数据",
            "期满日盈亏(%)": pnl if pnl is not None else "无数据",
            "持仓天数": days_held,
            "系统连续推荐次数": len(group),
            "结算类型": "周期到期清仓",
        })
    else:
        # ==========================================================
        # 【修复】判断今日新增：用最新一行（latest_row）的日期，而非最早一行
        # ==========================================================
        latest_date_str = latest_row['Date'].strftime('%Y-%m-%d')
        is_new_today = (latest_date_str == get_bj_time().strftime('%Y-%m-%d'))
        # ==========================================================
        
        today_open = None
        if 'Open_Price' in first_row and str(first_row['Open_Price']).strip():
            try:
                today_open = float(first_row['Open_Price'])
            except:
                pass
        if today_open is None:
            today_open = get_price_on_date(ticker, get_bj_time().strftime('%Y-%m-%d'), field='open')
            if today_open is None:
                live_open, _ = get_live_quote(ticker)
                today_open = live_open

        cur_price = price_map_today.get(ticker)
        if cur_price is None:
            cur_price = get_price_on_date(ticker, get_bj_time().strftime('%Y-%m-%d'), field='close')
        if cur_price is None:
            _, live_last = get_live_quote(ticker)
            cur_price = live_last or rec_price

        if is_new_today and today_open:
            rec_price = today_open

        cur_pnl = round(((cur_price - rec_price) / rec_price) * 100, 2) if rec_price > 0 else 0
        remaining = (maturity_date_dt.replace(tzinfo=None) - get_bj_time().replace(tzinfo=None)).days

        active_list.append({
            "代码": ticker,
            "名称": first_row.get('Name', ticker),
            "标签": latest_tag,
            "推荐评分": score_str,
            "持股周期建议": hold_period_str,
            "止损价": stop_loss_str,
            "首次推荐日": rec_date_str,
            "首次推荐价": rec_price,
            "今日开盘价": round(today_open, 2) if today_open else "N/A",
            "现价": cur_price,
            "持仓天数": days_held,
            "剩余天数": remaining,
            "当前盈亏(%)": cur_pnl,
            "今日新增": "是" if is_new_today else "否",
            "系统连续推荐次数": len(group),
        })

print(f"✅ 持仓中: {len(active_list)} 只 | 本次新归档: {len(expired_list)} 只 (含止损触发)")
if skipped_duplicate:
    print(f"📌 跳过 {skipped_duplicate} 笔已归档交易")

if not active_list and not expired_list:
    print("⚠️ 无复盘数据，退出。")
    sys.exit(0)

# ==========================================
# 7. 调用 AI 生成报告
# ==========================================
client = anthropic.Anthropic(
    api_key=os.environ.get("CLAWSOCKET_API_KEY"),
    base_url=os.environ.get("CLAWSOCKET_BASE_URL")
)

prompt = f'''
你是顶级量化风控总监。以下是今日需要复盘的 A 股标的数据：

【持仓中（周期内，需要给出风控指令）】：
{active_list}

【已关闭交易（本次新归档；其中"止损触发清仓"已经按止损价结算，绝不能继续追踪到期）】：
{expired_list}

在风控判断或策略复盘时，请结合推荐评分进行验证：高分票（80分以上）如果出现明显亏损，需要特别指出"高信心预期未兑现"；低分票（60分以下）如果反而盈利良好，也需要指出"评分体系可能过于保守"。

【今日新增标的特别说明】持仓列表中"今日新增"="是"的标的是当天刚生成的全新推荐，已有完整交易日数据，应计入正常盈亏分析。风控动作指令统一给"持有观察，纳入正常止损监控"。

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
    (判断现价是否跌破止损位，给出持有/止损/减仓指令)</p>
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
    messages=[{"role": "user", "content": prompt}]
) as stream:
    for text in stream.text_stream:
        ai_html += text

ai_html = ai_html.replace("```html", "").replace("```", "").strip()
html_start = ai_html.find("<div")
if html_start > 0:
    print(f"⚠️ 截断 AI 前置非HTML内容")
    ai_html = ai_html[html_start:]

# ==========================================
# 8. 归档至 review_history.csv
# ==========================================
review_log = "review_history.csv"
new_header = "Review_Date,Ticker,Name,Tag,Rec_Date,Rec_Price,Cur_Price,Days_Held,PnL_Pct,Maturity_PnL,Hold_Period,Stop_Loss,Rec_Count,Status,Score\n"
review_need_header = not (os.path.exists(review_log) and os.path.getsize(review_log) > 0)

if os.path.exists(review_log) and os.path.getsize(review_log) > 0:
    with open(review_log, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if lines and "Score" not in lines[0]:
        lines[0] = new_header
        with open(review_log, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print("⚠️ review_history.csv 表头升级")

try:
    with open(review_log, "a", encoding="utf-8") as f:
        if review_need_header:
            f.write(new_header)
        review_date = get_bj_time().strftime('%Y-%m-%d')

        for item in active_list:
            f.write(f"{review_date},{item['代码']},{item['名称']},{item['标签']},{item['首次推荐日']},{item['首次推荐价']},{item['现价']},{item['持仓天数']},{item['当前盈亏(%)']},,{item['持股周期建议']},{item['止损价']},{item['系统连续推荐次数']},持仓中,{item['推荐评分']}\n")

        for item in expired_list:
            pnl = item['期满日盈亏(%)'] if item['期满日盈亏(%)'] != "无数据" else ""
            status = item.get('结算类型', '周期到期清仓')
            if status == "止损触发清仓":
                f.write(f"{review_date},{item['代码']},{item['名称']},{item['标签']},{item['首次推荐日']},{item['首次推荐价']},{item['期满日价格']},{item['持仓天数']},{pnl},,{item['持股周期建议']},{item['止损价']},{item['系统连续推荐次数']},止损触发清仓,{item['推荐评分']}\n")
            else:
                f.write(f"{review_date},{item['代码']},{item['名称']},{item['标签']},{item['首次推荐日']},{item['首次推荐价']},{item['期满日价格']},{item['持仓天数']},{pnl},{pnl},{item['持股周期建议']},{item['止损价']},{item['系统连续推荐次数']},周期到期清仓,{item['推荐评分']}\n")
    print("✅ 归档写入成功")
except Exception as e:
    print(f"⚠️ 归档写入失败: {e}")

# ==========================================
# 9. KPI 计算与邮件 HTML 组装
# ==========================================
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
        'prevented': 0.0, 'status': item.get('结算类型', '周期到期清仓')
    })

active_count = len(active_list)
closed_count = len(all_closed_trades)
total_count = active_count + closed_count

new_today_count = sum(1 for x in active_list if x.get('今日新增') == '是')

# 【修复】今日新增的标的已有完整交易日数据，应纳入胜率统计
_win_rate_pool = [x for x in active_list if isinstance(x['当前盈亏(%)'], (int, float))]
active_wins = sum(1 for x in _win_rate_pool if x['当前盈亏(%)'] > 0)
active_win_rate = (active_wins / len(_win_rate_pool) * 100) if _win_rate_pool else 0.0

closed_wins = sum(1 for x in all_closed_trades if x['pnl'] > 0)
closed_win_rate = (closed_wins / closed_count * 100) if closed_count > 0 else 0.0

effective_risk = sum(1 for x in all_closed_trades if x['prevented'] >= -2.0)
risk_rate = (effective_risk / closed_count * 100) if closed_count > 0 else 0.0

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
        <div style="font-size: 12px; color: #95a5a6;">{active_wins} 赢 / {len(_win_rate_pool) - active_wins} 亏（含今日新增 {new_today_count} 笔）</div>
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

# ==========================================
# 10. 邮件发送
# ==========================================
def send_mail():
    acc = os.environ.get("EMAIL_ACCOUNT")
    pwd = os.environ.get("EMAIL_PASSWORD")
    owner_email = os.environ.get("TARGET_EMAILS") or os.environ.get("OWNER_EMAIL")
    if not acc or not pwd or not owner_email:
        print("⚠️ 邮件配置缺失，跳过发送。")
        return
    targets = [e.strip() for e in owner_email.split(",") if e.strip()]
    msg = MIMEMultipart()
    msg['From'] = acc
    msg['To'] = ", ".join(targets)
    msg['Subject'] = f"【盘后清算】A股风控纪律与复盘 ({get_bj_time().strftime('%Y-%m-%d')})"
    msg.attach(MIMEText(full_html, 'html'))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(acc, pwd)
            s.sendmail(acc, targets, msg.as_string())
        print("✅ 复盘报告已发送！")
    except Exception as e:
        print(f"❌ 发送失败: {e}")

send_mail()
print("🎯 A股盘后复盘完成。")
