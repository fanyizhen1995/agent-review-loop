#!/usr/bin/env python3
"""从项目目录或兼容 TOML 文件生成评审端与执行端文件。"""
import argparse
import copy
import importlib.util
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kit.config import load_config
from kit.render import render_text


def context_for(config):
    context = copy.deepcopy(config)
    context['forge'].setdefault('display_name', {'gitlab': 'GitLab', 'github': 'GitHub'}[config['forge']['kind']])
    reviewer = context['reviewer']
    reviewer.setdefault('relative_install_dir', os.path.relpath(reviewer['install_dir'], reviewer['repo']))
    reviewer.setdefault('token_dir', str(Path(reviewer.get('token_file') or context['credentials']['reviewer'].get('file', reviewer['repo'])).parent))
    module_spec = importlib.util.spec_from_file_location('approval_deny', ROOT / 'fixer/adapters/hermes/approval_deny.py')
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    commands = context['project']['test_commands']
    node_command = context['reviewer']['tests']['node_command']
    credential = context['credentials']['fixer']
    if credential.get('file'):
        credential_description = '`' + credential['file'] + '` 文件'
    elif credential.get('dotenv_file'):
        credential_description = '`' + credential['dotenv_file'] + '` 的 `' + credential['env'] + '`'
    else:
        credential_description = '`' + credential['env'] + '` 环境变量'
    def interval(value):
        match = re.fullmatch(r'(\d+)([mh])', value)
        if not match:
            return value
        number, unit = int(match[1]), match[2]
        if unit == 'm' and number % 60 == 0:
            return str(number // 60) + ' 小时'
        return str(number) + (' 分钟' if unit == 'm' else ' 小时')
    context['_generated'] = {
        'dispatcher_interval_display': interval(context['fixer']['cron']['dispatcher_schedule']),
        'cleanup_interval_display': interval(context['fixer']['cron']['cleanup_schedule']),
        'fixer_credential_description': credential_description,
        'forge_display': context['forge']['display_name'],
        'hot_files_plain': '、'.join(context['project']['hot_files']),
        'forbidden_files_markdown': '、'.join('`' + name + '`' for name in context['project']['forbidden_files']),
        'forbidden_files_plain': ' 和 '.join(context['project']['forbidden_files']),
        'work_branch_types': '|'.join(p.rstrip('/') for p in context['branches']['work_prefixes'] if {'fix': 'bug'}.get(p.rstrip('/'), p.rstrip('/')) in config['workflow']['issue_types']),
        'source_example': context['project']['source_dirs'][0] + '/文件' + context['project']['source_extensions'][0],
        'forbidden_namespaces_markdown': '、'.join('`' + name + '`' for name in context['fixer']['deploy']['forbidden_namespaces']),
        'forbidden_branches_markdown': '、'.join('`' + name + '`' for name in context['branches']['protected'] + [p + '*' for p in context['branches']['forbidden_prefixes']]),
        'test_command': commands[0] if commands else '',
        'node_test_command': node_command.split(' && ', 1)[-1],
        'deploy_lock_basename': Path(context['fixer']['deploy']['lock_file']).name,
        'approval_deny_json': json.dumps(module.approval_deny(context), ensure_ascii=False),
        'worktree_isolation_toml': str(context['fixer']['delegation']['worktree_isolation']).lower(),
    }
    from kit.workflow import requirements
    context['_generated']['verify_requirements'] = requirements(config)
    context['_generated']['approve_phrases'] = '、'.join(config['workflow']['merge']['approve_phrases']) or '默认同意短语（lgtm/同意合入等）'
    context['_generated']['issue_types'] = ' / '.join(config['workflow']['issue_types'])
    metrics = config['workflow']['metrics']
    context['_generated']['complexity_requirement'] = ('度量：`' + metrics['complexity_cmd'] + '`，给出改动前后的数值。') if metrics['complexity_cmd'] else '本项目未要求复杂度数值。'
    context['_generated']['test_ratio_requirement'] = (f"新增测试行数一般不超过源码新增行数的 {metrics['test_ratio_limit']:g} 倍，超过时说明理由。") if metrics['test_ratio_limit'] else '本项目不限制测试与源码的行数比例。'
    return context


def _render_project(config_path, output):
    project_input = Path(config_path).resolve()
    project_dir = project_input if project_input.is_dir() else None
    config_path = project_input / 'project.toml' if project_dir else project_input
    output = Path(output).resolve()
    if output == ROOT or output in ROOT.parents or any(output == ROOT / name or (ROOT / name) in output.parents for name in ('kit', 'reviewer', 'fixer', 'examples', 'install', 'docs', 'tests', 'hooks')):
        raise ValueError('输出目录不得覆盖套件源码或基准文件')
    config = load_config(config_path)
    context = context_for(config)
    output.mkdir(parents=True, exist_ok=True)
    for role in ('reviewer', 'fixer'):
        source_dir = ROOT / role if role == 'reviewer' else ROOT / 'fixer/adapters' / config['fixer']['adapter']
        target_dir = output / role
        target_dir.mkdir(exist_ok=True)
        for source in sorted(source_dir.rglob('*')):
            if not source.is_file() or '__pycache__' in source.parts or source.name in ('CONFIG-NOTES.md', 'RENDER-NOTES.md', 'KNOWN-NOTES.md', 'approval_deny.py', 'render.py'):
                continue
            relative = source.relative_to(source_dir)
            if role == 'reviewer' and relative.parts[0] == 'prompts':
                relative = Path(relative.name)
            if str(relative).endswith('.tmpl'):
                relative = Path(str(relative)[:-5])
            target = target_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.name.endswith('.tmpl') or role == 'fixer' and relative.suffix == '.toml':
                target.write_text(render_text(source.read_text(), context))
            else:
                shutil.copy2(source, target)
            if target.suffix == '.sh':
                target.chmod(0o755)
        if role == 'fixer':
            (target_dir / 'rules.md').write_text(render_text((ROOT / 'fixer/rules.base.md.tmpl').read_text(), context))
        overlay = 'rules-overlay.md' if role == 'fixer' else 'standards-overlay.md'
        document = target_dir / ('rules.md' if role == 'fixer' else 'standards.md')
        if project_dir and (project_dir / overlay).is_symlink():
            raise ValueError('项目 overlay 不得为符号链接')
        if project_dir and (project_dir / overlay).is_file():
            document.write_text(document.read_text().rstrip() + '\n\n' + render_text((project_dir / overlay).read_text(), context))
        if project_dir:
            for directory in config.get('project_files', {}).get(role, ['hooks', 'assets']):
                if directory not in ('hooks', 'assets'):
                    raise ValueError('project_files 仅支持 hooks/assets 目录')
                resource = project_dir / directory
                if resource.is_symlink():
                    raise ValueError('项目资源目录不得为符号链接')
                if resource.exists():
                    for item in resource.rglob('*'):
                        if item.is_symlink():
                            raise ValueError('项目资源不得包含符号链接')
                        if item.is_file():
                            destination = target_dir / directory / item.relative_to(resource)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            if directory == 'hooks':
                                destination.write_text(render_text(item.read_text(), context))
                                destination.chmod(item.stat().st_mode & 0o777)
                            else:
                                shutil.copy2(item, destination)
        shutil.copy2(config_path, target_dir / 'project.toml')
        shutil.copytree(ROOT / 'kit', target_dir / 'kit', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        if role == 'fixer':
            shutil.copy2(config_path, target_dir / 'scripts/project.toml')
            shutil.copytree(ROOT / 'kit', target_dir / 'scripts/kit', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    refs = output / 'fixer/claude-ref'
    refs.mkdir(exist_ok=True)
    # 写作规则已并入执行方规则文件（rules.md），执行端不再单独安装 writing-rules.md
    for name in ('complexity.py', 'standards.md'):
        shutil.copy2(output / 'reviewer' / name, refs / name)
    shutil.copy2(ROOT / 'CHANGELOG.md', refs / 'CHANGELOG.md')
    shutil.copy2(output / 'fixer/rules.md', refs / 'hermes-rules.md')
    # 项目可声明兼容资源位置，默认 hooks/assets 保持独立目录。
    if project_dir:
        for name, relative in config.get('project_files', {}).get('fixer_ref', {}).items():
            source = output / 'fixer' / relative
            target = refs / name
            if not source.resolve().is_relative_to((output / 'fixer').resolve()) or not target.resolve().is_relative_to(refs.resolve()):
                raise ValueError('兼容资源位置必须在受管目录内')
            if not source.is_file():
                source = project_dir / relative
                if source.is_symlink() or not source.resolve().is_relative_to(project_dir.resolve()):
                    raise ValueError('项目兼容资源必须为目录内的普通文件')
            if not source.is_file():
                raise ValueError('找不到声明的项目兼容资源：' + relative)
            shutil.copy2(source, target)
    adapter_spec = importlib.util.spec_from_file_location('hermes_render', ROOT / 'fixer/adapters/hermes/render.py')
    adapter = importlib.util.module_from_spec(adapter_spec)
    adapter_spec.loader.exec_module(adapter)
    (output / 'fixer/hermes-config-patch.yaml').write_text(adapter._yaml_patch(output, config))
    (output / 'fixer/cron-jobs.json').write_text(json.dumps(adapter._cron_jobs(config), ensure_ascii=False, indent=2) + '\n')
    reviewer = config['reviewer']
    cron = reviewer['cron']
    line = cron['schedule'] + ' cd ' + shlex.quote(reviewer['repo']) + ' && PATH=' + shlex.quote(cron['path']) + ' ' + shlex.quote(cron['python']) + ' ' + shlex.quote(os.path.relpath(reviewer['install_dir'], reviewer['repo']) + '/poll.py') + ' >> ' + shlex.quote(os.path.relpath(reviewer['runs_dir'], reviewer['repo']) + '/poll.log') + ' 2>&1 # agent-review-loop:' + config['project']['name'] + '\n'
    register = reviewer['register']
    if register['enabled']:
        line += register['cron'] + ' cd ' + shlex.quote(reviewer['repo']) + ' && PATH=' + shlex.quote(cron['path']) + ' ' + shlex.quote(cron['python']) + ' ' + shlex.quote(os.path.relpath(reviewer['install_dir'], reviewer['repo']) + '/register.py') + ' >> ' + shlex.quote(os.path.relpath(reviewer['runs_dir'], reviewer['repo']) + '/register.log') + ' 2>&1 # agent-review-loop:' + config['project']['name'] + ':register\n'
    (output / 'reviewer/crontab.line').write_text(line)
    if config['project'].get('language') != 'python':
        print('提示：内置 complexity.py 仅支持 Python；其他语言可配置 workflow.metrics.complexity_cmd。')
    return output


def render_project(config_path, output):
    output = Path(output).resolve()
    if output == ROOT or output in ROOT.parents or any(output == ROOT / name or (ROOT / name) in output.parents for name in ('kit', 'reviewer', 'fixer', 'examples', 'install', 'docs', 'tests', 'hooks')):
        raise ValueError('输出目录不得覆盖套件源码或基准文件')
    output.parent.mkdir(parents=True, exist_ok=True)
    # 成功渲染到临时目录后替换受管产物，消除禁用选项的旧文件。
    with tempfile.TemporaryDirectory(prefix='.render-', dir=output.parent) as temporary:
        staged = Path(temporary)
        _render_project(config_path, staged)
        output.mkdir(parents=True, exist_ok=True)
        managed = {'reviewer', 'fixer'}
        for name in managed:
            target = output / name
            if target.is_symlink():
                raise ValueError('受管输出不得为符号链接：' + str(target))
        for name in managed:
            target = output / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if (staged / name).exists():
                (staged / name).rename(target)
    print('渲染完成：' + str(output))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('output')
    args = parser.parse_args()
    render_project(args.config, args.output)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as exc:
        print('渲染失败：' + str(exc), file=sys.stderr)
        sys.exit(1)
