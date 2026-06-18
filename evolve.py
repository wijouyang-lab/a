# -*- coding: utf-8 -*-
import pandas as pd
import datetime
import os
import ast
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

print("启动 A股 scan.py 自动进化引擎（事件驱动版）...")

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
def get_bj_time():
    return datetime.datetime.now(BEIJING_TZ)

review_log = "review_history.csv"
if not os.path.exists(review_log):
    print("⚠️ 复盘账本不存在，跳过进化。需要先积累复盘数据。")
    exit(0)

try:
    df = pd.read_csv(review_log, on_bad_lines='skip')
    df['Review_Date'] = pd.to_datetime(df['Review_Date'])
    cutoff = get_bj_time() - datetime.timedelta(days=30)
    recent = df[df['Review_Date'] >= cutoff.replace(tzinfo=None)].copy()
    if len(recent) < 10:
        print(f"⚠️ 近30天复盘样本只有 {len(recent)} 条，不足10条，跳过进化。")
        exit(0)
except Exception as e:
    print(f"⚠️ 复盘账本读取失败: {e}")
    exit(1)

# 兼容历史标签名（Core_Double_Dragon/Sub_Pioneer）和新版统一标签（Core_Dragon）
ALL_CORE_TAGS = ['Core_Double_Dragon', 'Sub_Pioneer', 'Core_Dragon']

overall_win_rate = 0
if 'PnL_Pct' in recent.columns:
    recent['PnL_Pct'] = pd.to_numeric(recent['PnL_Pct'], errors='coerce')
    valid = recent[
        recent['PnL_Pct'].notna() &
        recent['Tag'].isin(ALL_CORE_TAGS)
    ].copy()
    if len(valid) > 0:
        overall_win_rate = round((valid['PnL_Pct'] > 0).sum() / len(valid) * 100, 1)

stats = {}
for tag in ALL_CORE_TAGS:
    group = recent[recent['Tag'] == tag].copy()
    group['PnL_Pct'] = pd.to_numeric(group['PnL_Pct'], errors='coerce')
    valid_group = group.dropna(subset=['PnL_Pct'])
    if len(valid_group) > 0:
        win = (valid_group['PnL_Pct'] > 0).sum()
        stats[tag] = {
            "总数": len(valid_group),
            "胜率": round(win / len(valid_group) * 100, 1),
            "平均盈亏": round(valid_group['PnL_Pct'].mean(), 2),
            "平均持仓天数": round(pd.to_numeric(valid_group['Days_Held'], errors='coerce').mean(), 1)
        }

# 按行业统计
industry_stats = {}
if 'Name' in recent.columns:
    core_valid = recent[
        recent['PnL_Pct'].notna() &
        recent['Tag'].isin(ALL_CORE_TAGS)
    ].copy()
    if 'Industry' in recent.columns:
        for industry, grp in core_valid.groupby('Industry'):
            if len(grp) >= 2:
                win_rate = round((grp['PnL_Pct'] > 0).sum() / len(grp) * 100, 1)
                avg_pnl = round(grp['PnL_Pct'].mean(), 2)
                industry_stats[industry] = {"胜率": win_rate, "平均盈亏": avg_pnl, "样本数": len(grp)}

# 按推荐评分分桶统计（验证评分是否真的有预测力）
score_stats = {}
if 'Score' in recent.columns:
    score_valid = recent[
        recent['PnL_Pct'].notna() &
        recent['Tag'].isin(ALL_CORE_TAGS) &
        recent['Score'].notna()
    ].copy()
    score_valid['Score'] = pd.to_numeric(score_valid['Score'], errors='coerce')
    score_valid = score_valid.dropna(subset=['Score'])
    if len(score_valid) >= 5:
        def score_bucket(s):
            if s >= 80:
                return "80-100分(高信心)"
            elif s >= 60:
                return "60-79分(中信心)"
            else:
                return "60分以下(低信心)"
        score_valid['Bucket'] = score_valid['Score'].apply(score_bucket)
        for bucket, grp in score_valid.groupby('Bucket'):
            if len(grp) >= 2:
                win_rate = round((grp['PnL_Pct'] > 0).sum() / len(grp) * 100, 1)
                avg_pnl = round(grp['PnL_Pct'].mean(), 2)
                score_stats[bucket] = {"胜率": win_rate, "平均盈亏": avg_pnl, "样本数": len(grp)}

print(f"📊 近30天真实胜率: {overall_win_rate}% | 各标签: {stats}")
if industry_stats:
    print(f"📊 行业胜率分布: {industry_stats}")
if score_stats:
    print(f"📊 评分区间胜率分布: {score_stats}")

EVOLVE_THRESHOLD = 60
if overall_win_rate >= EVOLVE_THRESHOLD:
    print(f"✅ 胜率 {overall_win_rate}% 达标，本周无需进化。")
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

loss_samples = recent[
    recent['PnL_Pct'].notna() &
    recent['Tag'].isin(ALL_CORE_TAGS) &
    (recent['PnL_Pct'] < 0)
].copy()

win_samples = recent[
    recent['PnL_Pct'].notna() &
    recent['Tag'].isin(ALL_CORE_TAGS) &
    (recent['PnL_Pct'] > 0)
].copy()

score_section = f"\n【推荐评分区间胜率分布（用于判断评分体系是否有真实预测力）】：\n{score_stats}\n" if score_stats else "\n【推荐评分区间胜率分布】：暂无足够样本（评分系统刚上线或样本不足5条）\n"

prompt = f"""
你是一个A股事件驱动型量化策略优化专家。当前系统是【消息+逻辑推演驱动版】，核心逻辑是：
事件识别 → 产业链推演 → 匹配资金活跃标的 → 技术面仅作风控兜底 → 对Top1-5给出1-100推荐评分

【近30天真实持仓表现】：
整体胜率：{overall_win_rate}%（目标>60%）
各标签细分：{stats}
行业胜率分布：{industry_stats}
{score_section}

【亏损样本（最近{len(loss_samples.tail(10))}条）】：
{loss_samples.tail(10).to_string()}

【盈利样本（最近{len(win_samples.tail(10))}条）】：
{win_samples.tail(10).to_string()}

【当前 scan.py 代码】：
{current_scan_code}

【分析任务】：
这是一个事件驱动系统，不是纯技术系统。请从以下几个维度分析胜率低的原因并提出改进：

1. 事件逻辑质量问题：
   - AI 是否在没有强事件时仍强行选股（凑数推荐）？
   - 事件到产业链的推演是否过于间接（三手四手受益）？
   - 是否存在"消息已经打完了"但仍然推荐的情况（涨跌幅已很大）？

2. 资金验证问题：
   - Top 300 中是否包含了太多与消息无关的纯技术票？
   - 是否应该提高涨跌幅过滤（例如当日已涨超8%的票消息已被充分消化）？

3. 止损设置问题：
   - 默认5%止损是否太紧或太松？
   - 不同行业/标签的止损应该差异化吗？

4. 评分体系校准问题（重点）：
   - 如果"60分以下"区间的胜率反而高于"80-100分"区间，说明评分体系存在严重偏差，评分标准需要重新校准
   - 评分应该更看重哪个维度（事件直接性/新闻共振/资金验证/技术健康度）？

5. 新闻来源质量：
   - 当前新闻源是否能覆盖A股最重要的政策消息？
   - 是否需要补充更多A股专项消息源？

可以调整的内容：
- prompt 中的事件逻辑推演指令（让 AI 更严格地要求直接受益逻辑）
- prompt 中的评分标准描述（如果发现评分与实际表现脱节，应调整评分权重的描述方式）
- 涨跌幅过滤门槛（当日涨跌幅区间限制）
- 止损比例（DEFAULT_STOP_LOSS_PCT）
- 持仓周期建议
- 新闻抓取来源和数量
- Top N 送给 AI 的标的数量

严格禁止：
- 不得改变代码整体结构、入库逻辑、邮件发送逻辑
- 不得修改模型名称（必须保持 claude-opus-4-8）
- 不得把系统改回技术指标驱动逻辑（RSI/MACD 阈值过滤）
- 不得删除"免死金牌"机制（无历史数据的票赋予占位符而不是删除）
- 不得删除1-100推荐评分机制，且评分提取格式必须严格保持为"评分:[XX]/100"，必须与正则表达式 r'评分\\s*[:：]\\s*\\[?(\\d{{1,3}})\\s*/\\s*100' 保持完全兼容，不得改成其他格式（如"XX分"、"评分XX"等变体），否则后续无法正确提取入库
- 不得把【核心精选】Top 1-5 的详细分析数量改少（必须维持5只都展开详细分析，除非当天逻辑确实不足5只）

【严格按以下格式输出，不要加任何其他内容】：

===REPORT_START===
<div style="background:#e8f5e9; border-left:6px solid #388e3c; padding:20px; border-radius:8px; margin-bottom:20px;">
<h3 style="color:#1b5e20; margin-top:0;">🔬 胜率诊断（事件驱动视角）</h3>
<p>(分析是事件逻辑质量问题、资金匹配问题、止损设置问题还是评分体系校准问题，结合亏损样本和评分区间分布说明)</p>
</div>
<div style="background:#e3f2fd; border-left:6px solid #1976d2; padding:20px; border-radius:8px;">
<h3 style="color:#0d47a1; margin-top:0;">🔧 本次改进内容</h3>
<ul>
<li>(改动1：具体说明改了什么，为什么这样改能提升胜率)</li>
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

# ==========================================
# 语法检查：生成的代码必须通过才能覆盖
# ==========================================
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
        if not user or not pwd:
            return
        style = "<style>body{font-family:sans-serif;background:#f4f6f9;color:#333;padding:20px;line-height:1.6}.container{max-width:900px;margin:0 auto;background:#fff;padding:30px;border-radius:10px;}</style>"
        full_html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>{style}</head>
        <body><div class='container'>
        <h1 style='color:#c62828;text-align:center;'>⚠️ A股进化失败 - 语法错误</h1>
        <p style='text-align:center;color:#666;'>触发胜率 <b style='color:#d32f2f;'>{win_rate}%</b>，但生成代码有语法错误，scan.py 未被修改</p>
        {report_html}
        </div></body></html>"""
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
    print("✅ scan.py 已自动更新！")
except Exception as e:
    print(f"❌ 文件写入失败: {e}")
    exit(1)


def send_evolve_mail(report_html, win_rate, backup_name):
    user = os.environ.get("EMAIL_ACCOUNT")
    pwd = os.environ.get("EMAIL_PASSWORD")
    if not user or not pwd:
        return

    notice = f"""
    <div style="background:#fff3e0; border-left:6px solid #ff9800; padding:20px; margin:20px 0; border-radius:8px;">
        <h3 style="color:#e65100; margin-top:0;">已自动覆盖 scan.py</h3>
        <p>旧版本已备份为 <b>{backup_name}</b>，可在 GitHub 仓库找到。</p>
        <p>如果发现新版本有问题，把备份文件内容复制回 scan.py 即可回滚。</p>
    </div>
    """
    style = "<style>body{font-family:sans-serif;background:#f4f6f9;color:#333;padding:20px;line-height:1.6}.container{max-width:900px;margin:0 auto;background:#fff;padding:30px;border-radius:10px;}</style>"
    full_html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>{style}</head>
    <body><div class='container'>
    <h1 style='color:#d32f2f;text-align:center;'>A股 scan.py 已自动进化（事件驱动版）</h1>
    <p style='text-align:center;color:#666;'>近30天真实胜率 <b style='color:#d32f2f;'>{win_rate}%</b>，系统已自动优化</p>
    {notice}
    <hr>
    <h2>本次改进报告</h2>
    {report_html}
    </div></body></html>"""

    msg = MIMEMultipart()
    msg['From'] = user
    msg['To'] = user
    msg['Subject'] = f"【A股自动进化完成】scan.py 已更新 ({get_bj_time().strftime('%Y-%m-%d')})"
    msg.attach(MIMEText(full_html, 'html'))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pwd)
            s.sendmail(user, [user], msg.as_string())
            print("✅ 进化通知邮件已发送至本人！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")


send_evolve_mail(report_html, overall_win_rate, backup_name)
