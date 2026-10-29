#!/usr/bin/env python3
"""按基准清理闲置子任务 worktree、临时文件，并提醒 token 到期。"""
import os
import subprocess
import time

import sys
from pathlib import Path
# 同时支持源码目录和安装后的 scripts/kit 目录。
for _parent in Path(__file__).resolve().parents:
    if (_parent / "kit").is_dir():
        sys.path.insert(0, str(_parent))
        break
from kit.config import load_config
from kit.forge import make_forge


def configuration():
    location = Path(__file__).resolve().parent / "project.toml"
    import os
    return load_config(os.environ.get("AGENT_REVIEW_CONFIG") or (str(location) if location.exists() else None))

CFG = configuration()
REPO = os.path.expanduser(CFG["fixer"]["repo"])
ROOTS = (os.path.expanduser(CFG["fixer"]["scratch_dir"]), os.path.join(REPO, ".worktrees"))
MIN_AGE = CFG["fixer"]["worktree_min_age_seconds"]


def git(*a, check=False):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, check=check).stdout


def busy_paths():
    """当前用户进程的工作目录，用于跳过正在使用的 worktree。"""
    out = set()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                out.add(os.readlink(f"/proc/{pid}/cwd"))
            except OSError:
                pass
    return out


def newest_mtime(path):
    newest = os.path.getmtime(path)
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in CFG["fixer"]["cleanup_skip_dirs"]]
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return newest


def token_expiry():
    """到期提醒按配置天数判断，每天最多一次；GitHub 不返回到期时间。"""
    import datetime as dt
    state = os.path.expanduser(CFG["fixer"]["token_notice_file"])
    today = dt.date.today().isoformat()
    if os.path.exists(state) and open(state).read().strip() == today:
        return None
    try:
        exp = make_forge(CFG, role="fixer").token_expiry()
        if isinstance(exp, dict):
            exp = exp.get("expires_at")
    except Exception:
        return None
    if not exp:
        return None
    left = (dt.date.fromisoformat(exp) - dt.date.today()).days
    if left > CFG["fixer"]["token_notice_days"]:
        return None
    open(state, "w").write(today)
    return f"[token 即将到期] hermes 的 {CFG['forge'].get('display_name', CFG['forge']['kind'])} token 将于 {exp} 到期（剩 {left} 天），请在 {CFG['forge'].get('display_name', CFG['forge']['kind'])} 新建后交评审方替换"


def main():
    removed = []
    busy = busy_paths()
    blocks = git("worktree", "list", "--porcelain").strip().split("\n\n")
    for b in blocks:
        lines = dict(l.split(" ", 1) for l in b.splitlines() if " " in l)
        path = lines.get("worktree", "")
        if not path or os.path.realpath(path) == os.path.realpath(REPO):
            continue
        if not any(os.path.realpath(path).startswith(os.path.realpath(r) + os.sep) for r in ROOTS):
            continue
        if any(p == path or p.startswith(path + os.sep) for p in busy):
            continue
        if os.path.exists(path) and time.time() - newest_mtime(path) < MIN_AGE:
            continue
        git("worktree", "remove", "--force", path)
        removed.append(path.replace(os.path.expanduser("~"), "~"))
    git("worktree", "prune")
    in_use = {l.split(" ", 1)[1] for l in git("worktree", "list", "--porcelain").splitlines() if l.startswith("branch ")}
    for ref in git("for-each-ref", "--format=%(refname)", "refs/heads/" + CFG["fixer"]["subagent_branch_prefix"]).split():
        if ref not in in_use:
            git("update-ref", "-d", ref)
            removed.append(ref.replace("refs/heads/", ""))
    # 清理达到配置时限、名称符合配置规则的临时文件。
    scratch = ROOTS[0]
    for name in os.listdir(scratch):
        path = os.path.join(scratch, name)
        if not (any(name.endswith(suffix) for suffix in CFG["fixer"]["scratch_suffixes"])
                or any(name.startswith(prefix) for prefix in CFG["fixer"]["scratch_prefixes"])):
            continue
        if time.time() - os.path.getmtime(path) < CFG["fixer"]["scratch_min_age_seconds"]:
            continue
        subprocess.run(["rm", "-rf", "--", path])
        removed.append(os.path.join(CFG["fixer"]["scratch_dir"], name))
    msg = token_expiry()
    if msg:
        print(msg)
    if removed:
        print(CFG["project"]["name"] + " worktree 清理：删除 " + str(len(removed)) + " 项：" + "、".join(removed))


if __name__ == "__main__":
    main()
