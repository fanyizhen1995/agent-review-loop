#!/usr/bin/env python3
"""每日登记执行方已合入 MR；--dry-run 只读取并打印计划。"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kit.config import load_config
from kit.forge import approval_patterns, make_forge
from kit.permissions import register_permission, validate_register_files
from kit.transport import TransportError


class State:
    """与 poll 共用 state.lock；登记锁只保护每日登记，不阻塞模型评审。"""
    def __init__(self, runs):
        self.root = Path(runs)

    def read(self):
        path = self.root / 'state.json'
        return json.loads(path.read_text()) if path.exists() else {}

    @contextmanager
    def lock(self, name):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / name).open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def mark(self, keys):
        with self.lock('state.lock'):
            state = self.read()
            state.setdefault('items', {})
            state.setdefault('runs', [])
            state.setdefault('merged_seen', [])
            registered = state.setdefault('registered', [])
            registered.extend(key for key in keys if key not in registered)
            temporary = self.root / f'state.json.{os.getpid()}.tmp'
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1) + '\n')
            temporary.replace(self.root / 'state.json')

    def event(self, text):
        with (self.root / 'events.md').open('a', encoding='utf-8') as stream:
            stream.write(f'- {datetime.now():%Y-%m-%d %H:%M:%S} {text}\n')


def issue_number(mr):
    body = mr.get('description', '')
    match = re.search(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b', body, re.I)
    if not match:
        raise ValueError(f"MR !{mr['iid']} 缺少关联 issue（Closes #编号）")
    return int(match[1])


def task_id(mr, issue, prefix):
    branch = mr.get('source_branch', '').lower()
    # 分支名形如 fix/37-body-size-limit：去掉类型前缀与 issue 号，避免 id 中重复
    branch = re.sub(r'^(fix|perf|robust|refactor|feat|chore)/', '', branch)
    branch = re.sub(rf'^{issue["iid"]}-', '', branch)
    slug = re.sub(r'[^a-z0-9]+', '-', branch).strip('-')
    if not slug:
        raise ValueError(f"MR !{mr['iid']} 缺少可用的源分支 slug")
    return f"{prefix}-{issue['iid']}-{slug}"


def problem_sentence(mr, fallback):
    match = re.search(r'^\s*#{1,6}\s+问题说明\s*\n(.*?)(?=^\s*#{1,6}\s|\Z)', mr.get('description', ''), re.M | re.S)
    if not match:
        return fallback.rstrip('。.!！?？')
    text = re.sub(r'\s+', ' ', match[1]).strip()
    return re.split(r'[。！？]|(?<=[a-zA-Z])\.(?:\s|$)', text, maxsplit=1)[0].strip() or fallback.rstrip('。.!！?？')


def build_registration(tasks_text, progress_text, records, settings, today, full_command):
    """纯生成函数：保留键顺序和原缩进，跳过任务 ID 已存在的 MR。"""
    data = json.loads(tasks_text)
    tasks = data['tasks']
    if not isinstance(tasks, list):
        raise ValueError('任务文件的 tasks 必须为列表')
    match = re.search(r'\n([ \t]+)"', tasks_text)
    indent = match[1] if match else '  '
    ids = {t['id'] for t in tasks}
    entries, lines, iids = [], [], []
    for record in records:
        mr, issue = record['mr'], record['issue']
        ident = task_id(mr, issue, settings.get('id_prefix', 'hermes'))
        if ident in ids:
            continue
        title = re.sub(r'^[a-zA-Z]+(?:\([^\n)]*\))?!?:\s*', '', mr['title']).strip()
        paths = sorted({c.get('new_path') or c.get('filename', '') for c in record['changes']
                        if not c.get('deleted_file') and c.get('status') != 'removed'})
        tests = [p for p in paths if p.startswith('tests/')]
        verify = 'python3 -m pytest -q ' + ' '.join(shlex.quote(p) for p in tests) if tests else full_command
        labels = set(mr.get('labels', [])) | set(issue.get('labels', []))
        priority = next((p for label, p in [('priority::高', 'high'), ('priority::中', 'medium'), ('priority::低', 'low')] if label in labels), 'medium')
        entry = {'id': ident, 'title': title,
                 'description': issue['title'] + f'（hermes 提出、评审通过、成员同意后合入，MR !{mr["iid"]}）',
                 'status': 'done', 'priority': priority, 'blocked_by': '', 'verify': verify,
                 'requires_eval': bool(re.match(r'^refactor(?:\(|!?:)', mr['title'], re.I))}
        entries.append(entry)
        ids.add(ident)
        iids.append(mr['iid'])
        if record.get('approver'):
            approval = f"同意人 @{record['approver']}"
        elif record.get('merged_by'):
            approval = f"由 @{record['merged_by']} 手动合入"
        else:
            approval = '同意人未记录'
        sha = (mr.get('merge_commit_sha') or mr.get('squash_commit_sha') or '')[:8]
        lines.append(f"- !{mr['iid']}（#{issue['iid']}）{title}：{problem_sentence(mr, issue['title'])}。合入提交 {sha or '未记录'}，{approval}。验证：{verify}。")
    message = f"docs(progress): 登记执行方合入的 {len(iids)} 项修复（{' '.join('!' + str(n) for n in iids)}）"
    if not entries:
        return {'entries': [], 'progress': '', 'message': message, 'files': {}}
    tasks.extend(entries)
    data['last_updated'] = today
    paragraph = f'# {today}：执行方合入登记（{len(entries)} 项）\n\n' + '\n'.join(lines) + '\n- 评审：由评审方完成评审，评论见各 MR。\n\n'
    return {'entries': entries, 'progress': paragraph, 'message': message,
            'files': {settings['tasks_file']: json.dumps(data, ensure_ascii=False, indent=indent) + '\n',
                      settings['progress_file']: paragraph + progress_text}}


def build_changelog(text, records, settings, today):
    """在最新日期段逐行登记，以独立 MR 编号避免重复。"""
    existing = {int(iid) for iid in re.findall(r'^- !(\d+)(?=\s|[（(])', text, re.M)}
    entries, lines = [], []
    for record in records:
        mr = record['mr']
        if mr['iid'] in existing:
            continue
        title = re.sub(r'\s+', ' ', mr['title']).strip()
        url = mr.get('web_url', '')
        lines.append(f"- !{mr['iid']} {title}" + (f" ({url})" if url else ''))
        entries.append(mr)
        existing.add(mr['iid'])
    message = f"docs(changelog): 登记合入的 {len(entries)} 项修改（{' '.join('!' + str(m['iid']) for m in entries)}）"
    if not entries:
        return {'entries': [], 'progress': '', 'message': message, 'files': {}}
    paragraph = '\n'.join(lines) + '\n'
    dates = list(re.finditer(r'^## (\d{4}-\d{2}-\d{2})[^\n]*\n?', text, re.M))
    if dates and dates[0][1] == today:
        offset = dates[1].start() if len(dates) > 1 else len(text)
        text = text[:offset].rstrip() + '\n' + paragraph + '\n' + text[offset:]
    else:
        offset = dates[0].start() if dates else (len(text) if text.startswith('# ') else 0)
        prefix = text[:offset].rstrip()
        text = (prefix + '\n\n' if prefix else '') + f'## {today}\n\n' + paragraph + '\n' + text[offset:]
    path = settings.get('file') or settings.get('changelog_file') or 'CHANGELOG.md'
    return {'entries': entries, 'progress': paragraph, 'message': message, 'files': {path: text}}


def build_hook(config, pending, settings):
    """只执行已安装 hooks 内的单个脚本，不经 shell 解析命令。"""
    name = settings.get('hook', '')
    if not isinstance(name, str) or not name or Path(name).is_absolute() or any(p in ('', '.', '..') for p in name.split('/')) or '\\' in name:
        raise ValueError('hook 必须是 hooks 下的相对脚本路径')
    installed = Path(config['reviewer']['install_dir']).resolve()
    root = installed / 'hooks'
    script = (root / name).resolve()
    if root.is_symlink() or not script.is_relative_to(root) or not script.is_file():
        raise ValueError('hook 不在已安装 hooks 目录内')
    process = subprocess.run([str(script)], input=json.dumps(pending, ensure_ascii=False),
                             capture_output=True, text=True, timeout=60, cwd=installed, check=True)
    files = json.loads(process.stdout)
    if files:
        validate_register_files(config, settings.get('branch', 'main'), files)
    elif not isinstance(files, dict):
        raise ValueError('hook 输出必须是路径到文本的 JSON 对象')
    return {'entries': pending, 'progress': '', 'message': f'docs(register): 登记合入的 {len(pending)} 项修改', 'files': files}


def approving_member(notes, members, merged_at=None, settings=None):
    """只使用合入前的成员同意，不把机器人或合入后的评论算作证据。"""
    approval, revoke = approval_patterns(settings)
    for note in notes:
        who = note.get('author', {}).get('username') or note.get('author', {}).get('login', '')
        if note.get('system') or who not in members or (merged_at and note.get('created_at', '') > merged_at):
            continue
        body = (note.get('body') or '').strip()
        if revoke.match(body):
            return None
        if len(body) <= 200 and approval.match(body):
            return who
    return None


def _register(config, f, state, dry_run):
    settings = config.get('workflow', {}).get('register', config['reviewer'].get('register', {}))
    registered = state.read().get('registered', [])
    pending = sorted((m for m in f.list_merged([config.get('labels', {}).get('source', 'source::agent')])
                      if f"mr:{m['iid']}" not in registered), key=lambda m: m['iid'])
    if not pending:
        return
    keys = [f"mr:{m['iid']}" for m in pending]
    if settings.get('format', 'none') == 'none':
        for mr in pending:
            text = f"MR !{mr['iid']} 已登记（仅事件）：{mr['title']}"
            if dry_run:
                print(text)
            else:
                state.event(text)
        if not dry_run:
            state.mark(keys)
        return
    if settings['format'] not in ('agents-tasks-json', 'changelog-md', 'hook'):
        raise ValueError('不支持的登记格式：' + settings['format'])
    branch = settings.get('branch', 'main')
    head = f.branch_head(branch)
    if settings['format'] == 'hook':
        plan = build_hook(config, pending, settings)
        return finish_registration(config, f, state, settings, plan, keys, head, dry_run)
    if settings['format'] == 'changelog-md':
        path = settings.get('file') or settings.get('changelog_file') or 'CHANGELOG.md'
        try:
            text = f.read_file(path, head)['content']
        except TransportError as exc:
            if exc.status != 404:
                raise
            text = ''
        plan = build_changelog(text, [{'mr': mr} for mr in pending], settings, datetime.now().date().isoformat())
        return finish_registration(config, f, state, settings, plan, keys, head, dry_run)
    tasks_text = f.read_file(settings['tasks_file'], head)['content']
    progress_text = f.read_file(settings['progress_file'], head)['content']
    existing = {t['id'] for t in json.loads(tasks_text)['tasks']}
    members = None
    records = []
    for summary in pending:
        mr = f.get_item('mr', summary['iid'])
        issue_id = issue_number(mr)
        if task_id(mr, {'iid': issue_id}, settings.get('id_prefix', 'hermes')) in existing:
            continue
        issue = f.get_item('issue', issue_id)
        notes = f.list_notes('mr', mr['iid'], sort='desc', limit=None)
        if notes and members is None:
            members = f.human_members()
        records.append({'mr': mr, 'issue': issue, 'changes': f.changes(mr['iid'])['changes'],
                        'approver': approving_member(notes, members or set(), mr.get('merged_at'), config.get('workflow', {}).get('merge', config['reviewer'].get('merge', {}))),
                        'merged_by': ((mr.get('merged_by') or {}).get('username') or (mr.get('merged_by') or {}).get('login'))})
    plan = build_registration(tasks_text, progress_text, records, settings, datetime.now().date().isoformat(), config['reviewer']['tests']['full_command'])
    return finish_registration(config, f, state, settings, plan, keys, head, dry_run)


def finish_registration(config, f, state, settings, plan, keys, head, dry_run):
    if plan['files']:
        validate_register_files(config, settings.get('branch', 'main'), plan['files'])
    if dry_run:
        print(json.dumps(plan['entries'], ensure_ascii=False, indent=2))
        print(plan['progress'])
        print(plan['message'] if plan['files'] else '无新增条目，无需提交')
        return
    if plan['files']:
        with register_permission():
            commit = f.commit_files(settings.get('branch', 'main'), plan['message'], plan['files'], head)
        state.event(f"登记成功：{plan['message']}，提交 {commit[:8]}")
    else:
        state.event('登记跳过：任务文件已存在对应 ID，' + ' '.join(keys))
    state.mark(keys)


def run(config, dry_run=False, forge=None):
    settings = config.get('workflow', {}).get('register', config['reviewer'].get('register', {}))
    if not settings.get('enabled', False):
        return
    state = State(config['reviewer']['runs_dir'])
    f = forge or make_forge(config)
    if dry_run:
        _register(config, f, state, True)
        return
    with state.lock('register.lock'):
        try:
            _register(config, f, state, False)
        except (SystemExit, Exception) as exc:
            state.event(f'登记失败：{exc}')
            print(f'登记失败：{exc}', file=sys.stderr)
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    config = load_config(args.config or os.environ.get('AGENT_REVIEW_CONFIG') or Path(__file__).with_name('project.toml'))
    if run(config, args.dry_run) is False:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
