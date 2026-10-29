#!/usr/bin/env python3
"""安装评审端文件；预览模式仅使用本地只读操作。"""
import argparse
from datetime import datetime
import difflib
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def merge_crontab(existing, line, project):
    """同步本项目的轮询和登记标记；保留其他项目与无关定时任务。"""
    owners = {project, project + ':register'}
    new, clean = [], set()
    for index, row in enumerate(line.strip().splitlines()):
        command, separator, owner = row.strip().partition(' # agent-review-loop:')
        if separator and owner not in owners:
            raise ValueError('crontab 标记与项目不匹配')
        if not separator:
            owner = project if index == 0 else project + ':register'
        clean.add(command)
        new.append(command + ' # agent-review-loop:' + owner)
    kept = []
    for row in existing.splitlines():
        command, separator, owner = row.strip().partition(' # agent-review-loop:')
        if separator and owner in owners or not separator and row.strip() in clean:
            continue
        kept.append(row)
    return '\n'.join(kept + new) + '\n'


def preview_file(source, target):
    old = target.read_bytes() if target.is_file() else b''
    new = source.read_bytes()
    if old == new:
        print('一致：' + str(target))
        return
    try:
        print(''.join(difflib.unified_diff(old.decode().splitlines(True), new.decode().splitlines(True), fromfile=str(target), tofile=str(source))), end='')
    except UnicodeDecodeError:
        print('二进制文件变化：' + str(target))


def install(config, build_dir, dry_run=False):
    if config.get('project', {}).get('language', 'python') != 'python':
        print('提示：complexity.py 仅支持 Python，请核对本项目的复杂度验证方法。')
    source = Path(build_dir) / 'reviewer'
    if not source.is_dir():
        raise ValueError('找不到渲染目录：' + str(source))
    settings = config['reviewer']
    target = Path(settings['install_dir']).expanduser()
    refs = Path(settings['ref_dir']).expanduser()
    project = config['project']['name']
    files = [(path, target / path.relative_to(source)) for path in sorted(source.rglob('*')) if path.is_file() and path.name != 'crontab.line']
    if settings.get('skill_dir') and (source / 'SKILL.md').exists():
        files.append((source / 'SKILL.md', Path(settings['skill_dir']).expanduser() / 'SKILL.md'))
    for name in ('standards.md', 'writing-rules.md'):
        if (source / name).exists():
            files.append((source / name, refs / name))
    rules_source = Path(build_dir) / 'fixer' / 'rules.md'
    if rules_source.exists():
        files.append((rules_source, refs / 'hermes-rules.md'))
    for src, dst in files:
        print('安装：' + str(src) + ' → ' + str(dst))
        preview_file(src, dst)
    cron_source = source / 'crontab.line'
    line = cron_source.read_text().strip() if cron_source.exists() else ''
    if not line:
        raise ValueError('crontab.line 不能为空')
    print('更新 crontab 标记行：' + merge_crontab('', line, project).strip())
    if dry_run:
        print('预览模式不调用 crontab；正式安装时读取现有 crontab，保留其他条目并备份。')
        return
    old_cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if old_cron.returncode and 'no crontab' not in old_cron.stderr.lower():
        raise RuntimeError('无法读取 crontab，请检查本机 cron 安装和权限')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = target.parent / (target.name + '.backup-' + stamp)
    backup.mkdir(parents=True)
    (backup / 'crontab.txt').write_text(old_cron.stdout)
    for index, (src, dst) in enumerate(files):
        if dst.exists():
            saved = backup / 'files' / str(index)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, saved)
            with (backup / 'manifest.tsv').open('a') as manifest:
                manifest.write(str(saved.relative_to(backup)) + '\t' + str(dst) + '\n')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for key in ('runs_dir', 'run_root'):
        if settings.get(key):
            Path(settings[key]).expanduser().mkdir(parents=True, exist_ok=True)
    merged = merge_crontab(old_cron.stdout, line, project)
    subprocess.run(['crontab', '-'], input=merged, text=True, check=True)
    print('备份目录：' + str(backup))


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
    output = args.build_option or args.build_dir or 'build/' + config['project']['name']
    install(config, output, args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('安装失败：' + str(exc), file=sys.stderr)
        sys.exit(1)
