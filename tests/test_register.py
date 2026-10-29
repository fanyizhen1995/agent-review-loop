"""使用虚构任务数据验证登记生成、历史补登和失败恢复。"""
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_register():
    spec = importlib.util.spec_from_file_location('reviewer_register', ROOT / 'reviewer/register.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def settings():
    return {'enabled': True, 'branch': 'main', 'tasks_file': 'tasks.json', 'progress_file': 'progress.md',
            'format': 'agents-tasks-json', 'id_prefix': 'hermes'}


def record(iid=14, kind='fix'):
    return {'mr': {'iid': iid, 'title': f'{kind}(scheduler): 修复排队状态', 'description': 'Closes #41\n\n## 问题说明\n\n排队状态没有更新。第二句。\n\n## 解决方式\n修正。',
                   'source_branch': f'{kind}/queue-{iid}', 'labels': ['priority::高'], 'merge_commit_sha': '123456789abcdef'},
            'issue': {'iid': 41, 'title': '排队状态异常'}, 'approver': 'member',
            'changes': [{'new_path': 'tests/test_queue.py'}, {'new_path': 'src/queue.py'}]}


@pytest.mark.parametrize('indent', [2, 4])
def test_generation_preserves_files_keys_indentation_and_prepends(indent):
    mod = load_register()
    original = json.dumps({'project': '示例项目', 'last_updated': '2026-10-28',
                           'tasks': [{'id': 'example-1-existing', 'title': '既有任务', 'status': 'done'}]},
                          ensure_ascii=False, indent=indent) + '\n'
    progress = '# 2026-10-28：已有进展\n\n- 已完成一项工作。\n'
    plan = mod.build_registration(original, progress, [record(), record(16, 'refactor')], settings(), '2026-10-29', '全量测试')
    written = plan['files']['tasks.json']
    result = json.loads(written)
    assert list(result) == list(json.loads(original))
    assert result['last_updated'] == '2026-10-29'
    first, second = result['tasks'][-2:]
    assert list(first) == ['id', 'title', 'description', 'status', 'priority', 'blocked_by', 'verify', 'requires_eval']
    assert first == {'id': 'hermes-41-queue-14', 'title': '修复排队状态',
                     'description': '排队状态异常（hermes 提出、评审通过、成员同意后合入，MR !14）',
                     'status': 'done', 'priority': 'high', 'blocked_by': '',
                     'verify': 'python3 -m pytest -q tests/test_queue.py', 'requires_eval': False}
    assert second['requires_eval'] is True
    assert '\n' + ' ' * indent + '"project"' in written
    assert plan['files']['progress.md'].endswith(progress)
    assert plan['progress'].startswith('# 2026-10-29：执行方合入登记（2 项）\n')
    assert '- !14（#41）修复排队状态：排队状态没有更新。合入提交 12345678，同意人 @member。验证：python3 -m pytest -q tests/test_queue.py。' in plan['progress']
    assert plan['progress'].rstrip().endswith('- 评审：由评审方完成评审，评论见各 MR。')
    assert plan['message'] == 'docs(progress): 登记执行方合入的 2 项修复（!14 !16）'
    again = mod.build_registration(written, plan['files']['progress.md'], [record()], settings(), '2026-10-29', '全量测试')
    assert again['entries'] == [] and again['files'] == {}


def test_fallbacks_priority_missing_tests_issue_title_and_no_approval():
    mod = load_register()
    r = record()
    r.update(changes=[], approver=None)
    r['mr'].update(description='Closes #41', labels=[])
    plan = mod.build_registration('{"tasks": []}', '旧记录', [r], settings(), '2026-10-29', 'python3 -m pytest -q tests')
    assert plan['entries'][0]['priority'] == 'medium'
    assert plan['entries'][0]['verify'] == 'python3 -m pytest -q tests'
    assert '排队状态异常。合入提交' in plan['progress']
    assert '同意人未记录' in plan['progress']


@pytest.mark.parametrize('failure', [False, True])
def test_historical_registration_success_and_failure(tmp_path, failure, monkeypatch):
    monkeypatch.delenv('CLAUDECODE', raising=False)
    mod = load_register()
    cfg = {'reviewer': {'register': settings(), 'runs_dir': str(tmp_path), 'tests': {'full_command': '全量测试'}}, 'labels': {'source': 'source::hermes'}}
    f = Mock()
    f.list_merged.return_value = [{'iid': iid} for iid in (14, 16, 17, 18)]
    f.get_item.side_effect = lambda kind, iid: record(iid)['mr'] if kind == 'mr' else record()['issue']
    f.branch_head.return_value = 'head'
    f.read_file.side_effect = lambda path, ref: {'content': '{"tasks": []}' if path == 'tasks.json' else '旧记录'}
    f.changes.return_value = {'changes': record()['changes']}
    f.list_notes.return_value = []
    if failure:
        f.commit_files.side_effect = ValueError('分支保护')
    else:
        f.commit_files.return_value = 'new'
    mod.run(cfg, forge=f)
    state = json.loads((tmp_path / 'state.json').read_text()) if (tmp_path / 'state.json').exists() else {}
    assert state.get('registered', []) == ([] if failure else ['mr:14', 'mr:16', 'mr:17', 'mr:18'])
    events = (tmp_path / 'events.md').read_text()
    assert ('登记失败：分支保护' if failure else '登记成功') in events
    assert f.commit_files.call_count == 1
    if not failure:
        assert f.commit_files.call_args.args[-1] == 'head'
        mod.run(cfg, forge=f)
        assert f.commit_files.call_count == 1


def test_dry_run_prints_plan_without_state_events_or_writes(tmp_path):
    mod = load_register()
    cfg = {'reviewer': {'register': settings(), 'runs_dir': str(tmp_path / 'absent'), 'tests': {'full_command': '全量测试'}}}
    f = Mock()
    f.list_merged.return_value = [{'iid': 14}]
    f.get_item.side_effect = lambda kind, iid: record()['mr'] if kind == 'mr' else record()['issue']
    f.branch_head.return_value = 'head'
    f.read_file.side_effect = lambda path, ref: {'content': '{"tasks": []}' if path == 'tasks.json' else '旧记录'}
    f.changes.return_value = {'changes': []}
    f.list_notes.return_value = []
    with patch('sys.stdout', new_callable=io.StringIO) as output:
        mod.run(cfg, dry_run=True, forge=f)
    assert 'hermes-41-queue-14' in output.getvalue()
    assert 'docs(progress): 登记执行方合入的 1 项修复（!14）' in output.getvalue()
    assert '# ' in output.getvalue()
    f.commit_files.assert_not_called()
    assert not (tmp_path / 'absent').exists()


def test_none_format_records_events_without_repository_writes(tmp_path):
    mod = load_register()
    cfg = {'reviewer': {'register': dict(settings(), format='none'), 'runs_dir': str(tmp_path)}}
    f = Mock()
    f.list_merged.return_value = [{'iid': 14, 'title': '修复'}]
    mod.run(cfg, forge=f)
    f.commit_files.assert_not_called()
    f.read_file.assert_not_called()
    assert json.loads((tmp_path / 'state.json').read_text())['registered'] == ['mr:14']
    assert '!14' in (tmp_path / 'events.md').read_text()


def test_register_first_run_keeps_poll_state_schema(tmp_path):
    mod = load_register()
    state = mod.State(tmp_path)
    state.mark(['mr:14'])
    data = state.read()
    assert data['items'] == {} and data['runs'] == [] and data['merged_seen'] == []


def test_existing_task_id_recovers_state_without_second_commit(tmp_path):
    mod = load_register()
    cfg = {'reviewer': {'register': settings(), 'runs_dir': str(tmp_path), 'tests': {'full_command': '全量测试'}}}
    f = Mock()
    f.list_merged.return_value = [{'iid': 14}]
    f.get_item.return_value = record()['mr']
    f.branch_head.return_value = 'head'
    f.read_file.side_effect = lambda path, ref: {'content': '{"tasks": [{"id": "hermes-41-queue-14"}]}' if path == 'tasks.json' else '旧记录'}
    mod.run(cfg, forge=f)
    f.commit_files.assert_not_called()
    f.changes.assert_not_called()
    assert json.loads((tmp_path / 'state.json').read_text())['registered'] == ['mr:14']


def test_approval_evidence_uses_only_human_notes_before_merge():
    mod = load_register()
    approval = {'author': {'username': 'member'}, 'created_at': '2026-10-29T10:00:00Z', 'body': 'lgtm'}
    late = dict(approval, body='hold', created_at='2026-10-29T12:00:00Z')
    assert mod.approving_member([late, approval], {'member'}, '2026-10-29T11:00:00Z') == 'member'
    assert mod.approving_member([late, approval], {'member'}) is None
    assert mod.approving_member([approval], {'other'}) is None


def test_changelog_prepends_date_appends_same_day_and_deduplicates():
    mod = load_register()
    opts = {'format': 'changelog-md', 'file': 'docs/changes.md'}
    old = '# Changelog\n\n## 2026-10-28\n\n- !9 旧修复\n'
    first = mod.build_changelog(old, [record()], opts, '2026-10-29')
    text = first['files']['docs/changes.md']
    assert text.index('## 2026-10-29') < text.index('## 2026-10-28')
    assert text.count('- !14 ') == 1
    second = mod.build_changelog(text, [record(), record(16)], opts, '2026-10-29')
    updated = second['files']['docs/changes.md']
    assert updated.count('## 2026-10-29') == 1
    assert updated.count('- !14 ') == 1 and updated.count('- !16 ') == 1
    assert updated.endswith('## 2026-10-28\n\n- !9 旧修复\n')
    assert mod.build_changelog(updated, [record()], opts, '2026-10-30')['files'] == {}


def hook_config(tmp_path, body, **settings_overrides):
    installed = tmp_path / 'installed'
    hooks = installed / 'hooks'
    hooks.mkdir(parents=True)
    script = hooks / 'register.py'
    script.write_text('#!/usr/bin/env python3\n' + body)
    script.chmod(0o755)
    opts = {'enabled': True, 'format': 'hook', 'hook': 'register.py', 'paths': ['docs/log.md'], **settings_overrides}
    return {'workflow': {'register': opts}, 'reviewer': {'register': opts, 'install_dir': str(installed), 'runs_dir': str(tmp_path / 'runs')}}


def test_hook_receives_pending_mrs_and_commits_only_explicit_paths(tmp_path, monkeypatch):
    monkeypatch.delenv('CLAUDECODE', raising=False)
    cfg = hook_config(tmp_path, "import json, sys\nmrs=json.load(sys.stdin)\nprint(json.dumps({'docs/log.md': str(mrs[0]['iid'])}))\n")
    f = Mock()
    f.list_merged.return_value = [record()['mr']]
    f.branch_head.return_value = 'head'
    f.commit_files.return_value = 'new'
    mod = load_register()
    assert mod.run(cfg, forge=f) is True
    assert f.commit_files.call_args.args[2] == {'docs/log.md': '14'}
    assert f.commit_files.call_args.args[3] == 'head'
    mod.run(cfg, forge=f)
    assert f.commit_files.call_count == 1


@pytest.mark.parametrize('output', ['{"code.py":"bad"}', '{"../docs/log.md":"bad"}', '{"docs/log.md":42}', '[]', 'invalid'])
def test_hook_rejects_invalid_or_unconfigured_outputs_without_marking(tmp_path, output):
    cfg = hook_config(tmp_path, 'print(' + repr(output) + ')\n')
    f = Mock()
    f.list_merged.return_value = [record()['mr']]
    f.branch_head.return_value = 'head'
    assert load_register().run(cfg, forge=f) is False
    f.commit_files.assert_not_called()
    assert not (tmp_path / 'runs/state.json').exists()


@pytest.mark.parametrize('hook', ['../outside.py', '/tmp/outside.py', 'escape.py'])
def test_hook_cannot_escape_installed_hooks_directory(tmp_path, hook):
    cfg = hook_config(tmp_path, 'print("{}")\n', hook=hook)
    outside = tmp_path / 'outside.py'
    outside.write_text('raise RuntimeError("should not execute")')
    (tmp_path / 'installed/hooks/escape.py').symlink_to(outside)
    f = Mock()
    f.list_merged.return_value = [record()['mr']]
    assert load_register().run(cfg, forge=f) is False
    f.commit_files.assert_not_called()


def test_configured_approval_phrases_match_historical_evidence():
    mod = load_register()
    opts = {'approve_phrases': ['可以发布', 'ship.it'], 'revoke_phrases': ['等等']}
    def note(body):
        return {'body': body, 'author': {'username': 'member'}}
    assert mod.approving_member([note('可以发布！')], {'member'}, settings=opts) == 'member'
    assert mod.approving_member([note('等等'), note('可以发布')], {'member'}, settings=opts) is None
    assert mod.approving_member([note('shipXit')], {'member'}, settings=opts) is None
    assert mod.approving_member([note('lgtm')], {'member'}, settings={'approve_phrases': [], 'revoke_phrases': []}) == 'member'


@pytest.mark.parametrize('path_key', ['file', 'changelog_file'])
def test_changelog_run_recovers_existing_mr_and_supports_config_path_alias(tmp_path, path_key):
    opts = {'enabled': True, 'format': 'changelog-md', path_key: 'docs/news.md'}
    cfg = {'workflow': {'register': opts}, 'reviewer': {'runs_dir': str(tmp_path)}}
    f = Mock()
    f.list_merged.return_value = [record()['mr']]
    f.branch_head.return_value = 'head'
    f.read_file.return_value = {'content': '# Changelog\n\n## 2026-10-28\n\n- !14 已登记\n'}
    assert load_register().run(cfg, forge=f) is True
    f.read_file.assert_called_once_with('docs/news.md', 'head')
    f.commit_files.assert_not_called()
    assert json.loads((tmp_path / 'state.json').read_text())['registered'] == ['mr:14']


@pytest.mark.parametrize('fmt', ['changelog-md', 'hook'])
def test_new_formats_dry_run_prints_plan_without_state_or_commit(tmp_path, fmt):
    if fmt == 'hook':
        cfg = hook_config(tmp_path, 'print(\'{"docs/log.md":"待写入"}\')\n')
    else:
        cfg = {'workflow': {'register': {'enabled': True, 'format': fmt}}, 'reviewer': {'runs_dir': str(tmp_path / 'runs')}}
    f = Mock()
    f.list_merged.return_value = [record()['mr']]
    f.branch_head.return_value = 'head'
    f.read_file.return_value = {'content': ''}
    with patch('sys.stdout', new_callable=io.StringIO) as output:
        load_register().run(cfg, dry_run=True, forge=f)
    assert '14' in output.getvalue()
    f.commit_files.assert_not_called()
    assert not (tmp_path / 'runs').exists()


def test_hook_files_alias_is_supported(tmp_path, monkeypatch):
    monkeypatch.delenv('CLAUDECODE', raising=False)
    cfg = hook_config(tmp_path, 'print(\'{"docs/log.md":"记录"}\')\n', paths=[], files=['docs/log.md'])
    f = Mock()
    f.list_merged.return_value = [record()['mr']]
    f.branch_head.return_value = 'head'
    f.commit_files.return_value = 'new'
    assert load_register().run(cfg, forge=f) is True
    assert f.commit_files.call_args.args[2] == {'docs/log.md': '记录'}


@pytest.mark.parametrize('extra', [
    {'file': '../changes.md'}, {'changelog_file': '/changes.md'}, {'file': 'docs//changes.md'},
    {'paths': 'docs/log.md'}, {'files': ['../log.md']}, {'hook': '../run.py'},
    {'format': 'hook', 'hook': 'run.py', 'paths': []},
    {'format': 'hook', 'paths': ['docs/log.md']},
])
def test_workflow_rejects_unsafe_registration_configuration(extra):
    from kit.config import load_config
    from kit.workflow import normalize
    cfg = load_config(ROOT / 'examples/projects/example-gitlab/project.toml')
    cfg['workflow']['register'].update(extra)
    with pytest.raises(ValueError, match='workflow.register'):
        normalize(cfg, {})
