#!/usr/bin/env python3
"""通过 SSH 安装执行端；预览模式不建立远端连接。"""
import argparse
import base64
from datetime import datetime
import inspect
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def yaml_sections(text):
    sections = []
    current = None
    for line in text.splitlines(True):
        match = re.match(r'''^(?:([A-Za-z_][\w-]*)|"([^"\\]+)"|'([^']+)'):(?:[ \t]|$)''', line.rstrip('\n'))
        if match:
            current = [next(value for value in match.groups() if value is not None), line]
            sections.append(current)
        elif line.strip() and not line[0].isspace() and not line.startswith('#'):
            raise ValueError('配置包含无法解析的顶层 YAML，请人工核实')
        elif current is None:
            sections.append([None, line])
        else:
            current[1] += line
    return sections


def merge_agent(content, replacement):
    """保留平面 agent 映射的缩进；重复键和复杂 YAML 交由人工核实。"""
    rows = content.splitlines(True)
    if not rows or not re.fullmatch(r'agent:[ \t]*(?:#.*)?\n?', rows[0]):
        raise ValueError('agent 需要使用多行 YAML 映射')
    indentation = None
    fields = set()
    found = None
    for index, line in enumerate(rows[1:], 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'( +)([A-Za-z_][\w-]*):[ \t]*(.*?)\n?', line)
        if not match:
            raise ValueError('agent 包含复杂 YAML，请人工核实后使用平面标量映射')
        spaces, key, value = match.groups()
        indentation = indentation or spaces
        if spaces != indentation or key in fields:
            raise ValueError('agent 存在重复字段或不一致的缩进，请人工核实')
        fields.add(key)
        if not value or value[0] in '&*!|>[{' or re.search(r':\s', value):
            raise ValueError('agent 包含复杂 YAML，请人工核实后使用平面标量映射')
        if value[0] == '"' and not re.fullmatch(r'"(?:[^"\\]|\\.)*"(?:\s+#.*)?', value):
            raise ValueError('agent 包含不支持的 YAML 字符串')
        if value[0] == "'" and not re.fullmatch(r"'(?:[^']|'')*'(?:\s+#.*)?", value):
            raise ValueError('agent 包含不支持的 YAML 字符串')
        if key == 'max_turns':
            found = index
    indentation = indentation or '  '
    row = indentation + 'max_turns: ' + replacement + '\n'
    if found is None:
        return content.rstrip('\n') + '\n' + row
    rows[found] = row
    return ''.join(rows)


def merge_config(original, fragment):
    """保留原 YAML 文本，仅替换三个约定键，拒绝未支持的映射形式。"""
    parts = yaml_sections(fragment)
    patch = {}
    for key, content in parts:
        if key is None:
            if content.strip() and not content.lstrip().startswith('#'):
                raise ValueError('配置片段包含无法识别的 YAML')
            continue
        if key not in ('approvals', 'delegation', 'agent') or key in patch:
            raise ValueError('配置片段只能包含唯一的 approvals、delegation、agent')
        patch[key] = content
    agent_patch = patch.get('agent', '')
    if agent_patch:
        if not re.fullmatch(r'agent:\s*\n(?:\s*#.*\n|\s*\n)*  max_turns: [1-9][0-9]*\s*\n?', agent_patch):
            raise ValueError('agent 配置片段只能修改 max_turns')
    result = []
    seen = set()
    for key, content in yaml_sections(original):
        if key not in patch:
            result.append(content)
            continue
        if key in seen:
            raise ValueError('原配置有重复受管键，请先由用户核实')
        seen.add(key)
        if key != 'agent':
            result.append(patch[key])
            continue
        value = re.search(r'^  max_turns: (.*)$', agent_patch, re.M).group(1)
        content = merge_agent(content, value)
        result.append(content)
    for key, content in patch.items():
        if key not in seen:
            result.append(content)
    return ''.join(piece if piece.endswith('\n') else piece + '\n' for piece in result)


def file_destinations(source, conf):
    """生成预览与正式安装共用的完整目标清单，不展开远端用户目录。"""
    destinations = []
    home = Path(conf['hermes_home'])
    review = Path(conf['review_dir'])
    for path in source.rglob('*'):
        if path.is_file():
            destinations.append((path, review / path.relative_to(source)))
    for path in (source / 'scripts').rglob('*'):
        if path.is_file():
            destinations.append((path, home / 'scripts' / path.relative_to(source / 'scripts')))
    for name in ('project.toml',):
        if (source / name).exists():
            destinations.append((source / name, home / 'scripts' / name))
    if (source / 'kit').is_dir():
        for path in (source / 'kit').rglob('*'):
            if path.is_file():
                destinations.append((path, home / 'scripts' / 'kit' / path.relative_to(source / 'kit')))
    for source_name, dest in [('rules.md', conf.get('rules_file')), ('SKILL.md', str(Path(conf['skill_dir']) / 'SKILL.md') if conf.get('skill_dir') else None), ('dispatcher.md', str(home / 'cron' / conf.get('cron', {}).get('dispatcher_filename', 'dispatcher.md')))]:
        if dest and (source / source_name).exists():
            destinations.append((source / source_name, Path(dest)))
    if conf.get('ref_dir'):
        references = source / 'claude-ref'
        if references.is_dir():
            for path in references.rglob('*'):
                if path.is_file():
                    destinations.append((path, Path(conf['ref_dir']) / path.relative_to(references)))
        for name in ('build-image.sh', conf.get('deploy', {}).get('certificate_name', 'registry.crt')):
            if (source / name).exists():
                destinations.append((source / name, Path(conf['ref_dir']) / name))
    return [(src, dest) for dest, src in {dst: src for src, dst in destinations}.items()]


def remote_install(payload):
    """此函数连同有限 YAML 合并函数通过 stdin 在执行端运行。"""
    import base64
    from datetime import datetime
    import difflib
    import io
    import json
    from pathlib import Path
    import shutil
    import subprocess
    import tarfile
    import tempfile
    import tomllib
    conf = payload['config']
    home = Path(conf['hermes_home']).expanduser()
    review = Path(conf['review_dir']).expanduser()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = home / ('agent-review-loop-backup-' + stamp)
    # 在写入前确认 CLI 能力；不支持的版本直接退出。
    create_help = subprocess.run(['hermes', 'cron', 'create', '--help'], capture_output=True, text=True, check=True).stdout
    edit_help = subprocess.run(['hermes', 'cron', 'edit', '--help'], capture_output=True, text=True, check=True).stdout
    list_help = subprocess.run(['hermes', 'cron', 'list', '--help'], capture_output=True, text=True, check=True).stdout
    if '--json' not in list_help:
        raise RuntimeError('hermes cron list 缺少 --json，当前 CLI 版本需人工核实')
    listing = json.loads(subprocess.run(['hermes', 'cron', 'list', '--json'], capture_output=True, text=True, check=True).stdout)
    existing = listing.get('jobs', listing) if isinstance(listing, dict) else listing
    if not isinstance(existing, list):
        raise RuntimeError('hermes cron list 的 JSON 格式无法识别')
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='agent-review-loop-', dir=home) as staging:
        staging = Path(staging)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload['archive'])), mode='r:gz') as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk() or not (staging / member.name).resolve().is_relative_to(staging.resolve()):
                    raise ValueError('安装包包含不安全路径')
            if hasattr(tarfile, 'data_filter'):
                archive.extractall(staging, filter='data')
            else:
                archive.extractall(staging)
        installed_project = home / 'scripts' / 'project.toml'
        if installed_project.exists():
            incoming_project = staging / 'project.toml'
            if not incoming_project.is_file():
                raise ValueError('安装包缺少项目标识，无法核实现有 Hermes home 的项目')
            installed_name = tomllib.loads(installed_project.read_text()).get('project', {}).get('name')
            incoming_name = tomllib.loads(incoming_project.read_text()).get('project', {}).get('name')
            if not installed_name or not incoming_name or installed_name != incoming_name:
                raise ValueError('Hermes home 已安装其他项目，请使用独立 profile 或 home')
        data = json.loads((staging / 'cron-jobs.json').read_text())
        jobs = data['jobs'] if isinstance(data, dict) else data
        commands = []
        for job in jobs:
            matches = [row for row in existing if row.get('name') == job['name']]
            if len(matches) > 1:
                raise RuntimeError('发现同名 hermes 定时任务，请人工核实')
            operation = 'edit' if matches else 'create'
            cmd = ['hermes', 'cron', operation]
            if matches:
                cmd.append(str(matches[0].get('id', matches[0].get('job_id'))))
            options = {'name': job['name'], 'schedule': job['schedule']}
            for key in ('deliver', 'workdir'):
                if job.get(key):
                    options[key] = job[key]
            for key, flag in (('prompt_file', 'prompt-file'), ('monitor_script', 'monitor'), ('script', 'script')):
                if job.get(key):
                    options[flag] = str(review / job[key])
            help_text = edit_help if matches else create_help
            for option, value in options.items():
                if '--' + option not in help_text:
                    raise RuntimeError('hermes cron 不支持参数 --' + option + '，请核实 CLI 版本')
                cmd.extend(['--' + option, str(value)])
            if job.get('no_agent'):
                if '--no-agent' not in help_text:
                    raise RuntimeError('hermes cron 不支持 --no-agent，请核实 CLI 版本')
                cmd.append('--no-agent')
            commands.append(cmd)
        config_file = home / 'config.yaml'
        old = config_file.read_text() if config_file.exists() else ''
        merged = merge_config(old, (staging / 'hermes-config-patch.yaml').read_text())
        print(''.join(difflib.unified_diff(old.splitlines(True), merged.splitlines(True), fromfile='config.yaml', tofile='合并结果', n=0)))
        backup.mkdir(parents=True)
        if config_file.exists():
            shutil.copy2(config_file, backup / 'config.yaml')
        (backup / 'cron-jobs-before.json').write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        destinations = [(src, dest.expanduser()) for src, dest in file_destinations(staging, conf)]
        for index, (source, dest) in enumerate(destinations):
            if dest.exists():
                saved = backup / 'files' / str(index)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, saved)
                with (backup / 'manifest.tsv').open('a') as manifest:
                    manifest.write(str(saved.relative_to(backup)) + '\t' + str(dest) + '\n')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        config_file.write_text(merged)
        for filename in ('findings.md', 'reviewed-files.txt'):
            record = review / filename
            record.parent.mkdir(parents=True, exist_ok=True)
            try:
                with record.open('x'):
                    pass
            except FileExistsError:
                pass
        if conf.get('scratch_dir'):
            Path(conf['scratch_dir']).expanduser().mkdir(parents=True, exist_ok=True)
        for key in ('notify_state_file', 'token_notice_file'):
            if conf.get(key):
                Path(conf[key]).expanduser().parent.mkdir(parents=True, exist_ok=True)
        for cmd in commands:
            subprocess.run(cmd, check=True)
        print('远端备份目录：' + str(backup))


def install(config, build_dir, dry_run=False):
    if config.get('project', {}).get('language', 'python') != 'python':
        print('提示：complexity.py 仅支持 Python，请核对本项目的复杂度验证方法。')
    source = Path(build_dir) / 'fixer'
    if not source.is_dir():
        raise ValueError('找不到渲染目录：' + str(source))
    conf = config['fixer']
    host = conf.get('ssh_host') or conf['host']
    if host.startswith('-') or any(char.isspace() for char in host):
        raise ValueError('fixer.host 必须是 SSH 主机名或别名')
    print('远端命令：' + shlex.join(['ssh', host, 'python3 -']))
    print('复制目录：' + str(source) + ' → ' + host + ':' + conf['review_dir'])
    print('逐文件目标清单：远端内容未知，以下 sha256 来自本地生成文件；正式连接后才能比较现有文件。')
    for path, destination in file_destinations(source, conf):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print('文件：' + str(path) + ' → ' + host + ':' + str(destination) + ' sha256=' + digest + '；差异：远端内容未知')
    print('配置差异：正式安装时读取远端 config.yaml，只修改 approvals、delegation、agent.max_turns，写入前备份。')
    print('配置片段：\n' + (source / 'hermes-config-patch.yaml').read_text())
    jobs = json.loads((source / 'cron-jobs.json').read_text())
    print('定时任务计划：' + json.dumps(jobs, ensure_ascii=False, indent=2))
    print('命令计划：hermes cron create/edit（按名称匹配），正式安装时先检查 --help 和 cron list --json。')
    print('项目检查：正式安装将读取远端 hermes_home/scripts/project.toml；已安装其他项目时拒绝覆盖，预览不进行 SSH。')
    print('状态初始化：仅在缺失时建立 findings.md、reviewed-files.txt，建立已配置的临时目录和通知状态父目录，保留已有记录。')
    if dry_run:
        print('预览模式不执行 ssh；远端文件差异需在正式安装时读取。')
        return
    archive_data = io.BytesIO()
    with tarfile.open(fileobj=archive_data, mode='w:gz') as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(source)), recursive=False)
    payload = {'config': conf, 'archive': base64.b64encode(archive_data.getvalue()).decode()}
    script = 'import re\nfrom pathlib import Path\n' + inspect.getsource(file_destinations) + '\n' + inspect.getsource(merge_agent) + '\n' + inspect.getsource(yaml_sections) + '\n' + inspect.getsource(merge_config) + '\n' + inspect.getsource(remote_install)
    script += '\nremote_install(' + repr(payload) + ')\n'
    subprocess.run(['ssh', host, 'python3 -'], input=script, text=True, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', nargs='?')
    parser.add_argument('build_dir', nargs='?')
    parser.add_argument('--config', dest='config_option')
    parser.add_argument('--build-dir', dest='build_option')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    from kit.config import load_config
    config_path = args.config_option or args.config
    if not config_path:
        parser.error('必须提供项目参数文件')
    config = load_config(config_path)
    install(config, args.build_option or args.build_dir or 'build/' + config['project']['name'], args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('安装失败：' + str(exc), file=sys.stderr)
        sys.exit(1)
