#!/usr/bin/env python3
"""离线检查工作区、HEAD 和 origin/main..HEAD 的新增发布内容。"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


class CheckError(Exception):
    """检查无法可靠完成。"""


def git(root, *args, allow_failure=False):
    env = dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, env=env)
    if result.returncode and not allow_failure:
        raise CheckError('本地 Git 读取失败，检查未完成')
    return result


def load_rules(path):
    try:
        lines = path.expanduser().read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        raise CheckError('规则文件不可读或不是 UTF-8 文本') from None
    rules = []
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        try:
            rules.append(re.compile(line))
        except re.error:
            raise CheckError(f'规则文件第 {number} 行正则无效') from None
    return rules


def excluded(name):
    path = Path(name)
    return bool(set(path.parts) & SKIP_DIRS) or path.suffix in {'.pyc', '.pyo'}


def text_content(data):
    if b'\x00' in data:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeError:
        return None


def workspace_files(root):
    def on_error(_error):
        raise CheckError('工作区目录不可读，检查未完成')

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        links = [name for name in dirs if (Path(directory) / name).is_symlink()]
        dirs[:] = [name for name in dirs if name not in links]
        for name in sorted(files + links):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if not excluded(relative):
                yield relative, path


def read_workspace(path, relative):
    try:
        if path.is_symlink():
            return os.fsencode(os.readlink(path))
        return path.read_bytes()
    except OSError:
        raise CheckError(f'工作区文件不可读：{relative}') from None


def scan(root, rules):
    hits = set()

    def matches(value):
        return any(rule.search(value) for rule in rules)

    def check_path(source, name):
        if matches(name):
            hits.add((source, name, 0))

    def check_content(source, name, data):
        content = text_content(data)
        if content is not None:
            for number, line in enumerate(content.splitlines(), 1):
                if matches(line):
                    hits.add((source, name, number))

    # .gitignore 忽略的文件（构建产物、运行目录）不会被推送，不扫描
    ignored = set(filter(None, git(root, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z',
                                   allow_failure=True).stdout.decode('utf-8', 'surrogateescape').split('\0'))) if (Path(root) / '.git').exists() else set()
    for name, path in workspace_files(root):
        if name in ignored:
            continue
        check_path('工作区', name)
        check_content('工作区', name, read_workspace(path, name))

    # 缺失的工作树文件视作待提交删除，不重新引入旧内容。
    def publishable(name):
        return not excluded(name) and os.path.lexists(root / name)

    # 暂存内容也可能与工作树不同，不能只依赖当前文件内容。
    entries = git(root, 'ls-files', '--stage', '-z').stdout
    for entry in entries.split(b'\x00'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, object_id, _stage = metadata.split()
        name = os.fsdecode(raw_name)
        if publishable(name):
            check_path('索引', name)
            if mode != b'160000':
                blob = git(root, 'cat-file', 'blob', object_id.decode('ascii')).stdout
                check_content('索引', name, blob)

    if git(root, 'rev-parse', '--verify', 'HEAD^{commit}', allow_failure=True).returncode:
        print('尚无 HEAD：离线降级，已检查当前工作区与索引')
        return hits

    head_paths = git(root, 'ls-tree', '-r', '-z', 'HEAD').stdout
    for entry in head_paths.split(b'\x00'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        _mode, object_type, object_id = metadata.split()
        name = os.fsdecode(raw_name)
        if publishable(name):
            check_path('HEAD', name)
            if object_type == b'blob':
                blob = git(root, 'cat-file', 'blob', object_id.decode('ascii')).stdout
                check_content('HEAD', name, blob)

    if git(root, 'rev-parse', '--verify', 'origin/main^{commit}', allow_failure=True).returncode:
        print('origin/main 不存在：离线降级，已扫描全部现存 HEAD 跟踪文件；未访问网络')
        return hits

    source = 'origin/main..HEAD'
    changed = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                  '--name-only', '--diff-filter=ACMRT', '-z', source, '--').stdout
    for raw in changed.split(b'\x00'):
        if not raw:
            continue
        name = os.fsdecode(raw)
        if not publishable(name):
            continue
        check_path(source, name)
        patch = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                    '--unified=0', source, '--', name).stdout
        content = text_content(patch)
        if content is None:
            continue
        line_number = None
        for line in content.splitlines():
            header = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
            if header:
                line_number = int(header.group(1))
            elif line_number is not None and line.startswith('+'):
                if matches(line[1:]):
                    hits.add((source, name, line_number))
                line_number += 1
            elif line_number is not None and line.startswith(' '):
                line_number += 1
    return hits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deny-file', type=Path, required=True,
                        help='仓库外部的 UTF-8 正则列表，每行一条，支持空行和 # 注释')
    args = parser.parse_args(argv)
    try:
        rules = load_rules(args.deny_file)
        result = git(Path.cwd(), 'rev-parse', '--show-toplevel')
        root = Path(os.fsdecode(result.stdout).strip())
        hits = scan(root, rules)
    except CheckError as exc:
        print(f'检查失败：{exc}', file=sys.stderr)
        return 2
    if hits:
        for source, name, number in sorted(hits):
            print(f'{source} {name}:{number}')
        print(f'发布检查命中 {len(hits)} 处；行号 0 表示文件路径命中')
        return 1
    print('发布检查：零命中')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
