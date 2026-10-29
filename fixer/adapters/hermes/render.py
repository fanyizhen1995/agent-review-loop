"""生成 Hermes 审批配置和三个定时任务描述。"""
import json
import tomllib

def _yaml_patch(output, config):
    approvals = tomllib.loads((output / 'fixer/config/approvals.toml').read_text())['approvals']
    delegation = tomllib.loads((output / 'fixer/config/delegation.toml').read_text())['delegation']
    lines = ['approvals:']
    for key in ('mode', 'cron_mode', 'timeout'):
        lines.append('  ' + key + ': ' + json.dumps(approvals[key], ensure_ascii=False))
    lines.append('  deny:')
    lines.extend('    - ' + json.dumps(rule, ensure_ascii=False) for rule in approvals['deny'])
    lines.append('delegation:')
    lines.extend('  ' + key + ': ' + json.dumps(value, ensure_ascii=False) for key, value in delegation.items())
    lines.extend(['agent:', '  max_turns: ' + str(config['fixer']['max_turns'])])
    return '\n'.join(lines) + '\n'


def _cron_jobs(config):
    cron = config['fixer']['cron']
    return {'jobs': [
        {'name': cron['dispatcher_name'], 'schedule': cron['dispatcher_schedule'], 'deliver': cron['deliver'], 'workdir': config['fixer']['repo'], 'prompt_file': 'dispatcher.md', 'monitor_script': 'scripts/actionable.py'},
        {'name': cron['notify_name'], 'schedule': cron['notify_schedule'], 'deliver': cron['deliver'], 'script': 'scripts/notify.py', 'no_agent': True},
        {'name': cron['cleanup_name'], 'schedule': cron['cleanup_schedule'], 'deliver': cron['cleanup_deliver'], 'script': 'scripts/cleanup.py', 'no_agent': True},
    ]}
