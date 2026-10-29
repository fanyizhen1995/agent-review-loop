"""读取项目 TOML；配置内仅允许凭据来源引用。"""
import copy
import os
import re
from pathlib import Path
import sys
import tomllib
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .conventions import SOURCE_LABEL_DEFAULT, USER_MENTION_TEMPLATE

DEFAULTS = {
    'labels': {'source': SOURCE_LABEL_DEFAULT},
    'transport': {'reviewer': {'mode': 'direct'}, 'fixer': {'mode': 'direct'}},
    'reviewer': {'merge': {'enabled': True, 'approvers': [], 'github_method': 'merge', 'remove_source_branch': True},
        'register': {'enabled': False, 'cron': '0 21 * * *', 'branch': 'main',
                     'tasks_file': 'tasks.json', 'progress_file': 'progress.md',
                     'format': 'none', 'id_prefix': 'hermes'},
        'limits': {'slots': 3, 'max_runs_per_hour': 12, 'max_attempts': 2,
        'claude_timeout': 1800, 'codex_timeout': 3600, 'codex_effort': 'high',
        'verify_budget_usd': '2', 'light_budget_usd': '1', 'quiet_minutes': 20,
        'mr_quiet_minutes': 5, 'user_quiet_minutes': 3}, 'cron': {'schedule': '*/3 * * * *'}},
}


def merge(base, overlay):
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _expand(value):
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, str) and value.startswith('~/'):
        return os.path.expanduser(value)
    return value


def load_config(path=None):
    path = Path(path or os.environ.get('AGENT_REVIEW_CONFIG') or Path(sys.argv[0]).resolve().parent / 'project.toml')
    if path.is_dir():
        path = path / 'project.toml'
    with path.open('rb') as stream:
        raw = tomllib.load(stream)
    cfg = _expand(merge(DEFAULTS, raw))
    # 远端路径根据执行端 home 展开，避免误用本机用户目录。
    remote = raw.get('fixer', {})
    remote_home = remote.get('home')
    if not remote_home and str(remote.get('hermes_home', '')).startswith('/'):
        remote_home = str(Path(remote['hermes_home']).parent)
    def remote_expand(value):
        if isinstance(value, dict):
            return {k: remote_expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remote_expand(v) for v in value]
        if isinstance(value, str) and value.startswith('~/'):
            return str(Path(remote_home) / value[2:]) if remote_home else value
        return value
    if remote:
        cfg['fixer'] = remote_expand(remote)
    if 'fixer' in raw.get('credentials', {}):
        cfg.setdefault('credentials', {})['fixer'] = remote_expand(raw['credentials']['fixer'])
    for table, keys in {'project': ('name',), 'forge': ('kind', 'base_url', 'project')}.items():
        for key in keys:
            if not cfg.get(table, {}).get(key):
                raise ValueError(f'缺少配置参数：{table}.{key}')
    if not isinstance(cfg['project']['name'], str) or not re.fullmatch(r'[\w][\w.-]*', cfg['project']['name']):
        raise ValueError('project.name 必须是只含字母、数字、下划线、点或连字符的项目名')
    if cfg['forge']['kind'] == 'gitlab' and (type(cfg['forge']['project']) is not int or cfg['forge']['project'] <= 0):
        raise ValueError('forge.project 必须为正整数 GitLab 项目编号')
    if cfg['forge']['kind'] not in ('gitlab', 'github'):
        raise ValueError('forge.kind 仅支持 gitlab/github')
    if not cfg['forge']['base_url'].startswith(('https://', 'http://')):
        raise ValueError('forge.base_url 必须是 HTTP/HTTPS 地址')
    url = urlsplit(cfg['forge']['base_url'])
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('forge.base_url 必须为不含凭据、查询或片段的站点地址')
    if cfg['forge']['kind'] == 'github' and len(str(cfg['forge']['project']).split('/')) != 2:
        raise ValueError('GitHub forge.project 应为 owner/repo')
    if 'source' not in raw.get('labels', {}) and 'source_label' in cfg['forge']:
        cfg['labels']['source'] = cfg['forge']['source_label']
    cfg['forge']['source_label'] = cfg['labels']['source']
    accounts = cfg.setdefault('accounts', {})
    accounts.setdefault('mention', accounts.get('user_mention', USER_MENTION_TEMPLATE.format(username=accounts.get('user', ''))))
    accounts.setdefault('user_mention', accounts['mention'])
    accounts.setdefault('user_note_since', '')
    required = {
        'project': ('language', 'source_dirs', 'source_extensions', 'hot_files', 'test_commands', 'forbidden_files'),
        'accounts': ('reviewer', 'fixer', 'user'),
        'branches': ('base', 'protected', 'forbidden_prefixes', 'work_prefixes'),
        'reviewer': ('repo', 'install_dir', 'ref_dir', 'runs_dir', 'run_root', 'git', 'tests', 'scope'),
        'reviewer.git': ('fetch_url', 'pre_fetch_cmd', 'base_branch', 'base_ref', 'source_ref_prefix'),
        'reviewer.tests': ('command', 'full_command', 'node_command'),
        'reviewer.cron': ('schedule', 'path', 'python'),
        'fixer': ('host', 'hermes_home', 'repo', 'review_dir', 'ref_dir', 'rules_file', 'deploy', 'approvals', 'delegation', 'cron', 'max_turns'),
    }
    for table, keys in required.items():
        value = cfg
        for part in table.split('.'):
            value = value.get(part, {}) if isinstance(value, dict) else {}
        for key in keys:
            if key not in value:
                raise ValueError(f'缺少配置参数：{table}.{key}')
    for role in ('reviewer', 'fixer'):
        spec = cfg['transport'][role]
        if spec['mode'] not in ('direct', 'ssh_chain'):
            raise ValueError(f'transport.{role}.mode 不支持')
        if spec['mode'] == 'ssh_chain' and not spec.get('hosts'):
            raise ValueError(f'transport.{role}.hosts 不得为空')
        cred = cfg.get('credentials', {}).get(role, {})
        if set(cred) - {'file', 'env', 'dotenv_file', 'type_display'}:
            raise ValueError(f'credentials.{role} 只能指定 file/env/dotenv_file 和 type_display')
        if not cred.get('file') and not cred.get('env'):
            raise ValueError(f'credentials.{role} 必须指定 file 或 env')
        if cred.get('dotenv_file') and not cred.get('env'):
            raise ValueError(f'credentials.{role}.dotenv_file 必须同时指定 env')
    for key in ('slots', 'max_runs_per_hour', 'max_attempts', 'claude_timeout', 'codex_timeout'):
        if not isinstance(cfg['reviewer']['limits'][key], int) or cfg['reviewer']['limits'][key] < 1:
            raise ValueError(f'reviewer.limits.{key} 必须为正整数')
    for key in ('quiet_minutes', 'mr_quiet_minutes', 'user_quiet_minutes'):
        value = cfg['reviewer']['limits'][key]
        if type(value) is not int or value < 0:
            raise ValueError(f'reviewer.limits.{key} 必须为非负整数')
    for key in ('verify_budget_usd', 'light_budget_usd'):
        try:
            value = Decimal(str(cfg['reviewer']['limits'][key]))
            valid = value.is_finite() and value > 0
        except InvalidOperation:
            valid = False
        if not valid:
            raise ValueError(f'reviewer.limits.{key} 必须为正数预算')
    from .workflow import normalize
    normalize(cfg, raw)
    merge_settings = cfg['reviewer']['merge']
    register = cfg['reviewer']['register']
    for name, value in (('merge.enabled', merge_settings['enabled']),
                        ('merge.remove_source_branch', merge_settings['remove_source_branch']),
                        ('register.enabled', register['enabled'])):
        if type(value) is not bool:
            raise ValueError(f'reviewer.{name} 必须为布尔值')
    if merge_settings['github_method'] not in ('merge', 'squash', 'rebase'):
        raise ValueError('reviewer.merge.github_method 仅支持 merge/squash/rebase')
    if not isinstance(merge_settings['approvers'], list) or any(not isinstance(x, str) or not x.strip() for x in merge_settings['approvers']):
        raise ValueError('reviewer.merge.approvers 必须为账号列表')
    if register['format'] not in ('none', 'agents-tasks-json', 'changelog-md', 'hook'):
        raise ValueError('reviewer.register.format 仅支持 none/changelog-md/agents-tasks-json/hook')
    for key in ('tasks_file', 'progress_file'):
        value = register[key]
        if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value or any(p in ('', '.', '..') for p in value.split('/')):
            raise ValueError(f'reviewer.register.{key} 必须为仓库内相对文件路径')
    if register['tasks_file'] == register['progress_file']:
        raise ValueError('reviewer.register 的两个文件必须不同')
    for key in ('branch', 'id_prefix'):
        if not isinstance(register[key], str) or not register[key].strip() or any(c in register[key] for c in ('\n', '\r')):
            raise ValueError(f'reviewer.register.{key} 必须为非空单行文本')
    register_cron = register['cron']
    if not isinstance(register_cron, str) or '\n' in register_cron or '\r' in register_cron or len(register_cron.split()) != 5:
        raise ValueError('reviewer.register.cron 必须为单行五字段 cron 表达式')
    schedule = cfg['reviewer']['cron']['schedule']
    if '\n' in schedule or '\r' in schedule or len(schedule.split()) != 5:
        raise ValueError('reviewer.cron.schedule 必须为单行五字段 cron 表达式')
    return cfg
