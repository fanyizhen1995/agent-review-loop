#!/usr/bin/env python3
"""评审方的受限 forge 入口：读取、评论、评审标签和关闭。"""
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kit.conventions import REVIEW_LABELS
from kit.forge import approval_patterns
from kit.permissions import merge_permission, register_permission

_CONFIG = None
_FORGE = None


def configure(config=None):
    """延迟读取参数，导入模块时不读凭据或发请求。"""
    global _CONFIG, _FORGE
    from kit.config import load_config
    from kit.forge import make_forge
    _CONFIG = config or load_config(os.environ.get('AGENT_REVIEW_CONFIG') or Path(__file__).with_name('project.toml'))
    _FORGE = make_forge(_CONFIG, role='reviewer')
    return _FORGE


def forge():
    return _FORGE or configure()


def readonly():
    return any(os.environ.get(k) == '1' for k in ('GL_READONLY', 'AGENT_REVIEW_READONLY', 'DRY_RUN'))


def check_request(q, kind, base):
    """保留线上白名单；同时检查路径边界及 GitHub 对应写操作。"""
    method, path, body = q['method'].upper(), q['path'], q.get('body')
    decoded = unquote(path)
    if not path.startswith(base + '/') or '..' in decoded.split('/') or '%' in decoded or '\\' in decoded or '#' in decoded:
        sys.exit('拒绝: 路径不在当前项目下或含非法路径段')
    if method == 'GET':
        return
    if readonly():
        sys.exit(f'拒绝: 只读模式下不允许 {method} {path}')
    if '?' in path:
        sys.exit('拒绝: 写操作路径不允许查询参数')
    base = re.escape(base)
    if kind == 'gitlab':
        if method == 'POST' and re.fullmatch(rf'{base}/(issues|merge_requests)/\d+/notes', path):
            if not isinstance(body, dict) or set(body) != {'body'}:
                sys.exit('拒绝: 评论只允许 body')
            return
        if method == 'PUT' and re.fullmatch(rf'{base}/(issues|merge_requests)/\d+/notes/\d+', path):
            if not isinstance(body, dict) or set(body) != {'body'}:
                sys.exit('拒绝: 编辑评论只允许修改 body')
            return
        if method == 'PUT' and re.fullmatch(rf'{base}/(issues|merge_requests)/\d+', path):
            if not body or set(body) - {'add_labels', 'remove_labels', 'state_event'}:
                sys.exit('拒绝: 只允许修改评审标签或关闭')
            if body.get('state_event', 'close') != 'close':
                sys.exit('拒绝: state_event 只允许 close')
            for key in ('add_labels', 'remove_labels'):
                validate_labels(filter(None, body.get(key, '').split(',')))
            return
    if kind == 'github':
        if method == 'POST' and re.fullmatch(rf'{base}/issues/\d+/comments', path) or method == 'PATCH' and re.fullmatch(rf'{base}/issues/comments/\d+', path):
            if not isinstance(body, dict) or set(body) != {'body'}:
                sys.exit('拒绝: 评论只允许 body')
            return
        if method == 'PATCH' and re.fullmatch(rf'{base}/(issues|pulls)/\d+', path) and body == {'state': 'closed'}:
            return
        if method == 'POST' and re.fullmatch(rf'{base}/issues/\d+/labels', path):
            if not isinstance(body, dict) or set(body) != {'labels'} or not isinstance(body['labels'], list):
                sys.exit('拒绝: 标签请求格式错误')
            validate_labels(body['labels'])
            return
        if method == 'DELETE' and re.fullmatch(rf'{base}/issues/\d+/labels/[^/]+', path):
            validate_labels([unquote(path.rsplit('/', 1)[1])])
            return
    sys.exit(f'拒绝: 不允许的操作 {method} {path}')


def validate_labels(labels):
    for label in labels:
        if label not in REVIEW_LABELS:
            sys.exit(f'拒绝: 只允许操作评审标签，收到 {label}')


def username(author):
    return author.get('username', author.get('login', '')) if isinstance(author, dict) else author


def _ownership(path):
    """原始请求和命令入口都校验评论所有权。"""
    f = forge()
    note = f.request('GET', path)
    if username(note.get('author', note.get('user', {}))) != _CONFIG['accounts']['reviewer']:
        sys.exit('拒绝: 评论不是评审账号发的')


def call(reqs):
    f = forge()
    for q in reqs:
        check_request(q, f.kind, f.base)
    results = []
    for q in reqs:
        if q['method'].upper() in ('PUT', 'PATCH') and re.search(r'/(notes|comments)/\d+$', q['path']):
            _ownership(q['path'])
        results.append(f.request(q['method'], q['path'], q.get('body')))
    return results


def pending():
    f = forge()
    source = _CONFIG.get('labels', {}).get('source', 'source::agent')
    items = []
    for kind in ('issue', 'mr'):
        for item in f.list_items(kind, 'opened', [source, 'review::待评审'], limit=100):
            x = {key: item[key] for key in ('iid', 'title', 'updated_at')}
            x['kind'] = kind
            if kind == 'mr':
                x.update(sha=item['sha'], source_branch=item['source_branch'])
            x['key'] = f"{x['sha']}|{x['updated_at']}" if kind == 'mr' else x['updated_at']
            items.append(x)
    return items


def humans():
    return forge().human_members()


def _merge_settings():
    config = _CONFIG or {}
    return config.get('workflow', {}).get('merge', config.get('reviewer', {}).get('merge', {}))


def is_approval(note, merge_settings=None):
    body = (note.get('body') or '').strip()
    approval, _ = approval_patterns(_merge_settings() if merge_settings is None else merge_settings)
    return (not note.get('system') and username(note.get('author', {})) in humans()
            and len(body) <= 200 and bool(approval.match(body)))


def latest_approval(notes, merge_settings=None):
    """调用方按时间倒序提供评论；最近的撤回使所有更早同意失效。"""
    members = humans()
    settings = _merge_settings() if merge_settings is None else merge_settings
    _, revoke = approval_patterns(settings)
    for note in notes:
        if note.get('system') or username(note.get('author', {})) not in members:
            continue
        if revoke.match((note.get('body') or '').strip()):
            return None
        if is_approval(note, settings):
            return note
    return None


def replies():
    f = forge()
    accounts = _CONFIG['accounts']
    source = _CONFIG.get('labels', {}).get('source', 'source::agent')
    out = []
    for kind in ('issue', 'mr'):
        for item in f.list_items(kind, 'opened', [source], limit=100):
            notes = [n for n in f.list_notes(kind, item['iid'], sort='desc', limit=20) if not n['system']]
            if not notes:
                continue
            note = notes[0]
            who, at = username(note['author']), note['created_at']
            settled = {'review::已确认', 'review::可合入'} & set(item['labels'])
            if is_approval(note):
                continue  # 同意由自动合入处理。
            if who in humans() and (who != accounts['user'] or at >= accounts.get('user_note_since', '')):
                author = '用户' if who == accounts['user'] else f'用户（{who}）'
            elif who in (accounts['fixer'], accounts['user']) and settled:
                author = 'hermes' if who == accounts['fixer'] else accounts.get('legacy_fixer_author', accounts['user'])
            else:
                continue
            out.append({'kind': kind, 'iid': item['iid'], 'title': item['title'], 'mode': 'reply',
                        'author': author, 'updated_at': at, 'note_id': note['id'],
                        'sha': item.get('sha'), 'source_branch': item.get('source_branch'),
                        'key': f"note-{note['id']}"})
    return out


def hermes_items(state):
    f = forge()
    source = _CONFIG.get('labels', {}).get('source', 'source::agent')
    out = []
    for kind in (('mr',) if state == 'merged' else ('issue', 'mr')):
        for item in f.list_items(kind, state, [source], limit=100, **({'order_by': 'updated_at'} if f.kind == 'gitlab' else {})):
            summary = {key: item[key] for key in ('iid', 'title', 'labels', 'updated_at')}
            summary['kind'] = kind
            if kind == 'mr':
                summary['merge_commit_sha'] = item.get('merge_commit_sha')
            out.append(summary)
    return out


def _write_guard():
    if readonly():
        sys.exit('拒绝: 只读模式下不允许写操作')


def main(argv):
    if not argv:
        sys.exit('用法: pending | list | show | notes | diff | commits | comment | edit-note | label | close | issues-search | get')
    f = forge()
    command, args = argv[0], argv[1:]
    if command == 'pending':
        out = pending()
    elif command == 'list':
        out = hermes_items(args[0] if args else 'opened')
    elif command == 'show':
        out = f.get_item(args[0], int(args[1]))
    elif command == 'notes':
        out = [dict(n, author=username(n['author'])) for n in f.list_notes(args[0], int(args[1]), sort='asc', limit=100)]
    elif command == 'diff':
        out = f.changes(int(args[0]))
    elif command == 'commits':
        out = f.commits(int(args[0]))
    elif command == 'issues-search':
        out = f.search_issues(' '.join(args))[:50]
    elif command == 'get':
        path = f.base + '/' + args[0].lstrip('/')
        if f.kind == 'github' and args[0].startswith('merge_requests'):
            path = f.base + '/' + args[0].replace('merge_requests', 'pulls', 1).replace('state=opened', 'state=open')
        out = call([{'method': 'GET', 'path': path}])[0]
    elif command in ('comment', 'edit-note', 'label', 'close'):
        _write_guard()
        kind, iid = args[0], int(args[1])
        if command == 'comment':
            out = f.add_note(kind, iid, Path(args[2]).read_text(encoding='utf-8'))
        elif command == 'edit-note':
            note_id = int(args[2])
            note = f.get_note(kind, iid, note_id)
            if username(note.get('author', note.get('user', {}))) != _CONFIG['accounts']['reviewer']:
                sys.exit('拒绝: 评论不是评审账号发的')
            out = f.edit_note(kind, iid, note_id, Path(args[3]).read_text(encoding='utf-8'))
        elif command == 'label':
            add = args[args.index('--add') + 1].split(',') if '--add' in args else []
            remove = args[args.index('--remove') + 1].split(',') if '--remove' in args else []
            if not add and not remove:
                sys.exit('拒绝: 未指定标签变更')
            validate_labels(add + remove)
            out = f.set_labels(kind, iid, add=add, remove=remove)
        else:
            out = f.close(kind, iid)
    else:
        sys.exit(f'未知命令 {command}')
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == '__main__':
    main(sys.argv[1:])
