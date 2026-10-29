#!/usr/bin/env python3
"""输出稳定的待办摘要；摘要变化时唤醒分派任务。"""
import json
import re
import os
import sys
from pathlib import Path
# 同时支持源码目录和安装后的 scripts/kit 目录。
for _parent in Path(__file__).resolve().parents:
    if (_parent / "kit").is_dir():
        sys.path.insert(0, str(_parent))
        break
from kit.config import load_config
from kit.forge import make_forge



class _DiscoverPaused(Exception):
    """打开的 issue 达到上限，本轮不做自主审视。"""

def configuration():
    location = Path(__file__).resolve().parent / "project.toml"
    import os
    return load_config(os.environ.get("AGENT_REVIEW_CONFIG") or (str(location) if location.exists() else None))

CFG = None
FORGE = None
HERMES_LABEL = REVIEW_BOT = HERMES_USER = LEGACY_USER = LEGACY_CUTOVER = None


def configure(config):
    global CFG, FORGE, HERMES_LABEL, REVIEW_BOT, HERMES_USER, LEGACY_USER, LEGACY_CUTOVER
    CFG = config
    FORGE = make_forge(config, role="fixer")
    HERMES_LABEL = config["forge"]["source_label"]
    REVIEW_BOT = config["accounts"]["reviewer"]
    HERMES_USER = config["accounts"]["fixer"]
    LEGACY_USER = config["accounts"].get("legacy_fixer_username", config["accounts"]["user"])
    LEGACY_CUTOVER = config["accounts"]["user_note_since"]


def is_own(note):
    author = note.get("author", {}).get("username")
    if author == HERMES_USER:
        return True
    if author == LEGACY_USER and note.get("created_at", "") < LEGACY_CUTOVER:
        return True
    return False


def main():
    configure(configuration())
    lines = []

    # 自己已打开且需要修改的 MR。
    my_mrs = FORGE.list_items("mr", "opened", [HERMES_LABEL], limit=50)
    need_fix_mrs = sorted(m["iid"] for m in my_mrs if "review::需修改" in m.get("labels", []) and "等待前置" not in m.get("labels", []))
    lines.append("mr_need_fix: " + (",".join(f"!{n}" for n in need_fix_mrs) if need_fix_mrs else "none"))

    # 执行方来源的待修改 issue。
    issues = FORGE.list_items("issue", "opened", [HERMES_LABEL], limit=100)
    need_fix_issues = sorted(i["iid"] for i in issues if "review::需修改" in i.get("labels", []) and "等待前置" not in i.get("labels", []))
    waiting = sorted([f"#{i['iid']}" for i in issues if "等待前置" in i.get("labels", [])] + [f"!{m['iid']}" for m in my_mrs if "等待前置" in m.get("labels", [])])
    lines.append("waiting: " + (",".join(waiting) if waiting else "none"))
    lines.append("issue_need_fix: " + (",".join(f"!{n}" for n in need_fix_issues) if need_fix_issues else "none"))

    # 最后一次己方评论之后的评审回复。
    reply_items = []
    for kind, iid in [("i", i["iid"]) for i in issues] + [("m", m["iid"]) for m in my_mrs]:
        try:
            notes = FORGE.list_notes("issue" if kind == "i" else "mr", iid, sort="desc", limit=100)
        except Exception:
            continue
        last_own = max([x["id"] for x in notes if is_own(x)], default=0)
        # 2026-10-28：只算需要 hermes 执行的评审评论（含"hermes 需修改正文"/"hermes 需执行"一节，
        # 或对修复方案的意见）；"需修改"结论由 mr_need_fix / issue_need_fix 覆盖，其余评论无需处理。
        from kit.conventions import ACTION_MARKERS
        markers = ACTION_MARKERS
        pending = [x["id"] for x in notes
                   if x.get("author", {}).get("username") == REVIEW_BOT and x["id"] > last_own
                   and any(m in (x.get("body") or "") for m in markers)]
        if pending:
            reply_items.append(f"{kind.upper()}{iid}:{len(pending)}")
    reply_items.sort(key=lambda s: (s[0], int(s[1:].split(":")[0])))
    lines.append("review_reply: " + (",".join(reply_items) if reply_items else "none"))

    # 开发条件同时要求确认问题及确认修复范围；按确认范围时间排序。
    merged_desc = ""
    try:
        merged = FORGE.list_items("mr", "merged", [], limit=100)
        merged_desc = "\n".join((m.get("description") or "") + (m.get("title") or "") for m in merged)
    except Exception:
        pass

    candidates = []
    for i in issues:
        labels = i.get("labels", [])
        if "review::已确认" in labels and "scope::确认修复" in labels:
            iid = i["iid"]
            try:
                notes = FORGE.list_notes("issue", iid, sort="desc", limit=100)
            except Exception:
                notes = []
            scope_notes = [x.get("created_at", "") for x in notes
                           if x.get("author", {}).get("username") == REVIEW_BOT
                           and "确认修复" in (x.get("body") or "")]
            candidates.append((scope_notes[-1] if scope_notes else i.get("created_at", ""), iid))
    candidates.sort(key=lambda t: (t[0], t[1]))

    open_hermes_mr = "none" if not my_mrs else ",".join(f"!{m['iid']}" for m in sorted(my_mrs, key=lambda m: m["iid"]))
    # 2026-10-28 起不再串行：已确认修复且还没有 open MR 的 issue 都可开发，按确认时间先后取第一个。
    # 文件冲突由开发前检查 open_mr_files 避免（同一文件已有 open MR 在改时跳到下一个）。
    covered = set()
    mr_files = []
    for m in sorted(my_mrs, key=lambda m: m["iid"]):
        desc = (m.get("description") or "") + (m.get("title") or "")
        covered.update(int(x) for x in re.findall(r"Closes #(\d+)", desc))
        try:
            ch = FORGE.changes(m["iid"])
            mr_files.append(f"!{m['iid']}:" + ",".join(c["new_path"] for c in ch.get("changes", [])))
        except Exception:
            mr_files.append(f"!{m['iid']}:?")
    slot, slot_note = "none", "无可开发项"
    waiting = []
    for _, iid in candidates:
        st = next((i for i in issues if i["iid"] == iid), None)
        if not st or st.get("state") == "closed" or iid in covered:
            continue
        waiting.append(f"#{iid}")
    if waiting:
        slot, slot_note = waiting[0], "可开发（待开发：" + ",".join(waiting) + "）"
    # 远端有自己的开发分支但还没有 MR（任何状态）时列为 wip；输出含分钟级 tick，保证每次唤醒。
    repo = os.path.expanduser(CFG["fixer"]["repo"])
    try:
        import subprocess, time
        out = subprocess.run(["git", "-C", repo, "ls-remote", "--heads", "origin"],
                             capture_output=True, text=True, timeout=30).stdout
        remote = [l.split("refs/heads/", 1)[1] for l in out.splitlines() if "refs/heads/" in l]
        all_mrs = FORGE.list_items("mr", "all", [], limit=100)
        with_mr = {m.get("source_branch") for m in all_mrs}
        wip = [b for b in remote if any(b.startswith(prefix) for prefix in CFG["branches"]["work_prefixes"]) and b not in with_mr]
        if wip:
            lines.append("wip: " + ",".join(sorted(wip)) + " | tick=" + time.strftime("%Y-%m-%dT%H:%M"))
    except Exception as e:
        lines.append(f"wip: 检查失败 {e}")
    lines.append("open_mr_files: " + (" ; ".join(mr_files) if mr_files else "none"))
    # 自主审视：findings.md 中尚未处理的发现条数，以及下一个未审视的源码文件。
    # 执行方打开的 issue 达到 fixer.max_open_issues 时暂停，先处理现有 issue。
    max_open = int(CFG["fixer"].get("max_open_issues", 30))
    try:
        if max_open > 0 and len(issues) >= max_open:
            raise _DiscoverPaused(f"discover: 暂停 | 打开的 issue {len(issues)}/{max_open}，先处理现有 issue")
        review_dir = os.path.expanduser(CFG["fixer"]["review_dir"])
        heads = [l for l in Path(review_dir, "findings.md").read_text(encoding="utf-8").splitlines() if l.startswith("### F-")]
        unfiled = [l.split()[1] for l in heads if "已提 #" not in l and "不提" not in l]
        done = {l.split("|")[0].strip() for l in Path(review_dir, "reviewed-files.txt").read_text(encoding="utf-8").splitlines()
                if any(l.startswith(d.rstrip("/") + "/") for d in CFG["project"]["source_dirs"])}
        busy = set()
        for m in FORGE.list_items("mr", "opened", [], limit=100):
            if HERMES_LABEL in m.get("labels", []):
                continue
            try:
                ch = FORGE.changes(m["iid"])
                busy.update(c["new_path"] for c in ch.get("changes", []))
            except Exception:
                pass
        src = sorted(f"{directory}/{name}" for directory in CFG["project"]["source_dirs"]
                     for name in os.listdir(os.path.join(repo, directory))
                     if any(name.endswith(ext) for ext in CFG["project"]["source_extensions"]) and name not in CFG["project"]["excluded_source_files"])
        todo = [f for f in src if f not in done and f not in busy]
        pending_review = sum(1 for i in issues if "review::待评审" in i.get("labels", []))
        lines.append(f"discover: 未处理发现 {len(unfiled)} 条（{','.join(unfiled[:6])}）| 下一个文件 "
                     f"{todo[0] if todo else 'none'} | 已审视 {len(done)}/{len(src)}，他人 MR 占用 {len([f for f in src if f in busy and f not in done])} | "
                     f"待评审 issue {pending_review} | 打开的 issue {len(issues)}/{max_open}")
    except _DiscoverPaused as paused:
        lines.append(str(paused))
    except Exception as e:
        lines.append(f"discover: 检查失败 {e}")
    lines.append(f"dev_slot: {slot} | {slot_note} | open_hermes_mr: {open_hermes_mr}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
