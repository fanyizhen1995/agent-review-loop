#!/usr/bin/env python3
"""检查项目接入条件；--offline 仅核对本地配置和文件。"""
import argparse
import base64
import inspect
import io
import zipfile
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def check_token_reference(reference):
    if reference.get('file'):
        path = Path(reference['file']).expanduser()
        if not path.is_file() or not os.access(path, os.R_OK):
            return False, '凭据文件不可读'
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            return False, '凭据文件权限应为 0600'
        return True, '凭据文件可读且权限为 0600'
    if reference.get('env'):
        if os.environ.get(reference['env']):
            return True, '凭据环境变量已设置'
        path = Path(reference.get('dotenv_file', '')).expanduser()
        if path.is_file():
            prefix = reference['env'] + '='
            if any(line.strip().removeprefix('export ').startswith(prefix) for line in path.read_text().splitlines()):
                return True, '凭据 .env 引用存在，内容未输出'
        return False, '凭据环境变量或 .env 键不可读'
    return False, '未配置凭据引用'


def check_protection(kind, protection, role, auto_merge=False):
    if kind == 'github':
        approvals = protection.get('required_pull_request_reviews') or {}
        if not approvals:
            return False, '缺少 PR 保护，需要人工核实'
        if auto_merge and approvals.get('required_approving_review_count', 0) > 0:
            return False, '人工 approve 要求会阻止自动合入；评审账号豁免需人工核实'
        if not auto_merge and approvals.get('required_approving_review_count', 0) < 1:
            return False, '缺少至少 1 个人工 approve 的 PR 保护'
        bypass = approvals.get('bypass_pull_request_allowances') or {}
        if any(bypass.get(category) for category in ('users', 'teams', 'apps')):
            return False, '存在 PR 保护豁免账号、团队或应用，需人工核实执行方无法绕过'
        if (protection.get('allow_force_pushes') or {}).get('enabled', False):
            return False, '仍然允许 force push'
        if (protection.get('allow_deletions') or {}).get('enabled', False):
            return False, '仍然允许删除受保护分支'
        if not (protection.get('enforce_admins') or {}).get('enabled', False):
            return False, '请核实执行方无法通过管理员或规则豁免绕过保护'
        return True, ('已要求 PR，以成员评论同意控制自动合入，并关闭 force push' if auto_merge else '已要求 PR、至少 1 个批准，并关闭 force push')
    for key in ('push_access_levels', 'merge_access_levels'):
        entries = protection.get(key, [])
        if not entries or any(row.get('access_level', 0) not in (0, 40) for row in entries):
            return False, '推送和合入均应限制为 Maintainer，个别账号授权需人工核实'
        if any(row.get('user_id') or row.get('group_id') for row in entries):
            return False, '存在个别账号或组授权，需人工核实执行方无法绕过'
    if protection.get('allow_force_push', False):
        return False, '仍然允许 force push'
    return True, '推送与合入权限符合 Maintainer 限制'


def role_snapshot(config, role):
    from kit.forge import make_forge
    forge = make_forge(config, role=role)
    user = forge.current_user()
    login = user.get('username', user.get('login', ''))
    membership = forge.membership(login) or {}
    credential_ok, credential_detail = check_token_reference(config['credentials'][role])
    result = {'credential_ok': credential_ok, 'credential_detail': credential_detail, 'login': login, 'membership': {key: membership.get(key) for key in ('access_level', 'permission', 'role_name')}, 'expiry': forge.token_expiry()}
    if role == 'reviewer':
        result['labels'] = [row['name'] for row in forge.list_labels()]
        branches = config.get('safety', {}).get('protected_branches', config.get('branches', {}).get('protected', []))
        result['protections'] = {branch: forge.branch_protection(branch) for branch in branches}
    return result


def remote_snapshot(config):
    """把代码和凭据引用传给远端，凭据仅在远端读取，不回传 token。"""
    host = config['fixer'].get('ssh_host') or config['fixer']['host']
    if host.startswith('-') or any(char.isspace() for char in host):
        raise ValueError('执行端主机名格式错误')
    archive_data = io.BytesIO()
    package = Path(__file__).resolve().parents[1] / 'kit'
    with zipfile.ZipFile(archive_data, 'w') as archive:
        for path in sorted(package.glob('*.py')):
            archive.writestr('kit/' + path.name, path.read_bytes())
    payload = base64.b64encode(archive_data.getvalue()).decode()
    code = "import base64,io,json,os,stat,sys,tempfile,zipfile\nfrom pathlib import Path\n" + inspect.getsource(check_token_reference) + "\n" + inspect.getsource(role_snapshot)
    code += "\nwith tempfile.TemporaryDirectory(prefix='agent-review-loop-doctor-') as target:\n"
    code += "    zipfile.ZipFile(io.BytesIO(base64.b64decode(" + repr(payload) + "))).extractall(target)\n"
    code += "    sys.path.insert(0,target)\n    try:\n"
    code += "        print(json.dumps(role_snapshot(" + repr(config) + ",'fixer'),ensure_ascii=False))\n"
    code += "    except Exception as exc:\n        print(json.dumps({'error':type(exc).__name__}))\n"
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, 'python3 -'], input=code, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def doctor(config, fix=False, offline=False):
    from kit.conventions import ALL_LABELS
    problems = []
    def report(ok, message):
        print(('通过：' if ok else '待处理：') + message)
        if not ok:
            problems.append(message)
    for command in ('python3', 'git', 'claude', 'codex', 'crontab'):
        report(shutil.which(command) is not None, '本机命令 ' + command)
    report(sys.version_info >= (3, 11), 'Python 版本至少 3.11')
    ref = config['credentials']['reviewer']
    ok, detail = check_token_reference(ref)
    report(ok, 'reviewer ' + detail)
    if offline:
        print('离线检查：跳过全部 forge API、SSH、crontab 和远端任务读取。')
        print('fixer 凭据位置已配置，远端可读性和有效性需要联机检查。')
        return problems
    kind = config['forge']['kind']
    for role in ('reviewer', 'fixer'):
        try:
            data = role_snapshot(config, role) if role == 'reviewer' else remote_snapshot(config)
            if data.get('error'):
                raise RuntimeError('远端检查失败')
            report(data['credential_ok'], role + ' ' + data['credential_detail'])
            report(data['login'] == config['accounts'][role], role + ' API 账号与参数一致')
            member = data['membership'] or {}
            access = member.get('access_level')
            permission = member.get('permission', member.get('role_name'))
            if kind == 'gitlab':
                target = 20 if role == 'reviewer' else 30
                elevated = role == 'reviewer' and (config['reviewer'].get('merge', {}).get('enabled', True) or config['reviewer'].get('register', {}).get('enabled', False))
                if elevated:
                    report(access is not None and access >= 40, role + ' 项目角色：' + str(access) + '（合入和登记需 Maintainer；单独分支授权需人工核实）')
                else:
                    report(access is not None and target <= access < 40, role + ' 项目角色：' + str(access) + '（至少 ' + str(target) + '，低于 Maintainer）')
            else:
                elevated = role == 'reviewer' and (config['reviewer'].get('merge', {}).get('enabled', True) or config['reviewer'].get('register', {}).get('enabled', False))
                allowed = ('write', 'push', 'maintain', 'admin') if elevated else (('read', 'triage', 'write', 'push') if role == 'reviewer' else ('write', 'push'))
                report(permission in allowed, role + ' 仓库权限：' + str(permission))
            print(role + ' token 到期：' + (str(data['expiry']) if data['expiry'] else 'API 未提供；GitHub fine-grained token 到期需人工记录'))
            if role == 'reviewer':
                from kit.forge import make_forge
                forge = make_forge(config, role=role)
                names = set(data['labels'])
                source = config.get('labels', {}).get('source', config['forge'].get('source_label', 'source::agent'))
                for name in sorted(ALL_LABELS | {source}):
                    if name not in names and fix:
                        forge.create_label(name, '#6699cc', 'agent-review-loop 工作流标签')
                        names.add(name)
                    report(name in names, '标签 ' + name)
                for branch, protection in data['protections'].items():
                    ok, text = check_protection(kind, protection, role, auto_merge=config['reviewer'].get('merge', {}).get('enabled', True))
                    report(ok, branch + '：' + text)
        except Exception as exc:
            report(False, role + ' API 检查失败（' + type(exc).__name__ + '），请核对网络、凭据及权限')
    cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True) if shutil.which('crontab') else None
    marker = '# agent-review-loop:' + config['project']['name']
    cron_lines = cron.stdout.splitlines() if cron else []
    report(any(row.endswith(marker) for row in cron_lines), '本机轮询 cron 标记行存在')
    if config['reviewer'].get('register', {}).get('enabled', False):
        report(any(row.endswith(marker + ':register') for row in cron_lines), '本机每日登记 cron 标记行存在')
    host = config['fixer'].get('ssh_host') or config['fixer']['host']
    if host.startswith('-') or any(char.isspace() for char in host):
        report(False, '执行端 SSH 主机名格式错误')
        return problems
    remote = "import json,shutil,subprocess,sys; print(json.dumps({'python':sys.version_info[:2],'git':bool(shutil.which('git')),'hermes':bool(shutil.which('hermes'))})); subprocess.run(['hermes','cron','list','--json'],check=True)"
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, 'python3 -'], input=remote, capture_output=True, text=True)
    report(result.returncode == 0, '执行端 Python、git、hermes 与定时任务列表可读取')
    if result.returncode == 0:
        first, _, rest = result.stdout.partition('\n')
        try:
            info = json.loads(first)
            report(tuple(info['python']) >= (3, 11) and info['git'] and info['hermes'], '执行端命令与版本符合要求')
            jobs = json.loads(rest)
            rows = jobs.get('jobs', []) if isinstance(jobs, dict) else jobs
            names = {row['name'] for row in rows}
            for key in ('dispatcher_name', 'notify_name', 'cleanup_name'):
                name = config['fixer']['cron'][key]
                report(name in names, 'hermes 定时任务 ' + name)
        except (ValueError, KeyError, TypeError):
            report(False, '执行端任务返回格式需要人工核实')
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('--fix', action='store_true', help='创建缺失标签，仍需 token 具有相应权限')
    parser.add_argument('--offline', action='store_true', help='不访问网络或 SSH，仅检查本机条件')
    args = parser.parse_args(argv)
    if args.offline and args.fix:
        parser.error('--offline 不支持 --fix')
    from kit.config import load_config
    return bool(doctor(load_config(args.config), args.fix, args.offline))


if __name__ == '__main__':
    sys.exit(main())
