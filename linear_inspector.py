"""
Linear 周巡查 bot v2 —— 面向迁移后的运营模型(9 项目单 owner + milestone 驱动 + 每周 update 制度 + Triage 收口)。
产出一条"管理者数字体检",四段:① 项目 update 断更 ② Milestone/北极星 ③ 逾期 issue(Urgent/High) ④ Triage 积压。
拉数走 Linear GraphQL API;发送走 Slack incoming webhook。

环境变量(GitHub Actions secrets / 本地 .env):
    LINEAR_API_KEY      Linear 个人 API key(必填)
    SLACK_WEBHOOK_URL   Slack incoming webhook(指向 #meshy-dataset-fte;DRY_RUN=1 时可空)
    DRY_RUN             "1" = 只打印不发,用于本地/CI 调试

可调参数见下方常量。名字→Slack ID 映射见 MENTIONS(填了才会真正 @到人,否则用纯文本 @名字)。
"""

import json
import os
import sys
from datetime import datetime, timezone, date
from urllib import request, error

# ====== 可调参数 ======
OVERDUE_THRESHOLD_DAYS = {1: 2, 2: 7}          # 只看 Urgent(1)/High(2);Medium 及以下不报,降噪
UPDATE_STALE_DAYS = 7                            # 项目 update 超过几天没发 = 断更
TRIAGE_STALE_DAYS = 3                            # Triage issue 停留超过几天 = 积压
MILESTONE_RISK_LEAD_DAYS = 60                    # 只对 targetDate 在这么多天内的 milestone 判滞后
MILESTONE_RISK_GAP = 0.25                        # 时间进度 - 完成进度 超过这个比例 = 滞后
MAX_ROWS = 20                                    # 每段最多列多少行

TEAM_ID = "edcafc58-b719-4c02-923c-21b70e7e830c"     # Data Set (DTS)
NORTH_STAR_PROJECT_ID = "7364cd38-c394-40be-9c59-2a3422acc6da"  # 数据总览

# 9 个项目:(name, id, lead 显示名)
PROJECTS = [
    ("数据获取", "99aa8409-b7b9-4486-8dee-4b367111131a", "Ivan"),
    ("数据处理", "416021e7-31d6-4eea-b29f-69f7b4c91388", "Zee"),
    ("数据清洗", "24d18d85-3551-42e1-8e58-e8b56af9ac2d", "Yaqi"),
    ("合成数据", "66ce4889-2fbf-46f6-a0da-28c98922a343", "Noah"),
    ("数据标注", "16c8d5b9-f087-40f4-946a-211c775cb0b5", "Sage"),
    ("数据修复与专项", "da1a06ea-5af5-4bac-9e14-a87f3078ffcb", "River"),
    ("数据分析研究", "6b5823db-4a77-43bc-86c9-d11a836587de", "Jianqiao"),
    ("数据基建", "2248f448-0eea-4f86-8baa-5810a45e4339", "Zee"),
    ("数据总览", NORTH_STAR_PROJECT_ID, "Kai"),
]

# 名字 → Slack user id(填了才会真正 @人;没填的用纯文本 @名字,不会通知)
# 用 Slack 里 "复制成员 ID" 获取,格式如 U04UP2HKEUF
MENTIONS = {
    "Kai": "U04UP2HKEUF",
    # "Ivan": "U...", "Yaqi": "U...", "Zee": "U...", "Noah": "U...",
    # "Sage": "U...", "River": "U...", "Jianqiao": "U...",
}

LINEAR_API = "https://api.linear.app/graphql"

# ====== GraphQL ======
PROJECTS_QUERY = """
query($ids:[ID!]){
  projects(filter:{id:{in:$ids}}, first:50){
    nodes{
      id name startDate
      lead{ displayName name }
      projectUpdates(first:5){ nodes{ createdAt } }
      projectMilestones(first:40){ nodes{ name targetDate progress } }
    }
  }
}
"""

ISSUES_QUERY = """
query($after:String,$filter:IssueFilter){
  issues(first:100, after:$after, filter:$filter){
    pageInfo{ hasNextPage endCursor }
    nodes{
      identifier title priority updatedAt createdAt url
      assignee{ displayName name }
      project{ name }
      state{ name type }
    }
  }
}
"""


def gql(key, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = request.Request(LINEAR_API, data=body,
                          headers={"Content-Type": "application/json", "Authorization": key},
                          method="POST")
    with request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "errors" in data:
        raise RuntimeError(f"Linear API error: {data['errors']}")
    return data["data"]


def load_dotenv():
    """本地调试:若存在 .env 则加载(CI 用真环境变量,无 .env,无副作用)。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ====== 工具 ======
def days_since(iso_str, now):
    ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return (now - ts).total_seconds() / 86400.0


def mention(name):
    uid = MENTIONS.get(name)
    return f"<@{uid}>" if uid else f"@{name}"


def norm_progress(p):
    """progress 兼容 0-1 或 0-100 两种口径,统一成 0-1。"""
    if p is None:
        return 0.0
    return p / 100.0 if p > 1 else float(p)


# ====== 各段计算 ======
def check_project_updates(projects_by_id, now):
    """① 项目 update 断更。返回 [(name, lead, stale_days 或 None=从未发)]"""
    out = []
    for name, pid, lead in PROJECTS:
        node = projects_by_id.get(pid)
        real_lead = ((node or {}).get("lead") or {}).get("displayName") or lead
        ups = ((node or {}).get("projectUpdates") or {}).get("nodes") or []
        if not ups:
            out.append((name, real_lead, None))
            continue
        # 取最近一条 update(距今最小天数)
        freshest = min(days_since(u["createdAt"], now) for u in ups)
        if freshest > UPDATE_STALE_DAYS:
            out.append((name, real_lead, freshest))
    # 从未发在前,其余按天数倒序
    out.sort(key=lambda x: (x[2] is not None, -(x[2] or 0)))
    return out


def collect_milestones(projects_by_id, now):
    """② 北极星 + 滞后风险。返回 (northstar_lines, risk_lines)"""
    north = []
    ns = projects_by_id.get(NORTH_STAR_PROJECT_ID)
    if ns:
        for m in (ns.get("projectMilestones") or {}).get("nodes") or []:
            if str(m.get("name", "")).startswith("📈"):
                north.append(f"{m['name']} — 进度 {round(norm_progress(m.get('progress'))*100)}%")

    risk = []
    today = now.date()
    for name, pid, _ in PROJECTS:
        node = projects_by_id.get(pid)
        if not node:
            continue
        start = node.get("startDate")
        for m in (node.get("projectMilestones") or {}).get("nodes") or []:
            td = m.get("targetDate")
            if not td:
                continue
            try:
                t_target = date.fromisoformat(td[:10])
            except ValueError:
                continue
            days_left = (t_target - today).days
            if not (0 <= days_left <= MILESTONE_RISK_LEAD_DAYS):
                continue
            try:
                t_start = date.fromisoformat(start[:10]) if start else None
            except (ValueError, TypeError):
                t_start = None
            if t_start is None or t_start >= t_target:
                # 没有起始日就用 targetDate 前 90 天估算
                span = 90.0
                elapsed = span - days_left
            else:
                span = (t_target - t_start).days or 1
                elapsed = (today - t_start).days
            time_prog = max(0.0, min(1.0, elapsed / span))
            done = norm_progress(m.get("progress"))
            if time_prog - done > MILESTONE_RISK_GAP:
                risk.append(f"⚠️ {name}·{m['name']} ｜ 进度 {round(done*100)}% ｜ 距 {td[:10]} 仅 {days_left} 天,明显滞后")
    return north, risk


def split_issues(issues, now):
    """③ 逾期(started, Urgent/High) + ④ Triage 积压"""
    overdue, triage = [], []
    for it in issues:
        st = (it.get("state") or {}).get("type")
        if st == "triage":
            stale = days_since(it["createdAt"], now)
            no_assignee = not it.get("assignee")
            if stale > TRIAGE_STALE_DAYS or no_assignee:
                triage.append({**it, "_stale": stale, "_noassignee": no_assignee})
        elif st == "started":
            prio = it.get("priority")
            th = OVERDUE_THRESHOLD_DAYS.get(prio)
            if th is None:
                continue
            stale = days_since(it["updatedAt"], now)
            if stale > th:
                overdue.append({**it, "_stale": stale})
    overdue.sort(key=lambda x: (x["priority"], -x["_stale"]))
    triage.sort(key=lambda x: -x["_stale"])
    return overdue, triage


# ====== 消息拼装(Slack mrkdwn) ======
def iso_week():
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


def build_message(stale_updates, north, risk, overdue, triage):
    week = iso_week()
    lines = [f"📋 *数据集 Linear 周巡查 · {week}*", ""]

    # ①
    lines.append("*① 项目 Update(每周制度红线)*")
    if stale_updates:
        lines.append("以下项目 Lead 本周还没发 update,请今天补上:")
        for name, lead, sd in stale_updates[:MAX_ROWS]:
            tail = "*从未发*" if sd is None else f"*{int(sd)} 天未发*"
            lines.append(f"• {name} ｜ {mention(lead)} ｜ {tail}")
    else:
        lines.append("✅ 9 个项目本周 update 均已发")
    lines.append("")

    # ②
    lines.append("*② Milestone 进度 / 风险*")
    if north:
        lines.append("北极星:" + " · ".join(north))
    for r in risk[:MAX_ROWS]:
        lines.append(r)
    lines.append("_当月实际增量请对照 Databricks 看板确认 vs ~60 万/月轨迹_")
    lines.append("")

    # ③
    if overdue:
        lines.append("*③ 逾期未更新(Urgent / High)*")
        cur = None
        for it in overdue[:MAX_ROWS]:
            head = {1: "🔴 Urgent(>2 天)", 2: "🟠 High(>7 天)"}.get(it["priority"])
            if head != cur:
                lines.append(head)
                cur = head
            proj = (it.get("project") or {}).get("name") or "无 project"
            who = mention((it.get("assignee") or {}).get("displayName") or "未分配")
            lines.append(f"• <{it['url']}|{it['identifier']}> {it['title']} ｜ {who} ｜ {proj} ｜ *{int(it['_stale'])} 天未更新*")
        lines.append("")

    # ④
    if triage:
        lines.append("*④ Triage 积压(算法收口)*")
        for it in triage[:MAX_ROWS]:
            extra = " ｜ 未分派" if it["_noassignee"] else ""
            lines.append(f"• <{it['url']}|{it['identifier']}> {it['title']} ｜ 停留 *{int(it['_stale'])} 天*{extra}")
        lines.append("")

    lines.append("_检查范围:Data Set team ｜ update 阈值 7 天 ｜ issue 阈值 Urgent>2 / High>7 天_")

    if not stale_updates and not risk and not overdue and not triage:
        return "📋 *数据集 Linear 周巡查 · %s*\n本周数据侧 Linear 一切在轨 ✅（update 齐、无逾期、无 Triage 积压）" % week
    return "\n".join(lines)


def post_to_slack(webhook, text):
    body = json.dumps({"text": text}).encode("utf-8")
    req = request.Request(webhook, data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=15) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Slack returned {resp.status}")


# ====== Main ======
def main():
    load_dotenv()
    key = os.environ.get("LINEAR_API_KEY")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    dry = os.environ.get("DRY_RUN") == "1"

    if not key:
        print("ERROR: LINEAR_API_KEY 未配置", file=sys.stderr)
        return 2
    if not webhook and not dry:
        print("ERROR: SLACK_WEBHOOK_URL 未配置(DRY_RUN=1 可跳过)", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)

    try:
        print("[1/3] 拉项目(update + milestone)…")
        ids = [p[1] for p in PROJECTS]
        pnodes = gql(key, PROJECTS_QUERY, {"ids": ids})["projects"]["nodes"]
        projects_by_id = {n["id"]: n for n in pnodes}

        print("[2/3] 拉 issue(started + triage)…")
        issues, cursor = [], None
        filt = {"team": {"id": {"eq": TEAM_ID}}, "state": {"type": {"in": ["started", "triage"]}}}
        while True:
            page = gql(key, ISSUES_QUERY, {"after": cursor, "filter": filt})["issues"]
            issues += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        print(f"      项目 {len(projects_by_id)} 个 / issue {len(issues)} 条")

        stale_updates = check_project_updates(projects_by_id, now)
        north, risk = collect_milestones(projects_by_id, now)
        overdue, triage = split_issues(issues, now)
    except Exception as e:
        msg = f"⚠️ 周巡查 bot 执行异常: {str(e)[:200]}"
        print(msg, file=sys.stderr)
        if webhook and not dry:
            try:
                post_to_slack(webhook, msg)
            except Exception:
                pass
        return 1

    text = build_message(stale_updates, north, risk, overdue, triage)

    print("[3/3] 输出…")
    print(f"      断更 {len(stale_updates)} / 北极星 {len(north)} / 滞后 {len(risk)} / 逾期 {len(overdue)} / Triage {len(triage)}")
    if dry:
        print("\n--- DRY_RUN,以下是将要发送的内容 ---\n")
        print(text)
    else:
        post_to_slack(webhook, text)
        print("      已发送 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
