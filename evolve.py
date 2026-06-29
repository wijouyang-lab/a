# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import ast
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

print("启动 A股 scan.py 自动进化引擎（事件驱动与宏观风控版，版本公平评估）...")

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

def get_version_start_date():
    """
    读取 scan_version.txt，获取当前 scan.py 版本的生效起始日期。
    找不到标记文件时保守按"今天"计算，避免拿旧版本的数据误杀新版本。
    """
    version_file = "scan_version.txt"
    if not os.path.exists(version_file):
        print("⚠️ 未找到 scan_version.txt，无法确认当前版本起始日期，保守按今天计算。")
        return get_bj_time().replace(tzinfo=None)
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if "," in content:
            date_str = content.split(",")[1]
            return datetime.datetime.strptime(date_str, '%Y-%m-%d')
    except Exception as e:
        print(f"⚠️ 读取版本标记失败: {e}，保守按今天计算。")
    return get_bj_time().replace(tzinfo=None)

version_start_date = get_version_start_date()
print(f"📌 当前 scan.py 版本生效起始日期: {version_start_date.strftime('%Y-%m-%d')}")
print("📌 评估方法：只统计【该日期之后首次推荐】且【已到期归档】的交易，持仓中的票不计入胜率")

review_log = "review_history.csv"
if not os.path.exists(review_log):
    print("⚠️ 复盘账本不存在，跳过进化。需要先积累复盘数据。")
    exit(0)

try:
    df = pd.read_csv(review_log, on_bad_lines='skip')
    df['Rec_Date'] = pd.to_datetime(df['Rec_Date'])
except Exception as e:
    print(f"⚠️ 复盘账本读取失败: {e}")
    exit(1)

# ==========================================
# 第一层过滤：只看"当前版本"产生的推荐
# ==========================================
current_version_picks = df[df['Rec_Date'] >= version_start_date].copy()

if current_version_picks.empty:
    print("⚠️ 当前版本下还没有任何推荐记录（可能刚切换版本不久），跳过进化。")
    exit(0)

distinct_tickers = current_version_picks['Ticker'].nunique()
print(f"📊 当前版本下共有 {distinct_tickers} 只不同标的产生过推荐记录")

# ==========================================
# 第二层过滤：只用"已超期归档"的数据算胜率
# ==========================================
ALL_CORE_TAGS = ['Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon']
# 已完成交易的三种状态：
#   已超期归档   —— review.py 盘后按理论到期日价格回溯计算（旧逻辑，作为兜底）
#   止损触发清仓 —— scan.py 阶段0b 盘中检测到现价跌破止损位，按现价计算（新）
#   周期到期清仓 —— scan.py 阶段0b 盘中检测到已达到/超过建议持股周期，按现价计算（新）
# 这三种都是"持仓已经平仓、结果已知"的真实交易，理应一起计入胜率，
# 而不是只用 review.py 那一种盘后回溯方式。
MATURED_STATUSES = ['已超期归档', '止损触发清仓', '周期到期清仓']

if 'Status' not in current_version_picks.columns:
    print("⚠️ review_history.csv 缺少 Status 列，无法区分持仓中/已到期，跳过进化。")
    exit(0)

matured = current_version_picks[
    (current_version_picks['Status'].isin(MATURED_STATUSES)) &
    current_version_picks['Tag'].isin(ALL_CORE_TAGS)
].copy()
matured['PnL_Pct'] = pd.to_numeric(matured['PnL_Pct'], errors='coerce')
matured = matured.dropna(subset=['PnL_Pct'])

still_active = current_version_picks[
    (current_version_picks['Status'] == '持仓中') &
    current_version_picks['Tag'].isin(ALL_CORE_TAGS)
].copy()
still_active_tickers = still_active['Ticker'].nunique()

print(f"📊 已到期归档（计入胜率）: {len(matured)} 条 | 仍持仓中（不计入，仅作参考）: {still_active_tickers} 只标的")

MIN_MATURED_SAMPLES = 10
if len(matured) < MIN_MATURED_SAMPLES:
    print(f"⚠️ 当前版本下已完成交易（已超期归档）样本只有 {len(matured)} 条，不足 {MIN_MATURED_SAMPLES} 条。")
    print("⚠️ 多数持仓可能还在进行中，暂不评判，跳过本次进化。")
    exit(0)

overall_win_rate = round((matured['PnL_Pct'] > 0).sum() / len(matured) * 100, 1)

stats = {}
for tag in ALL_CORE_TAGS:
    group = matured[matured['Tag'] == tag]
    if len(group) > 0:
        win = (group['PnL_Pct'] > 0).sum()
        stats[tag] = {
            "总数": len(group),
            "胜率": round(win / len(group) * 100, 1),
            "平均盈亏": round(group['PnL_Pct'].mean(), 2),
            "平均持仓天数": round(pd.to_numeric(group['Days_Held'], errors='coerce').mean(), 1)
        }

industry_stats = {}
if 'Industry' in matured.columns:
    for industry, grp in matured.groupby('Industry'):
        if len(grp) >= 2:
            win_rate = round((grp['PnL_Pct'] > 0).sum() / len(grp) * 100, 1)
            avg_pnl = round(grp['PnL_Pct'].mean(), 2)
            industry_stats[industry] = {"胜率": win_rate, "平均盈亏": avg_pnl, "样本数": len(grp)}

score_stats = {}
if 'Score' in matured.columns:
    score_valid = matured.copy()
    score_valid['Score'] = pd.to_numeric(score_valid['Score'], errors='coerce')
    score_valid = score_valid.dropna(subset=['Score'])
    if len(score_valid) >= 5:
        def score_bucket(s):
            if s >= 80: return "80-100分(高信心)"
            elif s >= 60: return "60-79分(中信心)"
            else: return "60分以下(低信心)"
        score_valid['Bucket'] = score_valid['Score'].apply(score_bucket)
        for bucket, grp in score_valid.groupby('Bucket'):
            if len(grp) >= 2:
                win_rate = round((grp['PnL_Pct'] > 0).sum() / len(grp) * 100, 1)
                avg_pnl = round(grp['PnL_Pct'].mean(), 2)
                score_stats[bucket] = {"胜率": win_rate, "平均盈亏": avg_pnl, "样本数": len(grp)}

# 按退出方式拆分胜率：用于判断"止损位是否设太紧/太松"、"建议持股周期是否合理"
exit_reason_stats = {}
if 'Status' in matured.columns:
    for status, grp in matured.groupby('Status'):
        if len(grp) >= 2:
            win_rate = round((grp['PnL_Pct'] > 0).sum() / len(grp) * 100, 1)
            avg_pnl = round(grp['PnL_Pct'].mean(), 2)
            exit_reason_stats[status] = {"胜率": win_rate, "平均盈亏": avg_pnl, "样本数": len(grp)}

print(f"📊 当前版本真实胜率（仅已到期归档）: {overall_win_rate}% | 各标签: {stats}")
if industry_stats: print(f"📊 行业胜率分布: {industry_stats}")
if score_stats: print(f"📊 评分区间胜率分布: {score_stats}")
if exit_reason_stats: print(f"📊 退出方式胜率分布（止损触发/周期到期/自然到期）: {exit_reason_stats}")

EVOLVE_THRESHOLD = 60
if overall_win_rate >= EVOLVE_THRESHOLD:
    print(f"✅ 胜率 {overall_win_rate}% 达标，本次无需进化。")
    exit(0)

print(f"⚠️ 胜率 {overall_win_rate}% 低于 {EVOLVE_THRESHOLD}%，触发进化引擎...")

try:
    with open("scan.py", "r", encoding="utf-8") as f:
        current_scan_code = f.read()
except Exception as e:
    print(f"⚠️ 读取 scan.py 失败: {e}")
    exit(1)

client = anthropic.Anthropic(
    api_key=os.environ.get("CLAWSOCKET_API_KEY"),
    base_url=os.environ.get("CLAWSOCKET_BASE_URL")
)

loss_samples = matured[matured['PnL_Pct'] < 0].copy()
win_samples = matured[matured['PnL_Pct'] > 0].copy()

score_section = f"\n【推荐评分区间胜率分布（用于判断评分体系是否有真实预测力）】：\n{score_stats}\n" if score_stats else "\n【推荐评分区间胜率分布】：暂无足够样本\n"
exit_reason_section = f"\n【退出方式胜率分布（区分止损触发清仓 / 周期到期清仓 / 已超期归档，用于判断止损比例与建议持股周期是否合理）】：\n{exit_reason_stats}\n" if exit_reason_stats else "\n【退出方式胜率分布】：暂无足够样本\n"

prompt = f"""
你是一个A股事件驱动与宏观量化结合的策略优化专家。当前系统是【事件+宏观大宗数据双重驱动版】，核心逻辑是：
宏观大势定调 → 产业链推演 → 个股新闻交叉验证 → 匹配资金活跃标的 → 技术面风控兜底。

【重要评估说明】：以下数据严格只包含本版本scan.py（自{version_start_date.strftime('%Y-%m-%d')}起生效）产生的、且已经持有到期满（已超期归档，真实结果已知）的交易，不包含仍在持仓中的票。

【当前版本真实持仓表现（仅已到期归档样本）】：
整体胜率：{overall_win_rate}%（目标>60%，样本数：{len(matured)}）
各标签细分：{stats}
行业胜率分布：{industry_stats}
{score_section}{exit_reason_section}

【亏损样本（最近{len(loss_samples.tail(10))}条）】：
{loss_samples.tail(10).to_string()}

【盈利样本（最近{len(win_samples.tail(10))}条）】：
{win_samples.tail(10).to_string()}

【当前 scan.py 代码】：
{current_scan_code}

【分析任务】：
请从以下几个维度分析胜率低的原因，并直接修改 scan.py 代码中的 `generate_ai_report` 的 prompt 提示词来优化策略：

1. 宏观与大宗数据敏锐度：
   - AI 是否未能有效利用传入的 10年期美债收益率、黄金、原油、铜 等数据进行趋势与噪音的区分？
   - 是否在宏观环境极度恶劣（如美债飙升、PCE爆表）时，依然强行看多高估值板块？

2. 事件逻辑与个股新闻交叉：
   - 是否在没有强事件时仍强行凑数选出 5 只股票？
   - 个股新闻是否存在隐蔽的负面消息未能被有效拦截？

3. 止损设置与评分系统：
   - 默认止损比例是否需要基于当前行情波动率进行调整？
   - 评分系统是否失效（低分胜率反而高）？如果失效，请在 Prompt 中重新校准打分权重（如加重“宏观大宗顺风”的权重）。

可以调整的内容：
- prompt 中的宏观推演指令（让 AI 必须更严格地关联国债/原油等数据定调）
- prompt 中的评分标准描述（重新校准打分逻辑）
- 止损比例（DEFAULT_STOP_LOSS_PCT）
- 涨跌幅过滤门槛

严格禁止：
- 不得改变代码整体架构、物理流控、邮件发送和 CSV 读写逻辑
- 不得修改模型名称（必须保持 claude-opus-4-8）
- 不得把系统改回纯技术指标驱动逻辑
- 不得删除"免死金牌"机制（无历史数据的票赋予占位符而不是删除）
- 不得删除1-100推荐评分机制及提取正则逻辑 `r'评分\\s*[:：]\\s*\\[?(\\d{{1,3}})\\s*/\\s*100'`
- 不得删除 scan.py 顶部的版本标记机制（update_version_marker）
- 不得删除阶段0a的持仓强制清仓逻辑（pre_scan_portfolio_review）
- 不得删除阶段0b的规则驱动卖出信号检测逻辑（check_rule_based_sell_signals，止损触发与持有到期判断，不依赖AI的纯数值判断）
- 不得删除统一卖出信号卡片渲染逻辑（build_sell_signal_card）及其在邮件最顶部的插入位置
- 不得删除或缩小 FROZEN_TAGS 集合（{{'Forced_Exit', 'Trap_Warning', 'Stop_Loss_Hit', 'Period_Matured'}}），这是防止已平仓标的被重复写入追踪的关键过滤器

【严格按以下格式输出，不要加任何其他内容】：

===REPORT_START===
<div style="background:#e8f5e9; border-left:6px solid #388e3c; padding:20px; border-radius:8px; margin-bottom:20px;">
<h3 style="color:#1b5e20; margin-top:0;">🔬 胜率诊断与宏观大宗风控调校</h3>
<p>(基于到期回溯数据，分析宏观指标利用度、逻辑质量及评分体系的缺陷)</p>
</div>
<div style="background:#e3f2fd; border-left:6px solid #1976d2; padding:20px; border-radius:8px;">
<h3 style="color:#0d47a1; margin-top:0;">🔧 本次改进内容</h3>
<ul>
<li>(改动1：具体说明在 prompt 中强化了什么宏观约束)</li>
<li>(改动2：...)</li>
</ul>
</div>
===REPORT_END===

===CODE_START===
(完整的改进后 scan.py 代码)
===CODE_END===
"""

raw_output = ""
with client.messages.stream(
    model="claude-opus-4-8",
    max_tokens=8000,
    temperature=0.2,
    messages=[{"role": "user", "content": prompt}]
) as stream:
    for text in stream.text_stream:
        raw_output += text

print("✅ Claude 进化方案生成完毕。")

report_html = ""
new_code = ""

try:
    if "===REPORT_START===" in raw_output and "===REPORT_END===" in raw_output:
        report_html = raw_output.split("===REPORT_START===")[1].split("===REPORT_END===")[0].strip()
    if "===CODE_START===" in raw_output and "===CODE_END===" in raw_output:
        new_code = raw_output.split("===CODE_START===")[1].split("===CODE_END===")[0].strip()
        new_code = new_code.replace("```python", "").replace("```", "").strip()
    if not new_code:
        print("⚠️ 未能提取到新代码，终止进化。")
        exit(1)
except Exception as e:
    print(f"⚠️ 解析失败: {e}")
    exit(1)

try:
    ast.parse(new_code)
    print("✅ 语法检查通过，准备覆盖 scan.py")
except SyntaxError as e:
    print(f"❌ 生成的代码有语法错误，终止进化，scan.py 保持不变: {e}")
    report_html += f"""
    <div style="background:#ffebee; border-left:6px solid #c62828; padding:20px; border-radius:8px; margin-top:20px;">
    <h3 style="color:#b71c1c; margin-top:0;">❌ 语法错误，本次进化已中止</h3>
    <p>生成的代码存在语法错误，scan.py 未被修改，系统继续使用原版本。</p>
    <p>错误详情：{str(e)}</p>
    </div>
    """
    def send_error_mail(report_html, win_rate):
        user = os.environ.get("EMAIL_ACCOUNT")
        pwd = os.environ.get("EMAIL_PASSWORD")
        if not user or not pwd: return
        style = "<style>body{font-family:sans-serif;background:#f4f6f9;color:#333;padding:20px;line-height:1.6}.container{max-width:900px;margin:0 auto;background:#fff;padding:30px;border-radius:10px;}</style>"
        full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'>{style}</head><body><div class='container'><h1 style='color:#c62828;text-align:center;'>⚠️ A股进化失败 - 语法错误</h1><p style='text-align:center;color:#666;'>触发胜率 <b style='color:#d32f2f;'>{win_rate}%</b>，但生成代码有语法错误，scan.py 未被修改</p>{report_html}</div></body></html>"
        msg = MIMEMultipart()
        msg['From'] = user
        msg['To'] = user
        msg['Subject'] = f"【A股进化失败】语法错误，scan.py 未修改 ({get_bj_time().strftime('%Y-%m-%d')})"
        msg.attach(MIMEText(full_html, 'html'))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(user, pwd)
                s.sendmail(user, [user], msg.as_string())
                print("✅ 错误通知邮件已发送！")
        except Exception as e:
            print(f"❌ 邮件发送失败: {e}")
    send_error_mail(report_html, overall_win_rate)
    exit(1)

try:
    backup_name = f"scan_backup_{get_bj_time().strftime('%Y%m%d')}.py"
    with open(backup_name, "w", encoding="utf-8") as f:
        f.write(current_scan_code)
    print(f"✅ 旧版本已备份至 {backup_name}")
    with open("scan.py", "w", encoding="utf-8") as f:
        f.write(f"# 自动进化版本 | 时间: {get_bj_time().strftime('%Y-%m-%d %H:%M')} | 触发胜率: {overall_win_rate}%\n\n")
        f.write(new_code)
    print("✅ scan.py 已自动更新！下次运行时会自动检测内容变化并重新标记版本起始日期。")
except Exception as e:
    print(f"❌ 文件写入失败: {e}")
    exit(1)

def send_evolve_mail(report_html, win_rate, backup_name):
    user = os.environ.get("EMAIL_ACCOUNT")
    pwd = os.environ.get("EMAIL_PASSWORD")
    if not user or not pwd: return

    notice = f"""
    <div style="background:#fff3e0; border-left:6px solid #ff9800; padding:20px; margin:20px 0; border-radius:8px;">
        <h3 style="color:#e65100; margin-top:0;">已自动覆盖 scan.py</h3>
        <p>旧版本已备份为 <b>{backup_name}</b>，可在 GitHub 仓库找到。</p>
        <p>如果发现新版本有问题，把备份文件内容复制回 scan.py 即可回滚。</p>
        <p>注意：下次 scan.py 运行时会自动检测到内容变化，重新记录版本起始日期，本次改动之前的数据将不再计入未来的胜率评估。</p>
    </div>
    """
    style = "<style>body{font-family:sans-serif;background:#f4f6f9;color:#333;padding:20px;line-height:1.6}.container{max-width:900px;margin:0 auto;background:#fff;padding:30px;border-radius:10px;}</style>"
    full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'>{style}</head><body><div class='container'><h1 style='color:#d32f2f;text-align:center;'>A股 scan.py 已自动进化（宏观风控驱动版）</h1><p style='text-align:center;color:#666;'>本版本真实胜率（仅已到期归档样本） <b style='color:#d32f2f;'>{win_rate}%</b>，系统已自动优化</p>{notice}<hr><h2>本次改进报告</h2>{report_html}</div></body></html>"

    msg = MIMEMultipart()
    msg['From'] = user
    msg['To'] = user
    msg['Subject'] = f"【A股自动进化完成】scan.py 宏观大宗风控已更新 ({get_bj_time().strftime('%Y-%m-%d')})"
    msg.attach(MIMEText(full_html, 'html'))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pwd)
            s.sendmail(user, [user], msg.as_string())
            print("✅ 进化通知邮件已发送至本人！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")

send_evolve_mail(report_html, overall_win_rate, backup_name)
