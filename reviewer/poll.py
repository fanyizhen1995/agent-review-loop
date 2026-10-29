#!/usr/bin/env python3
"""hermes issue/MR 评审轮询。由 cron 定时运行，本身不调用模型。

有 执行方来源标签 + review::待评审 的条目且自上次评审后有变化时，准备 worktree，
启动一次无头 claude 评审一条。用法：
  poll.py                 正常轮询
  poll.py --dry-run       评审但不往 forge 写（评论只写到运行目录）
  poll.py --item issue:31 指定条目（可与 --dry-run 合用，忽略标签过滤）
"""
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kit.config import load_config
from kit.conventions import TEXT_ONLY_MARK
import forge_cli as gl

CONFIG = load_config(os.environ.get("AGENT_REVIEW_CONFIG") or Path(__file__).with_name("project.toml"))
R = CONFIG["reviewer"]
REPO = R["repo"]
HERE = R["install_dir"]
RUNS = R["runs_dir"]
STATE = f"{RUNS}/state.json"
EVENTS = f"{RUNS}/events.md"
RUN_ROOT = R["run_root"]
REMOTE_REPO = R["git"]["fetch_url"]
BASE_BRANCH = R["git"]["base_branch"]
BASE_REF = R["git"]["base_ref"]
SOURCE_REF_PREFIX = R["git"]["source_ref_prefix"]
LIMITS = R["limits"]
MAX_RUNS_PER_HOUR = LIMITS["max_runs_per_hour"]
SLOTS = LIMITS["slots"]
MAX_ATTEMPTS = LIMITS["max_attempts"]
CLAUDE_TIMEOUT = LIMITS["claude_timeout"]
CODEX_TIMEOUT = LIMITS["codex_timeout"]
CODEX_EFFORT = LIMITS["codex_effort"]
VERIFY_BUDGET_USD = str(LIMITS["verify_budget_usd"])
LIGHT_BUDGET_USD = str(LIMITS["light_budget_usd"])
QUIET_MINUTES = LIMITS["quiet_minutes"]
MR_QUIET_MINUTES = LIMITS["mr_quiet_minutes"]
USER_QUIET_MINUTES = LIMITS["user_quiet_minutes"]


def fetch_ref(item):
    """GitHub 从 PR 引用拉取，支持来自 fork 的分支。"""
    if CONFIG["forge"]["kind"] == "github":
        return f"refs/pull/{item['iid']}/head"
    return SOURCE_REF_PREFIX + item["source_branch"]


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def event(msg):
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(f"- {now()} {msg}\n")


def sh(*cmd, cwd=REPO, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {p.stderr.strip()[:500]}")
    return p.stdout.strip()


def load_state():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {"items": {}, "runs": [], "merged_seen": []}


def save_state(st):
    tmp = f"{STATE}.{os.getpid()}.tmp"
    json.dump(st, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


class locked:
    """文件锁上下文：blocking=False 时拿不到返回 None。"""
    def __init__(self, path, blocking=True):
        self.path, self.blocking, self.f = path, blocking, None

    def __enter__(self):
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
            return self.f
        except BlockingIOError:
            self.f.close()
            self.f = None
            return None

    def __exit__(self, *a):
        if self.f:
            self.f.close()


def update_state(fn):
    """并行评审共用 state.json：读-改-写放在同一把锁内。"""
    with locked(f"{RUNS}/state.lock"):
        st = load_state()
        r = fn(st)
        save_state(st)
        return r


def prepare(item, run_dir):
    """在本次运行目录下建独立的 BASE/HEAD worktree；拉取与建 worktree 串行执行（git 引用锁）。"""
    rid = os.path.basename(run_dir)
    base, head = f"{run_dir}/base", None
    with locked(f"{RUNS}/prep.lock"):
        if R["git"].get("pre_fetch_cmd"):
            sh(*R["git"]["pre_fetch_cmd"])
        sh("git", "fetch", "-q", "--no-write-fetch-head", REMOTE_REPO,
           f"+{BASE_REF}:refs/hermes-review/{rid}/main")
        sh("git", "worktree", "add", "-q", "--detach", base, f"refs/hermes-review/{rid}/main")
        if item["kind"] == "mr":
            br, sha = item["source_branch"], item["sha"]
            sh("git", "fetch", "-q", "--no-write-fetch-head", REMOTE_REPO,
               f"+{fetch_ref(item)}:refs/hermes-review/{rid}/head")
            got = sh("git", "rev-parse", f"refs/hermes-review/{rid}/head")
            if got != sha:
                raise RuntimeError(f"MR !{item['iid']} 分支 {br} 取到 {got[:8]}，forge 为 {sha[:8]}")
            head = f"{run_dir}/head"
            sh("git", "worktree", "add", "-q", "--detach", head, f"refs/hermes-review/{rid}/head")
    return base, head


def cleanup(run_dir):
    rid = os.path.basename(run_dir)
    with locked(f"{RUNS}/prep.lock"):
        for wt in ("base", "head"):
            if os.path.isdir(f"{run_dir}/{wt}"):
                sh("git", "worktree", "remove", "--force", f"{run_dir}/{wt}", check=False)
        for ref in ("main", "head"):
            sh("git", "update-ref", "-d", f"refs/hermes-review/{rid}/{ref}", check=False)


def is_light(item):
    """回复、只改写法的 issue、只改说明没有新提交的 MR：只用 Claude 做轻量检查。"""
    if item.get("mode") == "reply":
        return "回复评论"
    if item.get("mode") == "triage":
        return None
    prev = (item.get("prev_key") or "").split("|")[0]
    if item["kind"] == "mr" and prev and prev == item.get("sha"):
        return "MR 无新提交"
    if item["kind"] == "issue":
        notes = gl.forge().list_notes("issue", item["iid"], sort="desc", limit=30)
        last = next((n for n in notes if not n["system"] and gl.username(n["author"]) == CONFIG["accounts"]["reviewer"]), None)
        if last and TEXT_ONLY_MARK in last["body"] and "评审结论：需修改" in last["body"]:
            return "上次只要求文字修改"
    return None


def review(item, dry_run):
    """代码和事实核对走两段（Codex 起草、Claude 复核）；回复与文字修改只用 Claude 轻量检查。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    run_dir = f"{RUN_ROOT}/{item['kind']}-{item['iid']}-{ts}"
    os.makedirs(run_dir)
    try:
        light = is_light(item)
        if light:
            return _light(item, dry_run, run_dir, light)
        return _review(item, dry_run, run_dir)
    finally:
        cleanup(run_dir)


def run_claude(prompt_file, target, run_dir, env, budget, dirs):
    prompt = open(f"{HERE}/{prompt_file}", encoding="utf-8").read() + target
    open(f"{run_dir}/{prompt_file}", "w", encoding="utf-8").write(prompt)
    with open(f"{run_dir}/claude.json", "w") as out, open(f"{run_dir}/claude.err", "w") as err:
        p = subprocess.run(
            ["claude", "-p", "--settings", f"{HERE}/settings.json", "--setting-sources", "user",
             "--add-dir", *dirs, run_dir, "--output-format", "json",
             "--no-session-persistence", "--max-budget-usd", budget],
            input=prompt, cwd=REPO, env=env, stdout=out, stderr=err, text=True,
            timeout=CLAUDE_TIMEOUT)
    result = ""
    try:
        result = json.load(open(f"{run_dir}/claude.json")).get("result", "")
    except Exception:
        pass
    verdict = None
    for line in reversed(result.strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{") and '"verdict"' in line:
            try:
                verdict = json.loads(line)
            except Exception:
                pass
            break
    return p.returncode, verdict


def _light(item, dry_run, run_dir, why):
    lines = [f"- 类型：{item['kind']}", f"- 编号：{item['iid']}", f"- 标题：{item['title']}",
             f"- 轻量检查原因：{why}", f"- RUN_DIR：{run_dir}", f"- DRY_RUN={'1' if dry_run else '0'}"]
    if item.get("mode") == "reply":
        lines.insert(3, f"- 模式：回复。处理 note {item['note_id']} 及其之后的新评论（最新评论作者：{item.get('author', '未知')}）")
    target = "\n## 本次评审对象\n\n" + "\n".join(lines) + "\n"
    env = dict(os.environ, RUN_DIR=run_dir, DRY_RUN="1" if dry_run else "0",
               AGENT_REVIEW_HEADLESS="1", AGENT_REVIEW_READONLY="1" if dry_run else "0", AGENT_REVIEW_CONFIG=str(Path(os.environ.get("AGENT_REVIEW_CONFIG") or Path(__file__).with_name("project.toml")).resolve()))
    rc, verdict = run_claude("light-prompt.md", target, run_dir, env, LIGHT_BUDGET_USD, [REPO])
    return rc, verdict, run_dir, None


def _review(item, dry_run, run_dir):
    base, head = prepare(item, run_dir)
    lines = [f"- 类型：{item['kind']}", f"- 编号：{item['iid']}", f"- 标题：{item['title']}",
             f"- BASE：{base}（{BASE_BRANCH} {sh('git', 'rev-parse', '--short', f'refs/hermes-review/{os.path.basename(run_dir)}/main')}）",
             f"- RUN_DIR：{run_dir}", f"- DRY_RUN={'1' if dry_run else '0'}"]
    if item.get("mode") == "triage":
        lines.insert(3, "- 模式：触发评估。按 standards.md「触发评估模式」执行，只做评审标准第 6 项")
    if item.get("mode") == "reply":
        lines.insert(3, f"- 模式：回复。处理 note {item['note_id']} 及其之后的新评论（最新评论作者：{item.get('author', '未知')}），按 standards.md 的「回复模式」执行")
    if item["kind"] == "mr":
        lines.insert(3, f"- HEAD：{head}（{item['source_branch']} {item['sha'][:8]}）")
    target = "\n## 本次评审对象\n\n" + "\n".join(lines) + "\n"
    env = dict(os.environ, RUN_DIR=run_dir, DRY_RUN="1" if dry_run else "0",
               AGENT_REVIEW_HEADLESS="1", AGENT_REVIEW_READONLY="1" if dry_run else "0", AGENT_REVIEW_CONFIG=str(Path(os.environ.get("AGENT_REVIEW_CONFIG") or Path(__file__).with_name("project.toml")).resolve()))

    # 工作流筛选信息传入两阶段模型进程，复核脚本无需命令前缀。
    env['ARL_BRANCH'] = item.get('source_branch') or BASE_BRANCH
    env['ARL_SHA'] = item.get('sha') or ''
    issue_types = CONFIG.get('workflow', {}).get('issue_types', ['bug', 'perf', 'robust', 'refactor'])
    selected_type = next((kind for kind in issue_types if kind in item.get('labels', []) or item.get('title', '').startswith('[' + kind + ']')), '')
    if selected_type:
        env['ARL_ISSUE_TYPE'] = selected_type
    if head:
        env['ARL_CHANGED_PATHS'] = sh('git', '-C', head, 'diff', '--name-only', sh('git', '-C', base, 'rev-parse', 'HEAD'), 'HEAD')

    # 第一段：Codex 起草（forge 只读）
    prompt = open(f"{HERE}/codex-prompt.md", encoding="utf-8").read() + target
    open(f"{run_dir}/codex-prompt.md", "w", encoding="utf-8").write(prompt)
    with open(f"{run_dir}/codex-events.jsonl", "w") as out, open(f"{run_dir}/codex.err", "w") as err:
        p = subprocess.run(
            ["codex", "exec", "-C", base, "-c", f"model_reasoning_effort={CODEX_EFFORT}",
             "--dangerously-bypass-approvals-and-sandbox", "--json",
             "-o", f"{run_dir}/codex-last-message.md", "-"],
            input=prompt, env=dict(env, GL_READONLY="1", AGENT_REVIEW_READONLY="1"), stdout=out, stderr=err, text=True,
            timeout=CODEX_TIMEOUT)
    missing = [f for f in ("comment.md", "evidence.md", "verdict.json")
               if not os.path.exists(f"{run_dir}/{f}")]
    if missing:
        return p.returncode, None, run_dir, f"Codex 未产出 {missing}（退出码 {p.returncode}）"

    # 第二段：Claude 复核并发布
    rc, verdict = run_claude("verify-prompt.md", target, run_dir, env, VERIFY_BUDGET_USD,
                             [base, *([head] if head else [])])
    return rc, verdict, run_dir, None


def notify_merged(f, only=None):
    """合入后的评论失败可在后续轮询补发，不再重复请求合入。"""
    pending = update_state(lambda st: dict(st.get('merge_notifications', {})))
    for key, notice in pending.items():
        if only is not None and key != only:
            continue
        who, sha = notice['who'], notice['sha']
        try:
            f.add_note('mr', notice['iid'], f'@{who} 已按同意自动合入，合入提交 `{sha[:8]}`。')
        except Exception as exc:
            log(f"!{notice['iid']} 合入通知失败，下一轮补发：{exc}")
            continue
        def sent(st):
            st.setdefault('merge_notifications', {}).pop(key, None)
            st.setdefault('merge', {})[key] = {'approval': notice['approval'], 'status': '已合入',
                                               'statuses': ['已合入'], 'at': now()}
        update_state(sent)


def auto_merge():
    """成员同意且评审通过时合入；冲突交执行方合并基准分支后重评。"""
    merge_settings = CONFIG.get('workflow', {}).get('merge', R.get('merge', {}))
    if not merge_settings.get('enabled', True) or gl.readonly():
        return
    f = gl.forge()
    notify_merged(f)
    source = CONFIG.get('labels', {}).get('source', 'source::agent')
    branch = BASE_BRANCH
    conflict_steps = (
        f"**hermes 需执行**\n1. 把 origin/{branch} 合并到本分支并解决冲突（不 rebase、不 force-push），重新运行全量测试和回归测试。\n"
        "2. 在本合并请求下说明冲突涉及的文件和解决方式，标签改回 `review::待评审`。")
    for m in f.list_items('mr', 'opened', [source]):
        iid = m['iid']
        approval = gl.latest_approval(f.list_notes('mr', iid, sort='desc', limit=None), merge_settings=merge_settings)
        if not approval:
            if 'review::可合入' in m['labels'] and f.merge_status(iid) == 'conflict':
                f.add_note('mr', iid, f"该合并请求评审已通过，但与 {branch} 有冲突，暂时无法合入。\n\n" + conflict_steps)
                f.set_labels('mr', iid, remove=['review::可合入'], add=['review::需修改'])
                log(f'!{iid} 评审已通过但有冲突，已交 hermes 解决')
            continue
        who = gl.username(approval['author'])
        key = f'mr:{iid}'
        done = update_state(lambda st: dict(st.setdefault('merge', {}).get(key, {})))

        def say(text, status):
            # 同一条同意、同一种状态只回复一次；保留所有已回复状态。
            statuses = done.get('statuses', [done.get('status')]) if done.get('approval') == approval['id'] else []
            if status in statuses:
                return False
            f.add_note('mr', iid, f'@{who} {text}')
            statuses.append(status)
            def mark(st):
                st.setdefault('merge', {})[key] = {'approval': approval['id'], 'status': status,
                                                  'statuses': statuses, 'at': now()}
            update_state(mark)
            return True

        if 'review::可合入' not in m['labels']:
            say('已收到合入同意。该合并请求评审通过后自动合入（需标签为可合入）。', '等待评审')
            continue
        detail = f.get_item('mr', iid)
        if 'review::可合入' not in detail['labels']:
            continue
        status = f.merge_status(iid)
        if status == 'mergeable':
            with gl.merge_permission():
                sha = f.merge(iid, detail['sha'], R.get('merge', {}).get('remove_source_branch', True))
            def queue_notice(st):
                st.setdefault('merge_notifications', {})[key] = {'iid': iid, 'who': who, 'sha': sha,
                                                                'approval': approval['id']}
            update_state(queue_notice)
            event(f"MR !{iid} 已按 @{who} 的同意自动合入（{sha[:8]}）：{m['title']}")
            notify_merged(f, only=key)
        elif status == 'conflict':
            say(f'已收到合入同意，但该合并请求与 {branch} 有冲突，暂时无法合入。冲突解决并重新评审通过后自动合入，不需要再次回复同意。\n\n' + conflict_steps, '冲突')
            f.set_labels('mr', iid, remove=['review::可合入'], add=['review::需修改'])
        elif status != 'checking':
            if say(f'已收到合入同意，但 {CONFIG["forge"]["kind"]} 显示暂时无法合入（状态：{status}），需要人工处理。', status):
                event(f'MR !{iid} 自动合入受阻：{status}')


def check_merged(st):
    for it in gl.hermes_items("merged"):
        key = f"{it['kind']}:{it['iid']}"
        if it["kind"] == "mr" and key not in st.setdefault("merged_seen", []):
            st["merged_seen"].append(key)
            event(f"MR !{it['iid']} 已合入（{(it.get('merge_commit_sha') or '')[:8]}），待登记 {R['scope']['tasks_file']}/{R['scope']['progress_file']}：{it['title']}")


def main():
    gl.configure(CONFIG)
    dry_run = "--dry-run" in sys.argv
    only = sys.argv[sys.argv.index("--item") + 1] if "--item" in sys.argv else None
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else None
    os.makedirs(f"{RUNS}/locks", exist_ok=True)
    if not only and os.path.exists(f"{RUNS}/PAUSE"):
        log("存在 PAUSE 文件，暂停轮询")
        return
    # 拿一个空闲评审通道；都在用时本轮跳过
    slot = None
    for i in range(SLOTS):
        f = locked(f"{RUNS}/slot-{i}.lock", blocking=False)
        if f.__enter__():
            slot = f
            break
    if not slot:
        log(f"{SLOTS} 个评审通道都在使用，跳过")
        return
    try:
        _main(dry_run, only, mode)
    finally:
        slot.__exit__()


def _main(dry_run, only, mode):
    if only:
        kind, iid = only.split(":")
        d = gl.forge().get_item(kind, int(iid))
        items = [{"kind": kind, "iid": int(iid), "title": d["title"], "updated_at": d["updated_at"],
                  "sha": d.get("sha"), "source_branch": d.get("source_branch"),
                  "key": d.get("sha") or d["updated_at"], "mode": mode}]
    else:
        update_state(check_merged)
        if not dry_run:
            with locked(f"{RUNS}/merge.lock", blocking=False) as got:
                if got:
                    try:
                        auto_merge()
                    except (SystemExit, Exception) as exc:
                        log(f"自动合入检查失败：{exc}")
                        event(f"自动合入检查失败：{exc}")
        now_utc = dt.datetime.now(dt.timezone.utc)
        def ready(x):
            # 用户评论 3 分钟、MR 5 分钟、其余 20 分钟静默期
            t = dt.datetime.fromisoformat(x["updated_at"].replace("Z", "+00:00"))
            m = (USER_QUIET_MINUTES if x.get("author") == "用户" else
                 MR_QUIET_MINUTES if x["kind"] == "mr" and x.get("mode") != "reply" else QUIET_MINUTES)
            return t < now_utc - dt.timedelta(minutes=m)
        try:
            candidates = gl.pending() + gl.replies()
        except Exception as exc:  # 网络或跳板机偶发失败：记一行日志，本轮结束，下一轮再取
            log(f"读取待评审条目失败：{exc}")
            return
        items = sorted((x for x in candidates if ready(x)),
                       key=lambda x: (x.get("author") != "用户", x["updated_at"]))
    for item in items:
        key = f"{item['kind']}:{item['iid']}"
        ilock = locked(f"{RUNS}/locks/{item['kind']}-{item['iid']}.lock", blocking=False)
        if not ilock.__enter__():
            continue  # 其他通道正在评审这一条
        try:
            def claim(st):
                st["runs"] = [t for t in st["runs"] if t > time.time() - 3600]
                rec = st["items"].get(key, {})
                if not only and rec.get("key") == item["key"] and rec.get("attempts", 0) >= MAX_ATTEMPTS:
                    return "skip"
                if not only and len(st["runs"]) >= MAX_RUNS_PER_HOUR and item.get("author") != "用户":
                    return "limit"
                item["prev_key"] = rec.get("key")
                attempts = rec.get("attempts", 0) + 1 if rec.get("key") == item["key"] else 1
                st["items"][key] = {"key": item["key"], "attempts": attempts, "last": now()}
                st["runs"].append(time.time())
                return attempts
            attempts = update_state(claim)
            if attempts == "skip":
                continue
            if attempts == "limit":
                log("达到每小时评审上限，留到下一轮")
                return
            log(f"评审 {key}（第 {attempts} 次）{item['title']}")
            try:
                rc, verdict, run_dir, err = review(item, dry_run)
            except Exception as e:
                log(f"{key} 准备或运行失败：{e}")
                event(f"{key} 评审失败：{e}")
                return
            if err:
                log(f"{key} {err}（{run_dir}）")
                if attempts >= MAX_ATTEMPTS:
                    event(f"{key} 连续 {attempts} 次失败，需人工处理：{err}（{run_dir}）")
                return
            tag = "（试跑，未写入 forge）" if dry_run else ""
            if verdict:
                def record(st):
                    st["items"][key]["verdict"] = verdict.get("verdict")
                    if verdict.get("verdict") == "结论待定":  # 交人工处理，不再自动重试
                        st["items"][key]["attempts"] = MAX_ATTEMPTS
                update_state(record)
                msg = f"{key} 结论：{verdict.get('verdict')}{tag}；{verdict.get('summary', '')}（{run_dir}）"
                log(msg)
                if verdict.get("verdict") in ("可合入", "结论待定") or dry_run:
                    event(msg)
            else:
                log(f"{key} 未取得结论，claude 退出码 {rc}（{run_dir}）")
                if attempts >= MAX_ATTEMPTS:
                    event(f"{key} 连续 {attempts} 次未取得结论，需人工处理（{run_dir}）")
            return  # 每个通道每轮只评审一条
        finally:
            ilock.__exit__()


if __name__ == "__main__":
    main()
