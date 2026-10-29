"""定时任务专用的进程内写权限；不通过环境变量向子进程传递授权。"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import PurePosixPath
import sys

_MERGE = ContextVar('merge_permission', default=False)
_REGISTER = ContextVar('register_permission', default=False)


def write_guard():
    if any(os.environ.get(k) == '1' for k in ('GL_READONLY', 'AGENT_REVIEW_READONLY', 'DRY_RUN')):
        sys.exit('拒绝: 只读模式下不允许写操作')
    if os.environ.get('CLAUDECODE') or os.environ.get('AGENT_REVIEW_HEADLESS') == '1':
        sys.exit('拒绝: 无头评审进程不允许合入或登记')


@contextmanager
def merge_permission():
    write_guard()
    token = _MERGE.set(True)
    try:
        yield
    finally:
        _MERGE.reset(token)


@contextmanager
def register_permission():
    write_guard()
    token = _REGISTER.set(True)
    try:
        yield
    finally:
        _REGISTER.reset(token)


def require_merge():
    write_guard()
    if not _MERGE.get():
        sys.exit('拒绝: 合入只能由自动合入流程执行')


def require_register(config, branch, files):
    write_guard()
    if not _REGISTER.get():
        sys.exit('拒绝: 文件提交只能由每日登记流程执行')
    validate_register_files(config, branch, files)


def validate_register_files(config, branch, files):
    """校验计划与实际提交共用的精确路径白名单，不授予写权限。"""
    settings = config.get('workflow', {}).get('register', config.get('reviewer', {}).get('register', {}))
    fmt = settings.get('format', 'agents-tasks-json')
    if fmt == 'changelog-md':
        allowed = {settings.get('file') or settings.get('changelog_file') or 'CHANGELOG.md'}
    elif fmt == 'hook':
        paths = settings.get('paths', [])
        configured_files = settings.get('files', [])
        if not isinstance(paths, list) or not isinstance(configured_files, list) or any(not isinstance(p, str) for p in paths + configured_files):
            sys.exit('拒绝: hook 必须显式配置 files/paths 路径列表')
        allowed = set(paths + configured_files)
    elif fmt == 'agents-tasks-json':
        allowed = {settings.get('tasks_file'), settings.get('progress_file')} - {None, ''}
    else:
        allowed = set()
    if not isinstance(files, dict):
        sys.exit('拒绝: 登记文件必须为路径到文本的映射')
    if branch != settings.get('branch', 'main') or not files or not set(files) <= allowed:
        sys.exit('拒绝: 登记仅允许配置的分支和文件')
    for path, content in files.items():
        if not isinstance(path, str) or PurePosixPath(path).is_absolute() or any(p in ('', '.', '..') for p in path.split('/')) or '\\' in path or not isinstance(content, str):
            sys.exit('拒绝: 登记文件路径或内容非法')
