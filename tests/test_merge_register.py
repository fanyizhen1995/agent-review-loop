"""自动合入及登记接口的离线契约测试。"""
import base64
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from kit.forge import GitLabForge, GitHubForge
from test_forge import Recorder

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_permission_environment(monkeypatch):
    for key in ('CLAUDECODE', 'AGENT_REVIEW_HEADLESS', 'DRY_RUN', 'GL_READONLY', 'AGENT_REVIEW_READONLY'):
        monkeypatch.delenv(key, raising=False)


def config(kind='gitlab'):
    return {'forge': {'project': 46 if kind == 'gitlab' else 'o/r'},
            'accounts': {'reviewer': 'reviewer', 'fixer': 'fixer', 'user': 'member'},
            'reviewer': {'merge': {}, 'register': {'branch': 'main', 'tasks_file': 'tasks.json', 'progress_file': 'progress.md'}}}


def cli_module():
    spec = importlib.util.spec_from_file_location('merge_cli', ROOT / 'reviewer/forge_cli.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('data,want', [
    ({'detailed_merge_status': 'mergeable'}, 'mergeable'),
    ({'detailed_merge_status': 'mergeable', 'has_conflicts': True}, 'conflict'),
    ({'detailed_merge_status': 'need_rebase'}, 'conflict'),
    *[({'detailed_merge_status': s}, 'checking') for s in ('checking', 'unchecked', 'preparing', 'approvals_syncing')],
    ({'detailed_merge_status': 'not_approved'}, 'blocked:not_approved'),
])
def test_gitlab_merge_status(data, want):
    assert GitLabForge(config(), Recorder(data)).merge_status(2) == want


@pytest.mark.parametrize('data,want', [
    ({'mergeable': True, 'mergeable_state': 'clean'}, 'mergeable'),
    ({'mergeable': False, 'mergeable_state': 'dirty'}, 'conflict'),
    ({'mergeable': None, 'mergeable_state': 'clean'}, 'checking'),
    ({'mergeable': True, 'mergeable_state': 'unknown'}, 'checking'),
    ({'mergeable': True, 'mergeable_state': 'blocked'}, 'blocked:blocked'),
    ({'mergeable': True, 'mergeable_state': 'unstable'}, 'blocked:unstable'),
])
def test_github_merge_status(data, want):
    assert GitHubForge(config('github'), Recorder(data)).merge_status(2) == want


def test_merge_requires_context_and_constructs_gitlab_request():
    cli = cli_module()
    t = Recorder({'merge_commit_sha': 'merged'})
    f = GitLabForge(config(), t)
    with pytest.raises(SystemExit):
        f.merge(2, 'head', True)
    assert not t.calls
    with cli.merge_permission():
        assert f.merge(2, 'head', True) == 'merged'
    assert t.calls == [('PUT', '/projects/46/merge_requests/2/merge', {'sha': 'head', 'should_remove_source_branch': True})]
    with pytest.raises(SystemExit):
        f.merge(2, 'head', True)


@pytest.mark.parametrize('fork,remove', [(False, True), (True, True), (False, False)])
def test_github_merge_and_safe_branch_deletion(fork, remove):
    cli = cli_module()
    cfg = config('github')
    cfg['reviewer']['merge']['github_method'] = 'squash'
    t = Recorder({'head': {'ref': 'fix/a', 'repo': {'full_name': 'other/r' if fork else 'o/r'}},
                  'base': {'repo': {'full_name': 'o/r'}}}, {'merged': True, 'sha': 'merged'}, None)
    f = GitHubForge(cfg, t)
    with cli.merge_permission():
        assert f.merge(2, 'head', remove) == 'merged'
    assert t.calls[1] == ('PUT', '/repos/o/r/pulls/2/merge', {'sha': 'head', 'merge_method': 'squash'})
    assert len(t.calls) == (3 if remove and not fork else 2)
    if remove and not fork:
        assert t.calls[-1] == ('DELETE', '/repos/o/r/git/refs/heads/fix%2Fa', None)


def test_permissions_block_readonly_headless_and_raw_cli():
    cli = cli_module()
    f = GitLabForge(config(), Recorder())
    for env in ({'DRY_RUN': '1'}, {'CLAUDECODE': '1'}, {'AGENT_REVIEW_HEADLESS': '1'}):
        with patch.dict(os.environ, env), pytest.raises(SystemExit):
            with cli.merge_permission():
                f.merge(2, 'sha', True)
    for command in ('merge', 'commit-files'):
        cli._FORGE = f
        with pytest.raises(SystemExit):
            cli.main([command])
    with pytest.raises(SystemExit):
        cli.check_request({'method': 'PUT', 'path': f.base + '/merge_requests/2/merge', 'body': {'sha': 'x'}}, f.kind, f.base)


@pytest.mark.parametrize('kind', ['gitlab', 'github'])
def test_human_members_and_explicit_approvers(kind):
    cfg = config(kind)
    rows = ([{'username': n, 'access_level': level, 'bot': bot} for n, level, bot in
             [('member', 30, False), ('reporter', 20, False), ('project_x', 40, False), ('bot', 40, True), ('reviewer', 40, False), ('fixer', 40, False)]]
            if kind == 'gitlab' else [{'login': n, 'permissions': p} for n, p in
             [('member', {'push': True}), ('maintainer', {'maintain': True}), ('reader', {'pull': True}), ('reviewer', {'admin': True}), ('fixer', {'push': True})]])
    cls = GitLabForge if kind == 'gitlab' else GitHubForge
    f = cls(cfg, Recorder(rows))
    assert f.human_members() == ({'member'} if kind == 'gitlab' else {'member', 'maintainer'})
    cfg['reviewer']['merge']['approvers'] = ['chosen', 'reviewer', 'fixer']
    assert cls(cfg, Recorder()).human_members() == {'chosen'}


def test_approval_and_revocation_follow_live_baseline():
    cli = cli_module()
    with patch.object(cli, 'humans', return_value={'member', 'other'}):
        def note(body, who='member', ident=1):
            return {'id': ident, 'body': body, 'author': {'username': who}, 'system': False}
        for body in ('lgtm', ' 同意合入！', 'approved', 'approve', 'LGTM\n说明'):
            assert cli.is_approval(note(body))
        for body in ('请 lgtm', '同意合入其他内容', 'lgtm ' + '长' * 201):
            assert not cli.is_approval(note(body))
        assert not cli.is_approval(note('lgtm', 'fixer'))
        assert cli.latest_approval([note('问题'), note('lgtm')])['id'] == 1
        assert cli.latest_approval([note('hold', 'other'), note('lgtm')]) is None
        assert cli.latest_approval([note('lgtm'), note('先别合')])['id'] == 1


def test_gitlab_commit_is_one_atomic_write_with_file_locks():
    cli = cli_module()
    cfg = config()
    t = Recorder({'commit': {'id': 'head'}},
                 {'content': base64.b64encode(b'old').decode(), 'last_commit_id': 'a'},
                 {'content': base64.b64encode(b'old').decode(), 'last_commit_id': 'b'}, {'id': 'new'})
    f = GitLabForge(cfg, t)
    with cli.register_permission():
        assert f.commit_files('main', '登记', {'tasks.json': '新任务', 'progress.md': '新进展'}, 'head') == 'new'
    writes = [c for c in t.calls if c[0] != 'GET']
    assert len(writes) == 1
    method, path, body = writes[0]
    assert (method, path) == ('POST', '/projects/46/repository/commits')
    assert body['actions'] == [
        {'action': 'update', 'file_path': 'tasks.json', 'content': '新任务', 'last_commit_id': 'a'},
        {'action': 'update', 'file_path': 'progress.md', 'content': '新进展', 'last_commit_id': 'b'}]


def test_github_commit_sequence_and_expected_head():
    cli = cli_module()
    cfg = config('github')
    t = Recorder({'object': {'sha': 'head'}}, {'tree': {'sha': 'base'}}, {'sha': 'blob1'}, {'sha': 'blob2'}, {'sha': 'tree'}, {'sha': 'new'}, {'object': {'sha': 'new'}})
    f = GitHubForge(cfg, t)
    with cli.register_permission():
        assert f.commit_files('main', '登记', {'tasks.json': '任务', 'progress.md': '进展'}, 'head') == 'new'
    assert [(m, p.removeprefix(f.base)) for m, p, _ in t.calls] == [
        ('GET', '/git/ref/heads/main'), ('GET', '/git/commits/head'),
        ('POST', '/git/blobs'), ('POST', '/git/blobs'), ('POST', '/git/trees'),
        ('POST', '/git/commits'), ('PATCH', '/git/refs/heads/main')]
    assert t.calls[4][2]['base_tree'] == 'base'
    assert t.calls[5][2]['parents'] == ['head']
    assert t.calls[6][2] == {'sha': 'new', 'force': False}


@pytest.mark.parametrize('kind', ['gitlab', 'github'])
def test_registration_permission_rejects_foreign_paths_branch_and_stale_head(kind):
    cli = cli_module()
    cls = GitLabForge if kind == 'gitlab' else GitHubForge
    f = cls(config(kind), Recorder({'commit': {'id': 'changed'}} if kind == 'gitlab' else {'object': {'sha': 'changed'}}))
    with pytest.raises(SystemExit):
        f.commit_files('main', '登记', {'tasks.json': 'x'}, 'head')
    with cli.register_permission():
        for branch, files in [('other', {'tasks.json': 'x'}), ('main', {'code.py': 'x'}), ('main', {'../tasks.json': 'x'})]:
            with pytest.raises(SystemExit):
                f.commit_files(branch, '登记', files, 'head')
        assert not f.transport.calls
        with pytest.raises(ValueError, match='分支'):
            f.commit_files('main', '登记', {'tasks.json': 'x'}, 'head')
    assert all(c[0] == 'GET' for c in f.transport.calls)


def poll_module():
    spec = importlib.util.spec_from_file_location('merge_poll', ROOT / 'reviewer/poll.py')
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'AGENT_REVIEW_CONFIG': str(ROOT / 'examples/projects/example-gitlab/project.toml')}):
        spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('approved,label,status,want_merge,want_note,want_label', [
    (True, False, 'mergeable', False, '评审通过后自动合入', False),
    (True, True, 'mergeable', True, '已按同意自动合入', False),
    (True, True, 'conflict', False, 'hermes 需执行', True),
    (False, True, 'conflict', False, 'hermes 需执行', True),
    (True, True, 'checking', False, '', False),
    (True, True, 'blocked:not_approved', False, '人工处理', False),
    (False, False, 'mergeable', False, '', False),
])
def test_auto_merge_paths_and_dedup(approved, label, status, want_merge, want_note, want_label):
    from unittest.mock import Mock
    p = poll_module()
    state = {}
    f = Mock()
    f.list_items.return_value = [{'iid': 2, 'title': '修复', 'labels': ['review::可合入'] if label else []}]
    f.get_item.return_value = {'sha': 'head', 'labels': ['review::可合入'] if label else []}
    f.merge_status.return_value = status
    f.merge.return_value = 'merged123'
    approval = {'id': 1, 'author': {'username': 'member'}} if approved else None
    with patch.object(p.gl, 'forge', return_value=f), patch.object(p.gl, 'latest_approval', return_value=approval), patch.object(p, 'update_state', side_effect=lambda fn: fn(state)), patch.object(p, 'event') as ev:
        p.auto_merge()
        assert f.merge.called == want_merge
        assert f.set_labels.called == want_label
        if want_note:
            assert want_note in f.add_note.call_args.args[2]
            if approved:
                assert '@member' in f.add_note.call_args.args[2]
        else:
            f.add_note.assert_not_called()
        if want_merge:
            f.merge.assert_called_once_with(2, 'head', True)
            assert 'member' in ev.call_args.args[0]
        if approved and status != 'mergeable':
            calls = f.add_note.call_count
            p.auto_merge()
            assert f.add_note.call_count == calls


def test_replies_skips_approval_and_includes_other_members():
    from unittest.mock import Mock
    cli = cli_module()
    cli._CONFIG = config()
    f = cli._FORGE = Mock()
    f.human_members.return_value = {'member', 'other'}
    f.list_items.side_effect = lambda kind, *a, **kw: [{'iid': 2, 'title': '修复', 'labels': []}] if kind == 'mr' else []
    n = {'id': 1, 'body': 'lgtm', 'author': {'username': 'other'}, 'created_at': '2026-10-29', 'system': False}
    f.list_notes.return_value = [n]
    assert cli.replies() == []
    n['body'] = '请说明实现'
    assert cli.replies()[0]['author'] == '用户（other）'


def test_members_are_cached_per_forge_instance():
    f = GitLabForge(config(), Recorder([{'username': 'member', 'access_level': 30}]))
    assert f.human_members() == {'member'}
    assert f.human_members() == {'member'}
    assert len(f.transport.calls) == 1


def test_merge_notification_failure_recovers_without_second_merge():
    from unittest.mock import Mock
    p = poll_module()
    state = {}
    f = Mock()
    f.list_items.return_value = [{'iid': 2, 'title': '修复', 'labels': ['review::可合入']}]
    f.get_item.return_value = {'sha': 'head', 'labels': ['review::可合入']}
    f.merge_status.return_value = 'mergeable'
    f.merge.return_value = 'merged123'
    f.add_note.side_effect = [RuntimeError('评论暂时失败'), {}]
    approval = {'id': 1, 'author': {'username': 'member'}}
    with patch.object(p.gl, 'forge', return_value=f), patch.object(p.gl, 'latest_approval', return_value=approval), patch.object(p, 'update_state', side_effect=lambda fn: fn(state)), patch.object(p, 'event') as ev:
        p.auto_merge()
        assert state['merge_notifications']
        assert ev.called
        f.list_items.return_value = []
        p.auto_merge()
        f.merge.assert_called_once()
        assert f.add_note.call_count == 2
        assert '@member' in f.add_note.call_args.args[2]
        assert not state.get('merge_notifications')


@pytest.mark.parametrize('kind,path,method', [
    ('gitlab', '/projects/46/repository/commits', 'POST'),
    ('github', '/repos/o/r/git/blobs', 'POST'),
    ('github', '/repos/o/r/git/trees', 'POST'),
    ('github', '/repos/o/r/git/commits', 'POST'),
    ('github', '/repos/o/r/git/refs/heads/main', 'PATCH'),
])
def test_raw_cli_never_exposes_repository_writes(kind, path, method):
    cli = cli_module()
    base = '/projects/46' if kind == 'gitlab' else '/repos/o/r'
    with cli.register_permission(), pytest.raises(SystemExit):
        cli.check_request({'method': method, 'path': path, 'body': {}}, kind, base)


def test_register_context_resets_after_exception_and_blocks_headless():
    cli = cli_module()
    f = GitLabForge(config(), Recorder())
    with pytest.raises(RuntimeError):
        with cli.register_permission():
            raise RuntimeError('生成中断')
    with pytest.raises(SystemExit):
        f.commit_files('main', '登记', {'tasks.json': 'x'}, 'head')
    with patch.dict(os.environ, {'AGENT_REVIEW_HEADLESS': '1'}), pytest.raises(SystemExit):
        with cli.register_permission():
            f.commit_files('main', '登记', {'tasks.json': 'x'}, 'head')
    assert not f.transport.calls


def test_github_default_merge_method_and_failed_merge_does_not_delete_branch():
    cli = cli_module()
    t = Recorder({'head': {'ref': 'fix/a', 'repo': {'full_name': 'o/r'}}}, {'merged': False, 'message': '保护阻止'})
    f = GitHubForge(config('github'), t)
    with cli.merge_permission(), pytest.raises(ValueError, match='保护阻止'):
        f.merge(2, 'head', True)
    assert t.calls[-1][2]['merge_method'] == 'merge'
    assert all(m != 'DELETE' for m, _, _ in t.calls)


def test_failed_old_notification_does_not_block_new_merges():
    from unittest.mock import Mock
    p = poll_module()
    state = {'merge_notifications': {'mr:1': {'iid': 1, 'who': 'member', 'sha': 'old', 'approval': 1}}}
    f = Mock()
    f.list_items.return_value = [{'iid': 2, 'title': '修复', 'labels': ['review::可合入']}]
    f.get_item.return_value = {'sha': 'head', 'labels': ['review::可合入']}
    f.merge_status.return_value = 'mergeable'
    f.merge.return_value = 'new'
    def add_note(kind, iid, body):
        if iid == 1:
            raise RuntimeError('旧评论禁止')
        return {}
    f.add_note.side_effect = add_note
    with patch.object(p.gl, 'forge', return_value=f), patch.object(p.gl, 'latest_approval', return_value={'id': 2, 'author': {'username': 'member'}}), patch.object(p, 'update_state', side_effect=lambda fn: fn(state)), patch.object(p, 'event'):
        p.auto_merge()
        f.merge.assert_called_once_with(2, 'head', True)
        assert list(state['merge_notifications']) == ['mr:1']


@pytest.mark.parametrize('kind', ['gitlab', 'github'])
@pytest.mark.parametrize('fmt,path,extra', [('changelog-md', 'CHANGELOG.md', {}), ('changelog-md', 'docs/news.md', {'file': 'docs/news.md'}), ('hook', 'docs/log.md', {'paths': ['docs/log.md']})])
def test_new_registration_formats_keep_exact_permission_whitelist(kind, fmt, path, extra):
    from kit.permissions import require_register
    cfg = config(kind)
    cfg['workflow'] = {'register': {'format': fmt, 'branch': 'main', **extra}}
    with cli_module().register_permission():
        require_register(cfg, 'main', {path: '内容'})
        for bad in ['code.py', '../' + path, '/'+path, 'docs//log.md']:
            with pytest.raises(SystemExit):
                require_register(cfg, 'main', {bad: '内容'})


def test_custom_merge_phrases_used_by_cli_and_poll(tmp_path):
    cli = cli_module()
    cli._CONFIG = {'workflow': {'merge': {'approve_phrases': ['可以发布'], 'revoke_phrases': ['等等']}}}
    def note(body):
        return {'body': body, 'author': {'username': 'member'}, 'id': 1}
    with patch.object(cli, 'humans', return_value={'member'}):
        assert cli.is_approval(note('可以发布！'))
        assert not cli.is_approval(note('lgtm'))
        assert cli.latest_approval([note('等等'), note('可以发布')]) is None
        cli._CONFIG['workflow']['merge'] = {'approve_phrases': [], 'revoke_phrases': []}
        assert cli.is_approval(note('lgtm'))


def test_gitlab_changelog_can_create_missing_file_without_masking_other_errors():
    from kit.permissions import register_permission
    from kit.transport import TransportError
    cfg = config()
    cfg['workflow'] = {'register': {'format': 'changelog-md'}}
    t = Recorder({'commit': {'id': 'head'}}, {'id': 'new'})
    f = GitLabForge(cfg, t)
    with patch.object(f, 'read_file', side_effect=TransportError(404)), register_permission():
        assert f.commit_files('main', '登记', {'CHANGELOG.md': '新记录'}, 'head') == 'new'
    assert t.calls[-1][2]['actions'] == [{'action': 'create', 'file_path': 'CHANGELOG.md', 'content': '新记录'}]
    with patch.object(f, 'read_file', side_effect=TransportError(403)), patch.object(f, 'branch_head', return_value='head'), register_permission(), pytest.raises(TransportError):
        f.commit_files('main', '登记', {'CHANGELOG.md': '新记录'}, 'head')
