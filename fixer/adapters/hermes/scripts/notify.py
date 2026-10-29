#!/usr/bin/env python3
"""只通知用户评论的评审回复和 MR 新增可合入标签；首次只记录基线。"""
import json
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


def configuration():
    location = Path(__file__).resolve().parent / "project.toml"
    import os
    return load_config(os.environ.get("AGENT_REVIEW_CONFIG") or (str(location) if location.exists() else None))


def main():
    cfg = configuration()
    forge = make_forge(cfg, role="fixer")
    LABEL = cfg["forge"]["source_label"]
    REVIEW_BOT = cfg["accounts"]["reviewer"]
    USER_MENTION = cfg["accounts"]["user_mention"]
    STATE_FILE = os.path.expanduser(cfg["fixer"]["notify_state_file"])
    MAX_FAIL_ALERTS = cfg["fixer"]["max_fail_alerts"]
    try:
        st = json.loads(Path(STATE_FILE).read_text())
    except Exception:
        st = {}
    first = st.get("version") != 2
    seen = st.get("items", {})
    out = []
    try:
        items = [("issue", "#", x) for x in forge.list_items("issue", "opened", [LABEL], limit=100)]
        items += [("merge_requests", "!", x) for x in forge.list_items("mr", "opened", [LABEL], limit=100)]
        for kind, mark, x in items:
            key = f"{mark}{x['iid']}"
            prev = seen.get(key, {})
            base = "issues" if kind == "issue" else "merge_requests"
            notes = forge.list_notes("issue" if kind == "issue" else "mr", x["iid"], sort="asc", limit=100)
            last_id = max([n["id"] for n in notes], default=0)
            mergeable = "review::可合入" in x.get("labels", [])
            if not first:
                for n in notes:
                    if (n["id"] > prev.get("last_note_id", 0) and not n["system"]
                            and n["author"]["username"] == REVIEW_BOT
                            and n["body"].lstrip().startswith(USER_MENTION)):
                        body = n["body"].lstrip()[len(USER_MENTION):].strip().replace("\r", "")
                        out.append(f"[你的评论有回复] {key} {x['title'][:50]}\n  {body[:200]}{'…' if len(body) > 200 else ''}\n  {n.get('web_url') or x['web_url'] + ('#note_' if cfg['forge']['kind'] == 'gitlab' else '#issuecomment-') + str(n['id'])}")
                if kind == "merge_requests" and mergeable and not prev.get("mergeable"):
                    out.append(f"[可合入] {key} {x['title'][:60]}\n  在合并请求下回复 lgtm 或 同意合入 即自动合入\n  {x['web_url']}")
            seen[key] = {"last_note_id": max(last_id, prev.get("last_note_id", 0)), "mergeable": mergeable}
        st["fail_count"] = 0
    except Exception as e:
        st["fail_count"] = st.get("fail_count", 0) + 1
        if st["fail_count"] == MAX_FAIL_ALERTS:
            out.append(f"[通知脚本异常] 连续 {MAX_FAIL_ALERTS} 次拉取 {cfg['forge'].get('display_name', cfg['forge']['kind'])} 失败：{e}")
    st.update(version=2, items=seen)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(st, ensure_ascii=False, indent=1))
    if out:
        print("\n".join(out))


if __name__ == "__main__":
    main()
