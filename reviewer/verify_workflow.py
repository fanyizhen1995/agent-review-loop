#!/usr/bin/env python3
"""按项目工作流执行评审复核，条件步骤在缺少筛选信息时保守重跑。"""
import fcntl
import fnmatch
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kit.config import load_config


def substitute_placeholders(command):
    """以环境变量传递占位符，按所在引号上下文保持值为单个参数。"""
    variables = {'branch': 'ARL_BRANCH', 'sha': 'ARL_SHA', 'kit': 'ARL_KIT_PATH', 'file': 'ARL_FILE'}
    output, quote, index = [], None, 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            output.append(command[index:index + 2])
            index += 2
            continue
        matched = False
        for key, variable in variables.items():
            token = '{' + key + '}'
            if command.startswith(token, index):
                reference = '${' + variable + '}'
                if quote == '"':
                    output.append(reference)
                elif quote == "'":
                    output.append("'" + '"' + reference + '"' + "'")
                else:
                    output.append('"' + reference + '"')
                index += len(token)
                matched = True
                break
        if matched:
            continue
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        output.append(char)
        index += 1
    return ''.join(output)


def git_value(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


def run(config, root, args):
    args = [str(Path(a).resolve()) if Path(a).exists() else a for a in args]
    role_dir = Path(__file__).resolve().parent
    issue_type = os.environ.get('ARL_ISSUE_TYPE')
    paths = os.environ.get('ARL_CHANGED_PATHS')
    branch = os.environ.get('ARL_BRANCH') or git_value(root, 'rev-parse', '--abbrev-ref', 'HEAD')
    sha = git_value(root, 'rev-parse', 'HEAD') or os.environ.get('ARL_SHA', '')
    environment = dict(os.environ, ARL_BRANCH=branch, ARL_SHA=sha, ARL_KIT_PATH=str(role_dir), ARL_FILE=os.environ.get('ARL_FILE', args[0] if args else ''))
    for step in config['workflow']['verify']:
        if not step['rerun_by_reviewer'] or not step.get('run'):
            continue
        if issue_type and step['for_types'] and issue_type not in step['for_types']:
            continue
        if paths is not None and step['when_paths'] and not any(fnmatch.fnmatch(p, pattern) for p in paths.splitlines() for pattern in step['when_paths']):
            continue
        if args and not step.get('reviewer_run'):
            raise ValueError('指定测试参数需要为步骤配置 workflow.verify.reviewer_run：' + step['name'])
        command = step.get('reviewer_run', step['run'])
        command = substitute_placeholders(command)
        # 配置命令可复用旧入口的 root/args；每一步独立切回代码目录。
        header = 'root=$1; shift; args=("$@"); export RUN_DIR; '
        if command.lstrip().startswith('hooks/'):
            command = shlex.quote(str(role_dir)) + '/' + command
        lock = None
        try:
            if step.get('lock'):
                lock_root = Path(config['reviewer']['runs_dir']) / 'verify-locks'
                lock_root.mkdir(parents=True, exist_ok=True)
                lock = (lock_root / (step['lock'] + '.lock')).open('a')
                fcntl.flock(lock, fcntl.LOCK_EX)
            print('复核步骤：' + step['name'], flush=True)
            subprocess.run(['bash', '-e', '-c', header + command, 'workflow', str(root), *args], cwd=root, env=environment, check=True)
        finally:
            if lock:
                lock.close()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('用法：runtest.sh <代码根目录> [测试参数...]')
    config = load_config(Path(__file__).with_name('project.toml'))
    run(config, Path(sys.argv[1]).resolve(), sys.argv[2:])
