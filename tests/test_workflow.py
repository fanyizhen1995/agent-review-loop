"""验证项目工作流的统一配置与渲染行为。"""
import importlib.util
import subprocess
import shutil
from pathlib import Path
import pytest
from kit.config import load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/projects/example-gitlab'


def renderer():
    spec = importlib.util.spec_from_file_location('workflow_renderer', ROOT / 'install/render.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_directory_config_and_legacy_mapping():
    cfg = load_config(EXAMPLE)
    assert cfg['workflow']['merge'] == cfg['reviewer']['merge']
    assert cfg['fixer']['adapter'] == 'hermes'


def test_workflow_drives_all_three_documents_and_reviewer(tmp_path):
    project = tmp_path / 'project'
    shutil.copytree(EXAMPLE, project)
    config = project / 'project.toml'
    text = config.read_text().replace('python3 -m pytest -q tests', 'echo 工作流验证成功')
    text = text.replace('reviewer_run = \'python3 -m pytest -q -p no:cacheprovider "${args[@]:-tests}"\'', 'reviewer_run = \'echo 工作流验证成功\'')
    config.write_text(text)
    (project / 'standards-overlay.md').write_text('项目专属评审补充')
    output = tmp_path / 'output'
    renderer().render_project(project, output)
    for name in ('fixer/rules.md', 'fixer/dispatcher.md', 'reviewer/standards.md'):
        assert 'echo 工作流验证成功' in (output / name).read_text()
        assert 'deploy-smoke' in (output / name).read_text()
    assert '项目专属评审补充' in (output / 'reviewer/standards.md').read_text()
    assert (output / 'fixer/hooks/smoke.sh').exists()
    result = subprocess.run(['bash', str(output / 'reviewer/runtest.sh'), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0
    assert '工作流验证成功' in result.stdout


def test_rejects_unknown_language_and_verify_kind(tmp_path):
    config = tmp_path / 'project.toml'
    text = (EXAMPLE / 'project.toml').read_text()
    config.write_text(text.replace('language = "zh"', 'language = "en"'))
    with pytest.raises(ValueError, match='zh'):
        load_config(config)
    config.write_text(text.replace('builtin = "regression"', 'builtin = "unknown"'))
    with pytest.raises(ValueError, match='builtin'):
        load_config(config)


def test_file_input_has_no_overlay_or_hooks(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / 'project')
    output = tmp_path / 'output'
    renderer().render_project(tmp_path / 'project/project.toml', output)
    assert not (output / 'fixer/hooks').exists()
    assert '示例项目执行补充' not in (output / 'fixer/rules.md').read_text()


def test_optional_empty_hook_and_disabled_metrics(tmp_path):
    project = tmp_path / 'project'
    shutil.copytree(EXAMPLE, project)
    config = project / 'project.toml'
    text = config.read_text().replace('format = "none"', 'format = "none"\nhook = ""')
    text = text.replace('complexity_cmd = "python3 {kit}/complexity.py {file}"', 'complexity_cmd = ""').replace('test_ratio_limit = 2.0', 'test_ratio_limit = 0')
    config.write_text(text)
    output = tmp_path / 'output'
    renderer().render_project(project, output)
    assert '本项目未要求复杂度数值' in (output / 'fixer/rules.md').read_text()
    assert '不超过源码新增行数的 2 倍' not in (output / 'reviewer/standards.md').read_text()


@pytest.mark.parametrize('before,after', [('run = "python3 -m pytest -q tests"', 'run = 123'), ('test_ratio_limit = 2.0', 'test_ratio_limit = nan')])
def test_invalid_workflow_reports_value_error(tmp_path, before, after):
    config = tmp_path / 'project.toml'
    config.write_text((EXAMPLE / 'project.toml').read_text().replace(before, after))
    with pytest.raises(ValueError, match='workflow'):
        load_config(config)


def test_reviewer_conditions_hooks_and_placeholders(tmp_path):
    project = tmp_path / 'project'
    shutil.copytree(EXAMPLE, project)
    config = project / 'project.toml'
    text = config.read_text()
    start = text.index('[[workflow.verify]]')
    text = text[:start] + '''[[workflow.verify]]
name = "local"
run = "hooks/check.sh {branch} {sha}"
for_types = ["bug"]
when_paths = ["src/**"]
lock = "test"
rerun_by_reviewer = true
'''
    text = text.replace('/home/agent/example-gitlab-remote/.claude/runs/hermes-review', str(tmp_path / 'runs'))
    config.write_text(text)
    (project / 'hooks/check.sh').write_text('#!/bin/bash\nset -eu\nprintf "版本：%s %s\\n" "$1" "$2"\n')
    (project / 'hooks/check.sh').chmod(0o755)
    output = tmp_path / 'output'
    renderer().render_project(project, output)
    import os
    env = dict(os.environ, ARL_ISSUE_TYPE='perf', ARL_CHANGED_PATHS='src/a.py', ARL_BRANCH='fix/a', ARL_SHA='abc')
    command = ['bash', str(output / 'reviewer/runtest.sh'), str(tmp_path)]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and '版本' not in result.stdout
    env['ARL_ISSUE_TYPE'] = 'bug'
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    expected_sha = subprocess.run(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip() or 'abc'
    assert '版本：fix/a ' + expected_sha in result.stdout


def test_targeted_reviewer_test_is_really_executed(tmp_path):
    project = tmp_path / 'project'
    shutil.copytree(EXAMPLE, project)
    # 使用真实失败脚本证明指定目标的退出状态不会被全量命令掩盖。
    config = project / 'project.toml'
    text = config.read_text().replace('run = "python3 -m pytest -q tests"', 'run = "true"').replace('reviewer_run = \'python3 -m pytest -q -p no:cacheprovider "${args[@]:-tests}"\'', 'reviewer_run = \'bash "${args[@]}"\'')
    config.write_text(text)
    target = tmp_path / 'regression.sh'
    target.write_text('exit 17\n')
    output = tmp_path / 'output'
    renderer().render_project(project, output)
    result = subprocess.run(['bash', str(output / 'reviewer/runtest.sh'), str(tmp_path), str(target)], capture_output=True, text=True)
    assert result.returncode != 0


def test_example_reviewer_honors_targeted_regression_path(tmp_path):
    output = tmp_path / 'output'
    renderer().render_project(EXAMPLE, output)
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_suite.py').write_text('def test_suite():\n    assert True\n')
    target = tmp_path / 'test_regression.py'
    target.write_text('def test_regression():\n    assert False\n')
    result = subprocess.run(['bash', str(output / 'reviewer/runtest.sh'), str(tmp_path), str(target)], capture_output=True, text=True, env=__import__('os').environ | {'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_ADDOPTS': '-p no:cacheprovider'})
    assert result.returncode != 0, '入口必须执行指定的失败回归测试'
    assert 'test_regression' in result.stdout


def test_resource_root_symlink_is_rejected(tmp_path):
    project = tmp_path / 'project'
    shutil.copytree(EXAMPLE, project)
    shutil.rmtree(project / 'hooks')
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'secret.txt').write_text('不得拷贝')
    (project / 'hooks').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        renderer().render_project(project, tmp_path / 'output')


def test_refactor_only_legacy_defaults_skip_regression(tmp_path):
    text = (EXAMPLE / 'project.toml').read_text()
    start = text.index('[[workflow.verify]]')
    text = text[:start].replace('["bug", "perf", "robust", "refactor"]', '["refactor"]')
    config = tmp_path / 'project.toml'
    config.write_text(text)
    cfg = load_config(config)
    assert not any(s.get('builtin') == 'regression' for s in cfg['workflow']['verify'])


def test_placeholder_branch_is_data_in_all_quote_styles(tmp_path):
    spec = importlib.util.spec_from_file_location('verify_runner', ROOT / 'reviewer/verify_workflow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, 'substitute_placeholders')
    import os
    value = 'fix/$(printf${IFS}INJECTED)'
    for command in ('printf %s {branch}', 'printf %s "{branch}"', "printf %s '{branch}'"):
        result = subprocess.run(['bash', '-c', module.substitute_placeholders(command)], env=dict(os.environ, ARL_BRANCH=value), capture_output=True, text=True)
        assert result.stdout == value


def test_only_enabled_types_generate_special_requirements(tmp_path):
    config = tmp_path / 'project.toml'
    text = (EXAMPLE / 'project.toml').read_text().replace('issue_types = ["bug", "perf", "robust", "refactor"]', 'issue_types = ["bug"]').replace('for_types = ["bug", "robust"]', 'for_types = ["bug"]')
    config.write_text(text)
    output = tmp_path / 'output'
    renderer().render_project(config, output)
    assert '## 4. 重构要求' not in (output / 'fixer/rules.md').read_text()
    assert '6. refactor 类' not in (output / 'reviewer/standards.md').read_text()
    assert '5. perf 类' not in (output / 'reviewer/standards.md').read_text()
    assert '分支名 fix/<issue号>' in (output / 'fixer/dispatcher.md').read_text()
