# -*- coding: utf-8 -*-
"""
A股盘后复盘与风控审查引擎（终极可靠版）
- 完全重构 supplement，确保可靠追加
- 只检查最近30天活跃持仓，避免历史误判
- 强制列对齐，确保写入正确
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
    print(f"致命错误：未检测到环境变量 {', '.join(_missing_env)}！")
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
print("启动 A 股盘后复盘引擎（终极可靠版）...")

ts.set_token(os.environ.get("TUSHARE_TOKEN"))
pro = ts.pro_api()

# ==========================================
# 1. 辅助函数
# ==========================================
def get_live_quote(ticker):
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
        except:
            pass
        try:
            v = float(row.get('price', 0))
            last_p = v if v > 0 else None
        except:
            pass
        return open_p, last_p
    except Exception as e:
        print(f"⚠️ 实时行情兜底查询失败 [{ticker}]: {e}")
        return None, None

def _ensure_table_columns(log_file):
    """
    确保表头包含所有必要列，并返回当前表头列列表。
    如果缺失列，则添加并填充空值。
    """
    if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
        # 文件不存在或为空，使用默认表头
        default_cols = [
            "Date", "Ticker", "Name", "Tag", "Industry",
            "Open_Price", "Low_Price", "Close_Price", "Amount", "Daily_Pct",
            "Hold_Period", "Stop_Loss", "Stop_Method", "Trail_Stop", "MA20", "MA50", "Score", "ATR_Pct", "周期共振", "Exit_Date", "Exit_Price", "PE_TTM", "EPS_TTM", "PB", "Earnings_Growth", "ROE", "估值评分", "估值结论"
        ]
        return default_cols

    with open(log_file, "r", encoding="utf-8") as f:
        header = f.readline().strip()
    cols = [c.strip() for c in header.split(",")]
    # 确保所有必要列存在
    required = ["Date", "Ticker", "Name", "Tag", "Industry",
                "Open_Price", "Low_Price", "Close_Price", "Amount", "Daily_Pct",
                "Hold_Period", "Stop_Loss", "Stop_Method", "Trail_Stop", "MA20", "MA50", "Score", "ATR_Pct", "周期共振", "Exit_Date", "Exit_Price", "PE_TTM", "EPS_TTM", "PB", "Earnings_Growth", "ROE", "估值评分", "估值结论"]
    missing = [c for c in required if c not in cols]
    if missing:
        # 添加缺失列（简单追加到末尾）
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # 更新表头
        new_cols = cols + missing
        new_header = ",".join(new_cols) + "\n"
        # 处理数据行，为缺失列补空值
        data_lines = lines[1:]
        fixed = []
        for line in data_lines:
            if not line.strip():
                continue
            fields = line.rstrip("\n").split(",")
            # 补齐到新列数
            while len(fields) < len(new_cols):
                fields.append("")
            fixed.append(",".join(fields))
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(new_header)
            if fixed:
                f.write("\n".join(fixed) + "\n")
        print(f"⚠️ 表头补全：添加列 {missing}")
        cols = new_cols
    return cols

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
    except:
        return stop_loss_str

# ==========================================
# 2. 补充待确认文件（完全重写）
# ==========================================
def supplement_ashare_stocks_from_pending():
    """
    盘后补充 scan.py 当天生成的 pending 文件。

    关键修复：
    1. 与 scan.py 保持一致：Observation 不视为真实持仓，不阻止当天重新入选。
    2. 去重改为 (Ticker, Date)，而不是“最近30天只要出现过就跳过”。
    3. 今天的 .csv 即使上一次被误标记为 .processed，只要 trade_history 中没有当天记录，
       仍会作为“今日恢复文件”重新导入，避免今天新增永久丢失。
    4. 写入前重新读取 trade_history，支持一次运行同时恢复多个遗漏标的。
    5. 今日 pending 是 scan 当天新建仓的权威来源：只按 (Ticker, Date) 去重，不能因为历史仍有同 ticker 持仓就跳过。
    6. 成功验证 trade_history 已写入当天记录后，同时删除 .csv 和 .csv.processed，避免残留。
    """
    log_file = "trade_history.csv"
    today_str = get_bj_time().strftime('%Y-%m-%d')

    # 正常 pending + 今日已被旧版本错误标记为 .processed 的恢复文件
    pending_files = sorted(
        f for f in glob.glob("ashare_stocks_pending_*.csv")
        if not f.endswith(".processed")
    )

    recovery_files = sorted(
        f for f in glob.glob(f"ashare_stocks_pending_{get_bj_time().strftime('%Y%m%d')}.csv.processed")
        if os.path.isfile(f)
    )

    all_files = []
    for f in pending_files:
        all_files.append((f, False))
    for f in recovery_files:
        if f not in [x[0] for x in all_files]:
            all_files.append((f, True))

    if not all_files:
        print("📋 无待确认A股文件，跳过补充。")
        return set()

    print(f"📋 发现 {len(all_files)} 份待确认/恢复文件：{[x[0] for x in all_files]}")

    existing_cols = _ensure_table_columns(log_file)
    print(f"📋 当前表头列：{existing_cols}")

    # 与 scan.py 保持完全一致：这 3 个标签才算“真实活跃持仓”
    HOLDING_TAGS = {'Core_Double_Dragon', 'Core_Dragon', 'Sub_Pioneer'}

    successfully_imported_tickers = set()

    for pending_file, is_recovery in all_files:
        m = re.search(r"ashare_stocks_pending_(\d{8})\.csv(?:\.processed)?$", pending_file)
        if not m:
            print(f"⚠️ 无法解析日期，跳过 {pending_file}")
            continue

        file_date_str = m.group(1)
        target_date_str = f"{file_date_str[:4]}-{file_date_str[4:6]}-{file_date_str[6:]}"
        is_today = (target_date_str == today_str)

        print(
            f"📡 处理 {pending_file}（交易日 {target_date_str}"
            f"{'；恢复旧版已处理文件' if is_recovery else ''}）..."
        )

        try:
            df_pending = pd.read_csv(pending_file, dtype=str, keep_default_na=False)
            if df_pending.empty:
                if not is_recovery:
                    os.rename(pending_file, f"{pending_file}.processed")
                print(f"ℹ️ {pending_file} 为空")
                continue

            # 统一 ticker 格式，避免前后空格造成“假重复”
            if 'Ticker' not in df_pending.columns:
                print(f"⚠️ {pending_file} 缺少 Ticker 列，跳过。")
                continue
            df_pending['Ticker'] = df_pending['Ticker'].astype(str).str.strip()

            # 获取目标交易日 OHLC
            df_prices = None
            for offset in range(0, 5):
                try_date = (
                    datetime.datetime.strptime(file_date_str, "%Y%m%d")
                    - datetime.timedelta(days=offset)
                ).strftime('%Y%m%d')
                try:
                    df_try = pro.daily(
                        trade_date=try_date,
                        fields='ts_code,open,high,low,close'
                    )
                    if df_try is not None and not df_try.empty:
                        # 对“今天”禁止悄悄拿前一交易日价格冒充今天；
                        # 非今天文件才允许回退最近可用交易日。
                        if is_today and try_date != file_date_str:
                            continue
                        df_prices = df_try
                        break
                except Exception:
                    pass

            open_map, low_map, close_map = {}, {}, {}
            if df_prices is not None and not df_prices.empty:
                open_map = dict(zip(df_prices['ts_code'], df_prices['open']))
                low_map = dict(zip(df_prices['ts_code'], df_prices['low']))
                close_map = dict(zip(df_prices['ts_code'], df_prices['close']))

            # 每个 pending 文件处理前重新读取账本，避免同一批次重复写入
            df_existing = pd.DataFrame()
            if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
                try:
                    df_existing = pd.read_csv(
                        log_file,
                        keep_default_na=False,
                        on_bad_lines='warn'
                    )
                    if 'Date' in df_existing.columns:
                        df_existing['Date'] = pd.to_datetime(
                            df_existing['Date'], errors='coerce'
                        )
                    if 'Ticker' in df_existing.columns:
                        df_existing['Ticker'] = (
                            df_existing['Ticker'].astype(str).str.strip()
                        )
                except Exception as e:
                    print(f"⚠️ 读取现有账本用于去重失败：{e}")
                    df_existing = pd.DataFrame()

            # 精确去重：同一股票 + 同一天已经写入，则永远不再重复追加
            existing_date_keys = set()
            if (
                not df_existing.empty
                and {'Ticker', 'Date'}.issubset(df_existing.columns)
            ):
                valid_existing = df_existing.dropna(subset=['Date']).copy()
                existing_date_keys = {
                    (
                        str(r['Ticker']).strip(),
                        pd.to_datetime(r['Date']).strftime('%Y-%m-%d')
                    )
                    for _, r in valid_existing.iterrows()
                }

            # 今日 pending 是 scan 当天新仓的权威来源。
            # 这里绝不能用“Ticker 当前是否已经活跃”作为阻塞条件，
            # 否则旧生命周期/旧推荐会把今天的新仓吞掉。只按 (Ticker, Date) 去重。
            new_records = []
            skipped_same_day = []
            missing_price_tickers = []

            for _, row in df_pending.iterrows():
                ticker = str(row.get('Ticker', '')).strip()
                if not ticker:
                    continue

                # 1) 同一 ticker + 同一天已经存在：严格去重
                if (ticker, target_date_str) in existing_date_keys:
                    skipped_same_day.append(ticker)
                    continue

                # 2) 不因为历史同 ticker 记录而跳过。
                #    scan 当天生成 pending 就代表今天是一次新的推荐/建仓事件；
                #    是否属于新的生命周期由后续 review 的日期边界决定。

                open_price = open_map.get(ticker)
                low_price = low_map.get(ticker)
                close_price = close_map.get(ticker)

                # 单独查询目标交易日
                if open_price is None or low_price is None or close_price is None:
                    try:
                        df_single = pro.daily(
                            ts_code=ticker,
                            start_date=file_date_str,
                            end_date=file_date_str,
                            fields='ts_code,open,high,low,close'
                        )
                        if df_single is not None and not df_single.empty:
                            if open_price is None:
                                open_price = float(df_single.iloc[0]['open'])
                            if low_price is None:
                                low_price = float(df_single.iloc[0]['low'])
                            if close_price is None:
                                close_price = float(df_single.iloc[0]['close'])
                    except Exception:
                        pass

                # 今天：只有拿不到今日 daily 时才允许实时行情兜底
                if (open_price is None or close_price is None or low_price is None) and is_today:
                    try:
                        live_open, live_last = get_live_quote(ticker)
                        if open_price is None:
                            open_price = live_open
                        if close_price is None:
                            close_price = live_last or live_open
                        if low_price is None and live_open is not None and live_last is not None:
                            low_price = min(live_open, live_last)
                    except Exception:
                        pass

                if open_price is None or close_price is None:
                    missing_price_tickers.append(ticker)

                if open_price is not None and close_price is not None:
                    try:
                        pct_chg = round(
                            (float(close_price) - float(open_price))
                            / float(open_price) * 100, 2
                        )
                    except Exception:
                        pct_chg = row.get('Daily_Pct', '')
                else:
                    pct_chg = row.get('Daily_Pct', '')

                calibrated_stop_loss = row.get('Stop_Loss', '')
                if open_price is not None:
                    calibrated_stop_loss = _recalibrate_stop_loss_ashare(
                        row.get('Stop_Loss', ''),
                        row.get('Scan_Ref_Price'),
                        open_price
                    )

                tag = 'Core_Dragon'
                hold_period = str(row.get('Hold_Period', '')).strip()
                if hold_period == '' or hold_period.lower() in {
                    'n/a', 'nan', 'none', '观望'
                }:
                    hold_period = '5-10天'
                    print(
                        f"⚠️ {ticker} Hold_Period 缺失，"
                        "使用默认值 '5-10天'"
                    )
                elif not re.search(r'\d+.*天', hold_period):
                    hold_period += '天'

                rec = {
                    'Date': target_date_str,
                    'Ticker': ticker,
                    'Name': row.get('Name', ticker),
                    'Tag': tag,
                    'Industry': row.get('Industry', ''),
                    'Open_Price': '' if open_price is None else open_price,
                    'Low_Price': '' if low_price is None else low_price,
                    'Close_Price': '' if close_price is None else close_price,
                    'Amount': row.get('Amount', ''),
                    'Daily_Pct': pct_chg,
                    'Hold_Period': hold_period,
                    'Stop_Loss': calibrated_stop_loss,
                    'Score': row.get('Score', ''),
                    'ATR_Pct': row.get('ATR_Pct', ''),
                    '周期共振': row.get('周期共振', ''),
                }
                new_records.append(rec)

                # 本次运行内立即加入去重集合，防止 pending 自身有重复行
                existing_date_keys.add((ticker, target_date_str))

            if skipped_same_day:
                print(f"⏭️ 同日已存在，跳过 {len(skipped_same_day)} 条：{skipped_same_day}")
            if missing_price_tickers:
                print(f"⚠️ 以下标的无完整价格：{missing_price_tickers}")

            if new_records:
                # 以 DataFrame 统一写出，避免名称/行业中出现逗号时破坏 CSV
                append_df = pd.DataFrame(new_records)
                for col in existing_cols:
                    if col not in append_df.columns:
                        append_df[col] = ''
                append_df = append_df[existing_cols]

                need_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0
                append_df.to_csv(
                    log_file,
                    mode='a',
                    header=need_header,
                    index=False,
                    encoding='utf-8'
                )

                for rec in new_records:
                    print(
                        f"   ✅ 已追加 {rec['Ticker']} "
                        f"(Date={rec['Date']}, Tag={rec['Tag']}, "
                        f"Hold_Period={rec['Hold_Period']})"
                    )

                print(
                    f"✅ [盘后补充] {pending_file} 成功追加 "
                    f"{len(new_records)} 条记录"
                )
            else:
                print(f"ℹ️ {pending_file} 无新记录需要追加")

            # 写入后立刻以 trade_history 做二次核验；只有所有 ticker 都存在当天记录，
            # 才删除 .csv / .processed。这样即使“无新记录”也是安全可删的，而写入失败绝不误删。
            try:
                if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
                    _verify = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='skip')
                    _verify['Ticker'] = _verify['Ticker'].astype(str).str.strip()
                    _verify['Date'] = pd.to_datetime(_verify['Date'], errors='coerce')
                    expected = {str(x).strip() for x in df_pending['Ticker'].tolist() if str(x).strip()}
                    actual = set(
                        _verify[_verify['Date'].dt.strftime('%Y-%m-%d') == target_date_str]['Ticker']
                        .astype(str).str.strip().tolist()
                    )
                    missing_after_write = sorted(expected - actual)
                    if not missing_after_write:
                        successfully_imported_tickers.update(expected)
                        # 同时清理正常 pending 和旧版遗留的 .processed。
                        cleanup_targets = {pending_file}
                        if is_today:
                            cleanup_targets.add(f"ashare_stocks_pending_{file_date_str}.csv")
                            cleanup_targets.add(f"ashare_stocks_pending_{file_date_str}.csv.processed")
                        for cleanup_file in cleanup_targets:
                            if os.path.exists(cleanup_file):
                                try:
                                    os.remove(cleanup_file)
                                    print(f"🗑️ {cleanup_file} 已验证写入成功，自动删除")
                                except Exception as cleanup_err:
                                    print(f"⚠️ 删除 {cleanup_file} 失败：{cleanup_err}")
                    else:
                        print(f"🚨 写入后核验失败，暂不删除 pending：缺失 {missing_after_write}")
                else:
                    print("🚨 trade_history.csv 不存在或为空，暂不删除 pending。")
            except Exception as verify_err:
                print(f"⚠️ pending 写入后核验失败，暂不删除文件：{verify_err}")

        except Exception as e:
            print(f"❌ 处理 {pending_file} 失败: {e}")

    return successfully_imported_tickers

# 返回本次成功导入的 ticker，供后续 review 强制确认“今日新增”进入持仓生命周期。
_today_pending_imported = supplement_ashare_stocks_from_pending() or set()
print(f"📌 今日 pending 成功导入/确认的标的：{sorted(_today_pending_imported)}")

# ==========================================
# 3. 加载账本，过滤有效持仓
# ==========================================
# 当前复盘日期：必须在加载账本/今日新增筛选之前定义
today_str = get_bj_time().strftime('%Y-%m-%d')

log_file = "trade_history.csv"
if not os.path.exists(log_file):
    print("⚠️ 交易账本不存在，退出。")
    sys.exit(0)

try:
    df = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='warn')
    df['Date'] = pd.to_datetime(df['Date'])
    df['Ticker'] = df['Ticker'].astype(str).str.strip()

    # ==========================================================
    # 【关键自愈】A股 T+1：今天建立的新仓必须留在持仓中。
    # 之前出现过一个更隐蔽的问题：pending 重新运行时只按
    # “Ticker + Date 是否存在”做核验，如果今天已有同日记录但 Tag 是
    # Observation / Stop_Loss_Hit / Period_Matured / Forced_Exit 等非活跃状态，
    # 系统会误以为 pending 已成功导入，然后删除 pending；后面的 review 又因为
    # ACTIVE_TAGS 过滤把这只股票吞掉。
    #
    # 在本流程里，scan 当天生成的交易记录代表“当天新建仓事件”。A股 T+1 下，
    # 当天不能卖出，因此 Date==today 的记录若不是活跃持仓，必须自愈为 Core_Dragon。
    # 只修复“今天”的记录，不修改任何历史日期。
    # ==========================================================
    today_rows_mask = df['Date'].dt.strftime('%Y-%m-%d').eq(today_str)
    today_non_active_mask = today_rows_mask & ~df['Tag'].astype(str).str.strip().eq('Core_Double_Dragon') & ~df['Tag'].astype(str).str.strip().eq('Core_Dragon') & ~df['Tag'].astype(str).str.strip().eq('Sub_Pioneer')
    if today_non_active_mask.any():
        repaired_tickers = sorted(df.loc[today_non_active_mask, 'Ticker'].astype(str).str.strip().unique().tolist())
        old_tags = (
            df.loc[today_non_active_mask, ['Ticker', 'Tag']]
            .drop_duplicates()
            .astype(str)
            .apply(lambda r: f"{r['Ticker']}={r['Tag']}", axis=1)
            .tolist()
        )
        df.loc[today_non_active_mask, 'Tag'] = 'Core_Dragon'
        df.to_csv(log_file, index=False, encoding='utf-8')
        print(
            f"🛠️ [T+1自愈] 今天发现 {len(repaired_tickers)} 只记录不是活跃持仓，"
            f"已恢复为 Core_Dragon：{repaired_tickers}；原状态：{old_tags}"
        )

    # 与 scan.py 保持一致：真实持仓使用以下3种 Tag。Observation 只是观察池。
    ACTIVE_TAGS = {'Core_Double_Dragon', 'Core_Dragon', 'Sub_Pioneer'}
    cutoff_date = get_bj_time() - datetime.timedelta(days=30)
    recent_picks = df[
        (df['Date'] >= cutoff_date.replace(tzinfo=None)) &
        (df['Tag'].astype(str).str.strip().isin(ACTIVE_TAGS))
    ].copy()

    # ==========================================================
    # 【最高优先级】今日 pending 是“今天实际建仓事件”的权威来源。
    # 只要 ticker 在 _today_pending_imported 中，即使历史账本存在旧的
    # Observation / Stop_Loss_Hit / Period_Matured 等记录，也必须把“今天这一行”
    # 当作一笔全新的持仓，并强制纳入 review。绝不能再次依赖 ACTIVE_TAGS、
    # 最近30天、历史归档等条件，否则会出现“账本有记录、报告没有”的问题。
    # ==========================================================
    _today_pending_imported = {str(x).strip() for x in (_today_pending_imported or set()) if str(x).strip()}
    if _today_pending_imported:
        _today_rows_all = df[
            df['Date'].dt.strftime('%Y-%m-%d').eq(today_str) &
            df['Ticker'].astype(str).str.strip().isin(_today_pending_imported)
        ].copy()

        if not _today_rows_all.empty:
            # 今日新仓一律恢复成真实持仓状态。只改今天，不碰历史记录。
            _today_rows_all.loc[:, 'Tag'] = 'Core_Dragon'

            # 同步修复 df，保证后面所有流程看到的也是活跃持仓。
            _today_key_mask = (
                df['Date'].dt.strftime('%Y-%m-%d').eq(today_str) &
                df['Ticker'].astype(str).str.strip().isin(_today_pending_imported)
            )
            df.loc[_today_key_mask, 'Tag'] = 'Core_Dragon'

            # 从最近30天主集合里删除同 ticker 的旧生命周期，只加入今天的新仓。
            recent_picks = recent_picks[
                ~recent_picks['Ticker'].astype(str).str.strip().isin(_today_pending_imported)
            ].copy()
            recent_picks = pd.concat([recent_picks, _today_rows_all], ignore_index=True)

            print(f"📌 强制纳入今日新仓 {len(_today_rows_all)} 条：{sorted(_today_pending_imported)}")
        else:
            print(f"🚨 今日 pending 已确认 {sorted(_today_pending_imported)}，但 trade_history 找不到对应日期记录！")
    if recent_picks.empty:
        print("⚠️ 最近30天无活跃持仓，退出。")
        sys.exit(0)
    # 今日交易记录强校验：所有今天建立的记录都必须能在活跃持仓集合中找到。
    _today_all = df[df['Date'].dt.strftime('%Y-%m-%d') == today_str].copy()
    _today_active = _today_all[_today_all['Tag'].isin(ACTIVE_TAGS)]
    if not _today_all.empty:
        _today_missing_active = sorted(set(_today_all['Ticker'].astype(str).str.strip()) - set(_today_active['Ticker'].astype(str).str.strip()))
        if _today_missing_active:
            print(f"🚨 [T+1一致性检查] 今天仍有未进入持仓集合的标的：{_today_missing_active}")
        else:
            print(f"✅ [T+1一致性检查] 今天 {len(_today_all)} 条记录全部处于活跃持仓状态")

    print(f"📊 加载到 {len(recent_picks)} 条活跃持仓记录")
    # 打印摘要
    for ticker, group in recent_picks.groupby('Ticker'):
        latest = group.sort_values('Date').iloc[-1]
        print(f"   {ticker}: 最新日期={latest['Date'].strftime('%Y-%m-%d')}, Tag={latest['Tag']}, Hold_Period={latest.get('Hold_Period', 'N/A')}")
except Exception as e:
    print(f"⚠️ 读取账本失败: {e}")
    sys.exit(1)

# 版本兼容：股票采用动态持有，不再要求 Hold_Period 必须存在。
# 旧账本仍保留 Hold_Period 字段，仅作为历史统计信息。
for col in ['Hold_Period', 'Stop_Loss', 'Stop_Method', 'Trail_Stop', 'MA20', 'MA50', 'Score',
            'ATR_Pct', 'PE_TTM', 'EPS_TTM', 'PB', 'Earnings_Growth', 'ROE', '估值评分', '估值结论']:
    if col not in recent_picks.columns:
        recent_picks[col] = ''

# Observation 等非真实持仓仍保持原逻辑；这里只过滤明显已终止的记录。
if recent_picks.empty:
    print("⚠️ 无有效持仓，退出。")
    sys.exit(0)

# ==========================================
# 4. 获取历史行情（含 OHLC）
# ==========================================
start_hist = (get_bj_time() - datetime.timedelta(days=120)).strftime('%Y%m%d')
end_hist = get_bj_time().strftime('%Y%m%d')
all_tickers = recent_picks['Ticker'].unique().tolist()

try:
    df_hist_all = pro.daily(
        ts_code=",".join(all_tickers),
        start_date=start_hist,
        end_date=end_hist,
        fields='ts_code,trade_date,open,high,low,close'
    ).sort_values(['ts_code', 'trade_date'])
except:
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


def _indicator_frame_ashare(ticker, before_date_str):
    """返回指定股票截至 before_date 前一交易日的 OHLC 技术指标数据。"""
    if df_hist_all.empty:
        return pd.DataFrame()
    sub = df_hist_all[df_hist_all['ts_code'].astype(str).str.strip() == str(ticker).strip()].copy()
    if sub.empty:
        return sub
    sub['trade_date'] = pd.to_datetime(sub['trade_date'], errors='coerce')
    target = pd.to_datetime(before_date_str, errors='coerce')
    if pd.isna(target):
        return pd.DataFrame()
    sub = sub[sub['trade_date'] < target].copy().sort_values('trade_date')
    for c in ['open', 'high', 'low', 'close']:
        sub[c] = pd.to_numeric(sub[c], errors='coerce')
    return sub.dropna(subset=['high', 'low', 'close']).copy()


def get_trailing_stop_context_ashare(ticker, existing_stop=None, before_date_str=None):
    """
    A股动态移动止损：MA20 / MA50 + ATR + MACD / KDJ。
    止损线只允许上移，不允许因为波动突然放大而向下扩大风险。
    """
    before_date_str = before_date_str or today_str
    hist = _indicator_frame_ashare(ticker, before_date_str)
    if len(hist) < 20:
        return {
            'exec_stop': float(existing_stop) if existing_stop not in (None, '') else None,
            'candidate': float(existing_stop) if existing_stop not in (None, '') else None,
            'ma20': None, 'ma50': None, 'atr': None, 'atr_pct': None,
            'macd_bearish': False, 'kdj_falling': False,
            'method': '数据不足，沿用原止损'
        }

    close = hist['close']
    high = hist['high']
    low = hist['low']
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])

    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    latest_close = float(close.iloc[-1])
    if latest_close <= 0 or atr <= 0:
        return {
            'exec_stop': float(existing_stop) if existing_stop not in (None, '') else None,
            'candidate': float(existing_stop) if existing_stop not in (None, '') else None,
            'ma20': ma20, 'ma50': ma50, 'atr': atr, 'atr_pct': None,
            'macd_bearish': False, 'kdj_falling': False,
            'method': '技术数据不足，沿用原止损'
        }

    atr_pct = atr / latest_close * 100.0
    stop_pct = max(3.0, min(12.0, atr_pct * 2.0))
    candidates = [latest_close * (1 - stop_pct / 100.0)]
    if ma20 > 0:
        candidates.append(ma20 - atr)
    if ma50 > 0:
        candidates.append(ma50 - 1.5 * atr)

    # 趋势尚未破坏时，保护线跟随价格/均线抬高；不因为单日波动把保护线下移。
    candidate = max(candidates)

    macd_bearish = False
    kdj_falling = False
    try:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_bearish = bool(dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-1] < dif.iloc[-2])
    except Exception:
        pass

    try:
        low9 = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv = (close - low9) / (high9 - low9).replace(0, pd.NA) * 100
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        j = 3 * k - 2 * d
        kdj_falling = bool(j.iloc[-1] < j.iloc[-2] and j.iloc[-1] < d.iloc[-1])
    except Exception:
        pass

    if macd_bearish and kdj_falling:
        candidate = max(candidate, latest_close - 1.5 * atr)

    # 不允许保护线高于最新收盘的 98%，避免正常波动造成“机械即卖”。
    candidate = min(candidate, latest_close * 0.98)

    old = None
    try:
        old = float(existing_stop)
        if old <= 0:
            old = None
    except Exception:
        old = None
    exec_stop = max(old, candidate) if old is not None else candidate

    methods = ['MA20/MA50', f'ATR×{stop_pct/atr_pct:.1f}' if atr_pct else 'ATR', 'MACD/KDJ']
    if macd_bearish and kdj_falling:
        methods.append('MACD+KDJ转弱加严')
    return {
        'exec_stop': round(float(exec_stop), 2),
        'candidate': round(float(candidate), 2),
        'ma20': round(ma20, 2), 'ma50': round(ma50, 2),
        'atr': round(atr, 3), 'atr_pct': round(atr_pct, 2),
        'macd_bearish': macd_bearish,
        'kdj_falling': kdj_falling,
        'method': '+'.join(methods)
    }


def update_trade_history_trailing_stop_ashare(ticker, rec_date_str, stop_context):
    """把新的移动止损线与技术状态写回 trade_history.csv。"""
    if not os.path.exists(log_file) or not stop_context:
        return
    try:
        dfx = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='warn')
        for col in ['Stop_Method', 'Trail_Stop', 'Stop_Loss', 'MA20', 'MA50', 'ATR_Pct']:
            if col not in dfx.columns:
                dfx[col] = ''
            dfx[col] = dfx[col].astype(object)
        mask = (
            dfx['Ticker'].astype(str).str.strip().eq(str(ticker).strip()) &
            dfx['Date'].astype(str).str[:10].eq(str(rec_date_str)[:10])
        )
        if not mask.any():
            return
        stop = stop_context.get('exec_stop')
        if stop is not None:
            dfx.loc[mask, 'Stop_Loss'] = round(float(stop), 2)
            dfx.loc[mask, 'Trail_Stop'] = round(float(stop), 2)
        dfx.loc[mask, 'Stop_Method'] = stop_context.get('method', 'MA20/MA50 + ATR + MACD/KDJ')
        if stop_context.get('ma20') is not None:
            dfx.loc[mask, 'MA20'] = stop_context['ma20']
        if stop_context.get('ma50') is not None:
            dfx.loc[mask, 'MA50'] = stop_context['ma50']
        if stop_context.get('atr_pct') is not None:
            dfx.loc[mask, 'ATR_Pct'] = stop_context['atr_pct']
        dfx.to_csv(log_file, index=False, encoding='utf-8')
    except Exception as e:
        print(f"⚠️ {ticker} 移动止损写回 trade_history 失败: {e}")

# 加载历史归档去重
already_archived = set()
cooldown_tickers = set()
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
        # 【修复】止损后冷却期：最近5天内已止损/清仓的标的禁止重新进入持仓
        if {'Ticker', 'Review_Date', 'Status'}.issubset(existing_review.columns):
            existing_review['Review_Date'] = pd.to_datetime(existing_review['Review_Date'], errors='coerce')
            _cutoff = get_bj_time() - datetime.timedelta(days=5)
            _recent_exit = existing_review[
                (existing_review['Review_Date'] >= _cutoff.replace(tzinfo=None)) &
                (existing_review['Status'].isin(['止损触发清仓', '突发清仓暂停']))
            ]
            cooldown_tickers = set(_recent_exit['Ticker'].astype(str).str.strip().unique())
            if cooldown_tickers:
                print(f"🚫 冷却期过滤：最近5天内已止损/清仓 {len(cooldown_tickers)} 只：{sorted(cooldown_tickers)}")
    except Exception as e:
        print(f"⚠️ 归档/冷却期读取失败: {e}")
        pass

# ==========================================
# 5.5 A股 T+1：读取前一交易日“止损待执行”标的
# ==========================================
pending_t1_stop = {}
if os.path.exists(review_log_path) and os.path.getsize(review_log_path) > 0:
    try:
        _review_t1 = pd.read_csv(review_log_path, on_bad_lines='skip', keep_default_na=False)
        required_t1_cols = {'Review_Date', 'Ticker', 'Status'}
        if required_t1_cols.issubset(_review_t1.columns):
            _review_t1['Review_Date'] = pd.to_datetime(_review_t1['Review_Date'], errors='coerce')
            _review_t1['Ticker'] = _review_t1['Ticker'].astype(str).str.strip()
            _today_dt = pd.to_datetime(today_str)
            _review_t1 = _review_t1[
                _review_t1['Review_Date'].notna() &
                (_review_t1['Review_Date'] <= _today_dt)
            ].sort_values(['Ticker', 'Review_Date'])

            # 每只股票只看“最近一条复盘状态”。只有最近状态仍是 T+1止损待执行，
            # 且该状态来自今天之前，今天才允许执行。这样可以避免旧的待执行记录
            # 在股票已经重新开仓/恢复持有后被误触发。
            _latest_review_by_ticker = _review_t1.groupby('Ticker', as_index=False).tail(1)
            for _, _r in _latest_review_by_ticker.iterrows():
                _ticker_key = str(_r['Ticker']).strip()
                if (
                    str(_r['Status']).strip() == 'T+1止损待执行' and
                    _r['Review_Date'] < _today_dt
                ):
                    pending_t1_stop[_ticker_key] = _r.to_dict()

            if pending_t1_stop:
                print(f"📌 T+1联动：发现 {len(pending_t1_stop)} 只前一交易日已触发止损、今日可执行的标的")
    except Exception as e:
        print(f"⚠️ T+1止损待执行记录读取失败：{e}")

# ==========================================
# 6. 遍历持仓，执行动态止损
# ==========================================
active_list = []
expired_list = []  # 保留变量以兼容旧报表；股票不再按持仓天数触发到期退出
skipped_duplicate = 0

print(f"开始处理 {len(recent_picks)} 条活跃持仓记录：股票动态持有，仅以移动止损/趋势破坏退出...")

_ALL_HOLDING_TAGS = {'Core_Double_Dragon', 'Core_Dragon', 'Sub_Pioneer'}

for ticker, group in recent_picks.groupby('Ticker'):
    group = group.sort_values('Date').copy()
    _ticker_key = str(ticker).strip()

    # 【修复】跳过处于止损后冷却期的标的（5天内已止损/清仓的不重新进入持仓）
    if _ticker_key in cooldown_tickers:
        print(f"🚫 {_ticker_key} 处于止损后冷却期（5天），跳过")
        continue

    if _ticker_key in _today_pending_imported:
        today_rows = group[group['Date'].dt.strftime('%Y-%m-%d').eq(today_str)].copy()
        if today_rows.empty:
            continue
        group = today_rows.sort_values('Date').copy()
        print(f"🆕 {ticker} 今日新仓：隔离历史生命周期，T+1锁定。")
    else:
        try:
            ticker_all = df[df['Ticker'].astype(str).str.strip().eq(_ticker_key)].sort_values('Date').reset_index(drop=True)
            active_pos = ticker_all[ticker_all['Tag'].astype(str).str.strip().isin(_ALL_HOLDING_TAGS)]
            if not active_pos.empty:
                last_active_idx = active_pos.index[-1]
                lifecycle = []
                for pos in range(int(last_active_idx), -1, -1):
                    if str(ticker_all.iloc[pos]['Tag']).strip() not in _ALL_HOLDING_TAGS:
                        break
                    lifecycle.append(pos)
                lifecycle.reverse()
                if lifecycle:
                    group = ticker_all.iloc[lifecycle].copy()
        except Exception as e:
            print(f"⚠️ {ticker} 生命周期恢复失败，沿用最近持仓记录：{e}")

    group = group.sort_values('Date')
    first_row = group.iloc[0]
    latest_row = group.iloc[-1]
    rec_date_str = first_row['Date'].strftime('%Y-%m-%d')
    latest_date_str = latest_row['Date'].strftime('%Y-%m-%d')
    is_new_today = latest_date_str == today_str
    t1_locked = is_new_today
    days_held = max(0, (get_bj_time().replace(tzinfo=None) - first_row['Date']).days)

    latest_tag = str(latest_row.get('Tag', '')).strip()
    if latest_tag in {'Trap_Warning', 'Forced_Exit', 'Stop_Loss_Hit', 'Period_Matured', 'Dropped'} and not (is_new_today and _ticker_key in _today_pending_imported):
        continue

    # 兼容历史账本，但股票今后统一标记为“动态持有”。
    score_str = str(latest_row.get('Score', 'N/A')).strip() or 'N/A'
    hold_period_str = '动态持有'
    try:
        rec_price = float(first_row.get('Open_Price', 0) or 0)
    except Exception:
        rec_price = 0.0
    if rec_price <= 0:
        try:
            rec_price = float(first_row.get('Close_Price', 0) or 0)
        except Exception:
            rec_price = 0.0

    # 今日实际 OHLC：不要再错误地把建仓日 Low_Price 当作今日最低价。
    today_open = get_price_on_date(ticker, today_str, field='open')
    today_low = get_price_on_date(ticker, today_str, field='low')
    today_close = get_price_on_date(ticker, today_str, field='close')
    if today_open is None or today_close is None:
        try:
            live_open, live_last = get_live_quote(ticker)
            today_open = today_open or live_open
            today_close = today_close or live_last or live_open
        except Exception:
            pass
    if today_low is None and today_open is not None and today_close is not None:
        today_low = min(today_open, today_close)
    if today_close is None:
        today_close = price_map_today.get(ticker)
    if today_close is None:
        continue
    if is_new_today and today_open is not None and today_open > 0:
        rec_price = float(today_open)

    existing_stop = None
    # 优先沿用上一轮已经抬高的 Trail_Stop。
    for _, rr in group.iloc[::-1].iterrows():
        for key in ('Trail_Stop', 'Stop_Loss'):
            try:
                vv = float(rr.get(key, 0) or 0)
                if vv > 0:
                    existing_stop = vv
                    break
            except Exception:
                pass
        if existing_stop is not None:
            break

    stop_ctx = get_trailing_stop_context_ashare(ticker, existing_stop=existing_stop, before_date_str=today_str)
    exec_stop = stop_ctx.get('exec_stop')

    # 【修复】技术指标数据不足且没有历史止损时，使用买入价默认止损
    if exec_stop is None and rec_price > 0:
        exec_stop = round(rec_price * 0.95, 2)
        stop_ctx['exec_stop'] = exec_stop
        stop_ctx['candidate'] = exec_stop
        stop_ctx['method'] = '默认止损(买入价-5%)'
        print(f"⚠️ {ticker} 技术指标数据不足，启用默认止损 {exec_stop}")

    update_trade_history_trailing_stop_ashare(ticker, rec_date_str, stop_ctx)

    t1_stop_triggered_today = False

    # 先处理昨日触发、今日允许执行的 T+1 止损。
    if _ticker_key in pending_t1_stop and not is_new_today:
        if today_open is not None and float(today_open) > 0 and (str(ticker), rec_date_str) not in already_archived:
            exit_price = float(today_open)
            pnl = round((exit_price - rec_price) / rec_price * 100, 2) if rec_price > 0 else 0.0
            try:
                df_stop = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='warn')
                for col in ['Exit_Price', 'Exit_Date', 'Stop_Method', 'Trail_Stop', 'MA20', 'MA50', 'ATR_Pct']:
                    if col not in df_stop.columns:
                        df_stop[col] = ''
                    df_stop[col] = df_stop[col].astype(object)
                mask = (df_stop['Ticker'].astype(str).str.strip().eq(_ticker_key) & ~df_stop['Tag'].astype(str).isin({'Stop_Loss_Hit','Forced_Exit','Dropped','Trap_Warning','Period_Matured'}))
                df_stop.loc[mask, 'Tag'] = 'Stop_Loss_Hit'
                df_stop.loc[mask, 'Exit_Price'] = exit_price
                df_stop.loc[mask, 'Exit_Date'] = today_str
                if exec_stop is not None:
                    df_stop.loc[mask, 'Trail_Stop'] = exec_stop
                    df_stop.loc[mask, 'Stop_Loss'] = exec_stop
                df_stop.to_csv(log_file, index=False, encoding='utf-8')
            except Exception as e:
                print(f"⚠️ T+1执行止损写回失败 {ticker}: {e}")
            expired_list.append({
                '代码': ticker, '名称': first_row.get('Name', ticker), '标签': 'Stop_Loss_Hit',
                '推荐评分': score_str, '持股周期建议': hold_period_str, '止损价': exec_stop if exec_stop is not None else 'N/A',
                '首次推荐日': rec_date_str, '首次推荐价': rec_price, '期满日': '', '期满日价格': exit_price,
                '期满日盈亏(%)': pnl, '持仓天数': days_held, '系统连续推荐次数': len(group),
                '结算类型': '止损触发清仓', '执行说明': '昨日触发止损，A股T+1今日开盘执行',
                'Stop_Method': stop_ctx.get('method', 'MA20/MA50 + ATR + MACD/KDJ')
            })
            continue

    # 今日最低价触发保护线。T+1 新仓只能记录待执行。
    if exec_stop is not None and today_low is not None and float(today_low) <= float(exec_stop):
        if is_new_today:
            t1_stop_triggered_today = True
            print(f"⏳ [T+1锁定] {ticker} 今日最低价 {today_low:.2f} <= 移动止损 {exec_stop:.2f}，下一交易日执行")
        elif (str(ticker), rec_date_str) not in already_archived:
            exit_price = min(float(exec_stop), float(today_open)) if today_open is not None and float(today_open) > 0 and float(today_open) < float(exec_stop) else float(exec_stop)
            pnl = round((exit_price - rec_price) / rec_price * 100, 2) if rec_price > 0 else 0.0
            try:
                df_stop = pd.read_csv(log_file, keep_default_na=False, on_bad_lines='warn')
                for col in ['Exit_Price', 'Exit_Date', 'Stop_Method', 'Trail_Stop', 'MA20', 'MA50', 'ATR_Pct']:
                    if col not in df_stop.columns:
                        df_stop[col] = ''
                    df_stop[col] = df_stop[col].astype(object)
                mask = (df_stop['Ticker'].astype(str).str.strip().eq(_ticker_key) & ~df_stop['Tag'].astype(str).isin({'Stop_Loss_Hit','Forced_Exit','Dropped','Trap_Warning','Period_Matured'}))
                df_stop.loc[mask, 'Tag'] = 'Stop_Loss_Hit'
                df_stop.loc[mask, 'Exit_Price'] = exit_price
                df_stop.loc[mask, 'Exit_Date'] = today_str
                df_stop.loc[mask, 'Stop_Loss'] = float(exec_stop)
                df_stop.loc[mask, 'Trail_Stop'] = float(exec_stop)
                df_stop.loc[mask, 'Stop_Method'] = stop_ctx.get('method', 'MA20/MA50 + ATR + MACD/KDJ')
                df_stop.to_csv(log_file, index=False, encoding='utf-8')
            except Exception as e:
                print(f"⚠️ 更新 {ticker} 止损状态失败: {e}")
            expired_list.append({
                '代码': ticker, '名称': first_row.get('Name', ticker), '标签': 'Stop_Loss_Hit',
                '推荐评分': score_str, '持股周期建议': hold_period_str, '止损价': exec_stop,
                '首次推荐日': rec_date_str, '首次推荐价': rec_price, '期满日': '', '期满日价格': exit_price,
                '期满日盈亏(%)': pnl, '持仓天数': days_held, '系统连续推荐次数': len(group),
                '结算类型': '止损触发清仓', '执行说明': '移动止损触发，按可执行价格模拟结算',
                'Stop_Method': stop_ctx.get('method', 'MA20/MA50 + ATR + MACD/KDJ')
            })
            continue

    cur_pnl = round((float(today_close) - rec_price) / rec_price * 100, 2) if rec_price > 0 else 0.0
    active_list.append({
        '代码': ticker, '名称': first_row.get('Name', ticker), '标签': latest_tag, '推荐评分': score_str,
        '持股周期建议': hold_period_str, '止损价': exec_stop if exec_stop is not None else 'N/A',
        '首次推荐日': rec_date_str, '首次推荐价': rec_price,
        '今日开盘价': round(float(today_open), 2) if today_open is not None else 'N/A',
        '现价': float(today_close), '持仓天数': days_held, '剩余天数': '—',
        '当前盈亏(%)': cur_pnl, '今日新增': '是' if is_new_today else '否',
        'T+1锁定': '是' if t1_locked else '否',
        '风控状态': 'T+1止损待执行' if t1_stop_triggered_today else ('T+1锁定' if t1_locked else '正常监控'),
        '系统连续推荐次数': len(group), 'Stop_Method': stop_ctx.get('method', 'MA20/MA50 + ATR + MACD/KDJ'),
        'Trail_Stop': exec_stop if exec_stop is not None else 'N/A', 'MA20': stop_ctx.get('ma20', 'N/A'),
        'MA50': stop_ctx.get('ma50', 'N/A'), 'ATR_Pct': stop_ctx.get('atr_pct', 'N/A'),
        'MACD状态': '偏空' if stop_ctx.get('macd_bearish') else '未转空',
        'KDJ状态': 'J线回落' if stop_ctx.get('kdj_falling') else '未明显转弱',
        'PE_TTM': first_row.get('PE_TTM', ''), 'EPS_TTM': first_row.get('EPS_TTM', ''),
        'PB': first_row.get('PB', ''), 'Earnings_Growth': first_row.get('Earnings_Growth', ''),
        'ROE': first_row.get('ROE', ''), '估值评分': first_row.get('估值评分', ''), '估值结论': first_row.get('估值结论', '')
    })

# ==========================================================
# 今日新仓最终一致性校验：任何 pending 导入的股票都必须在 active_list。
# 这里不再相信前面的中间状态，直接做最终结果核验。
# ==========================================================
_active_today_tickers = {str(x.get('代码', '')).strip() for x in active_list if x.get('今日新增') == '是'}
_missing_today = sorted(_today_pending_imported - _active_today_tickers)
if _missing_today:
    print(f"🚨 [最终一致性失败] 今日 pending 标的仍未进入持仓报告：{_missing_today}")
    # 最后一道保险：从 trade_history 的今天记录直接构造 active_list。
    for _miss in _missing_today:
        _rows = df[(df['Date'].dt.strftime('%Y-%m-%d').eq(today_str)) & (df['Ticker'].astype(str).str.strip().eq(_miss))].copy()
        if _rows.empty:
            continue
        _r = _rows.sort_values('Date').iloc[-1]
        try:
            _open = float(_r.get('Open_Price', 0) or _r.get('Close_Price', 0))
        except Exception:
            _open = 0.0
        try:
            _cur = float(price_map_today.get(_miss, _r.get('Close_Price', _open)))
        except Exception:
            _cur = _open
        try:
            _sl = str(_r.get('Stop_Loss', 'N/A'))
        except Exception:
            _sl = 'N/A'
        _pnl = round((_cur - _open) / _open * 100, 2) if _open > 0 else 0.0
        active_list.append({
            '代码': _miss,
            '名称': _r.get('Name', _miss),
            '标签': 'Core_Dragon',
            '推荐评分': _r.get('Score', 'N/A'),
            '持股周期建议': '动态持有',
            '止损价': _sl,
            '首次推荐日': today_str,
            '首次推荐价': _open,
            '今日开盘价': _open,
            '现价': _cur,
            '持仓天数': 0,
            '剩余天数': '—',
            '当前盈亏(%)': _pnl,
            '今日新增': '是',
            'T+1锁定': '是',
            '风控状态': 'T+1锁定',
            '系统连续推荐次数': 1,
        })
    _active_today_tickers = {str(x.get('代码', '')).strip() for x in active_list if x.get('今日新增') == '是'}
    _missing_today = sorted(_today_pending_imported - _active_today_tickers)
    if _missing_today:
        print(f"❌ [最终一致性失败] 仍无法构造今日持仓：{_missing_today}")
    else:
        print(f"✅ [最终一致性修复] 今日 pending 全部进入持仓：{sorted(_active_today_tickers)}")
else:
    print(f"✅ [最终一致性通过] 今日 pending 全部进入持仓：{sorted(_active_today_tickers)}")

print(f"✅ 持仓中: {len(active_list)} 只 | 本次新归档: {len(expired_list)} 只（移动止损/T+1执行）")
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

【已关闭交易（本次新归档；股票只因移动止损/趋势破坏等实际退出原因关闭，不存在股票固定到期日）】：
{expired_list}

在风控判断或策略复盘时，请结合推荐评分进行验证：高分票（80分以上）如果出现明显亏损，需要特别指出"高信心预期未兑现"；低分票（60分以下）如果反而盈利良好，也需要指出"评分体系可能过于保守"。

【A股T+1交易规则——必须严格遵守】
1. "今日新增"="是"代表今天开盘建立的新仓，当天绝对禁止卖出、止损清仓或减仓。
2. 即使今日盘中最低价跌破止损位，也只能标记为"T+1止损待执行"，不能在今天生成"止损触发清仓"归档。
3. 下一交易日若昨日已经触发止损，允许按今日开盘价作为模拟执行价完成清仓归档；这是T+1后的实际可执行口径。
4. 今日新增仍然必须纳入当前盈亏分析、胜率统计和报告，但风控动作必须写为"持有观察，T+1锁定；若止损已触发则下一交易日执行"。
5. 绝对不要把今天新仓的盘中低点直接当成已经成交的止损价。

请严格按以下 HTML 骨架输出复盘报告（直出HTML，禁加markdown框，盈利标红，亏损标绿）：

<div style="background: #eceff1; border-left: 6px solid #455a64; padding: 20px; margin-bottom: 25px; border-radius: 8px;">
    <h3 style="margin-top: 0; color: #263238;">⚖️ 盘后总体风控审查</h3>
    <p>(总结持仓中标的整体盈亏状况，以及本次新归档标的的策略胜率评估，特别指出评分与实际表现是否存在明显反差；若有今日新增标的，在此提一句今日共新增几只)</p>
</div>

<h2 style="color: #1565c0; border-bottom: 2px solid #1565c0; padding-bottom: 5px;">📊 持仓中 - 风控纪律核对单</h2>
<div style="background: #fff; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
    <h3 style="margin: 0 0 10px 0;">[若"今日新增"="是"则在最前面加一个 🆕今日新增 徽章] [首次推荐日] | [股票名称] ([代码]) | 评分[推荐评分]/100 | 系统连续推荐[N]次 | 动态持有</h3>
    <p><b>持股状态:</b> 动态持有 | <b>当前移动止损位:</b> [止损价] | <b>止损方法:</b> [Stop_Method]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] ➔ <b>现价:</b> ¥[现价]（今日开盘价 ¥[今日开盘价]） | <b>当前盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[当前盈亏(%)]%</span></p>
    <p><span style="background: #607d8b; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">风控动作指令</span>
    (严格遵守A股T+1：今日新增只能持有观察；今日若触发止损，只记录为T+1待执行；非新仓才可正常执行止损/减仓)</p>
</div>

<h2 style="color: #37474f; border-bottom: 2px solid #cfd8dc; padding-bottom: 5px; margin-top: 40px;">📁 已超期归档 - 策略复盘评价</h2>
<div style="background: #f5f5f5; padding: 20px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #e0e0e0;">
    <h3 style="margin: 0 0 10px 0;">[首次推荐日] | [股票名称] ([代码]) | 评分[推荐评分]/100 | 期满日:[期满日]</h3>
    <p><b>持股状态:</b> 动态持有 | <b>当前移动止损位:</b> [止损价] | <b>止损方法:</b> [Stop_Method]</p>
    <p><b>买入成本:</b> ¥[首次推荐价] → <b>期满日价格:</b> ¥[期满日价格] | <b>策略实际盈亏:</b> <span style="font-weight:bold; color:[盈利#d32f2f/亏损#388e3c];">[期满日盈亏(%)]%</span></p>
    <p><span style="background: #455a64; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 12px;">策略复盘</span>
    (评价这次策略是否成功，归因分析盈亏原因；若为T+1止损，则说明触发日与实际执行日的差异)</p>
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
archive_cols = [
    "Review_Date","Ticker","Name","Tag","Rec_Date","Rec_Price","Cur_Price","Days_Held",
    "PnL_Pct","Maturity_PnL","Hold_Period","Stop_Loss","Stop_Method","Trail_Stop",
    "MA20","MA50","ATR_Pct","Rec_Count","Status","Score","PE_TTM","EPS_TTM","PB",
    "Earnings_Growth","ROE","估值评分","估值结论"
]

# 兼容旧 review_history：有旧列就保留数据，缺失的新列补空，并统一表头。
try:
    if os.path.exists(review_log) and os.path.getsize(review_log) > 0:
        _old_review = pd.read_csv(review_log, keep_default_na=False, on_bad_lines='skip')
    else:
        _old_review = pd.DataFrame()
    for _c in archive_cols:
        if _c not in _old_review.columns:
            _old_review[_c] = ''
    _old_review = _old_review[archive_cols]
    _old_review.to_csv(review_log, index=False, encoding='utf-8')
    print("✅ review_history.csv 表结构已升级/对齐")
except Exception as e:
    print(f"⚠️ review_history.csv 表结构升级失败，将尝试直接追加：{e}")

try:
    review_date = get_bj_time().strftime('%Y-%m-%d')
    rows_to_append = []

    for item in active_list:
        _status = item.get('风控状态', '正常监控')
        rows_to_append.append({
            "Review_Date": review_date, "Ticker": item.get('代码',''), "Name": item.get('名称',''),
            "Tag": item.get('标签',''), "Rec_Date": item.get('首次推荐日',''), "Rec_Price": item.get('首次推荐价',''),
            "Cur_Price": item.get('现价',''), "Days_Held": item.get('持仓天数',0), "PnL_Pct": item.get('当前盈亏(%)',''),
            "Maturity_PnL": '', "Hold_Period": '动态持有', "Stop_Loss": item.get('止损价',''),
            "Stop_Method": item.get('Stop_Method',''), "Trail_Stop": item.get('Trail_Stop',''), "MA20": item.get('MA20',''),
            "MA50": item.get('MA50',''), "ATR_Pct": item.get('ATR_Pct',''), "Rec_Count": item.get('系统连续推荐次数',''),
            "Status": 'T+1止损待执行' if _status == 'T+1止损待执行' else '持仓中', "Score": item.get('推荐评分',''),
            "PE_TTM": item.get('PE_TTM',''), "EPS_TTM": item.get('EPS_TTM',''), "PB": item.get('PB',''),
            "Earnings_Growth": item.get('Earnings_Growth',''), "ROE": item.get('ROE',''),
            "估值评分": item.get('估值评分',''), "估值结论": item.get('估值结论','')
        })

    for item in expired_list:
        rows_to_append.append({
            "Review_Date": review_date, "Ticker": item.get('代码',''), "Name": item.get('名称',''),
            "Tag": item.get('标签','Stop_Loss_Hit'), "Rec_Date": item.get('首次推荐日',''), "Rec_Price": item.get('首次推荐价',''),
            "Cur_Price": item.get('期满日价格',''), "Days_Held": item.get('持仓天数',0),
            "PnL_Pct": item.get('期满日盈亏(%)',''), "Maturity_PnL": '', "Hold_Period": '动态持有',
            "Stop_Loss": item.get('止损价',''), "Stop_Method": item.get('Stop_Method',''), "Trail_Stop": item.get('止损价',''),
            "MA20": '', "MA50": '', "ATR_Pct": item.get('ATR_Pct',''), "Rec_Count": item.get('系统连续推荐次数',''),
            "Status": item.get('结算类型','止损触发清仓'), "Score": item.get('推荐评分',''), "PE_TTM": '', "EPS_TTM": '',
            "PB": '', "Earnings_Growth": '', "ROE": '', "估值评分": '', "估值结论": ''
        })

    if rows_to_append:
        _append_df = pd.DataFrame(rows_to_append, columns=archive_cols)
        _append_df.to_csv(review_log, mode='a', header=False, index=False, encoding='utf-8')
    print(f"✅ 归档写入成功：{len(rows_to_append)} 条")
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
    except:
        pass

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
