"""归一项目工作流，并生成三个角色共用的验证要求。"""
import math
import re


def normalize(config, raw):
    workflow = config.setdefault('workflow', {})
    for key in ('merge', 'register'):
        previous = config['reviewer'][key]
        previous.update(workflow.get(key, {}))
        workflow[key] = previous
    register = workflow['register']
    def relative_file(value, key):
        if (not isinstance(value, str) or not value or value.startswith('/') or '\\' in value
                or any(c in value for c in ('\n', '\r', '\0'))
                or any(part in ('', '.', '..') for part in value.split('/'))):
            raise ValueError('workflow.register.' + key + ' 必须为仓库内相对文件路径')
    for key in ('file', 'changelog_file', 'hook'):
        if key in register and not (key == 'hook' and register[key] == ''):
            relative_file(register[key], key)
    for key in ('paths', 'files'):
        paths = register.get(key, [])
        if not isinstance(paths, list):
            raise ValueError('workflow.register.' + key + ' 必须为相对文件路径列表')
        for path in paths:
            relative_file(path, key)
    if register.get('format') == 'hook':
        if not register.get('hook'):
            raise ValueError('workflow.register.hook 必须指定已安装 hooks 下的脚本')
        if not register.get('paths') and not register.get('files'):
            raise ValueError('workflow.register.paths/files 必须显式列出 hook 可写文件')
    workflow.setdefault('language', 'zh')
    workflow.setdefault('issue_types', ['bug', 'perf', 'robust', 'refactor'])
    if workflow['language'] != 'zh':
        raise ValueError('workflow.language 目前只提供 zh 模板，其他语言尚未支持')
    kinds = workflow['issue_types']
    if not isinstance(kinds, list) or not kinds or any(x not in ('bug', 'perf', 'robust', 'refactor') for x in kinds) or len(set(kinds)) != len(kinds):
        raise ValueError('workflow.issue_types 必须为不重复的 bug/perf/robust/refactor 类型列表')
    if 'verify' not in workflow:
        workflow['verify'] = [{'name': 'unit', 'run': config['reviewer']['tests']['full_command'],
                               'reviewer_run': config['reviewer']['tests']['command'].replace('exec ', ''), 'rerun_by_reviewer': True},
                              *([{'name': 'regression', 'builtin': 'regression', 'for_types': [x for x in ('bug', 'robust') if x in kinds]}] if set(kinds) & {'bug', 'robust'} else [])]
    steps = workflow['verify']
    if not isinstance(steps, list):
        raise ValueError('workflow.verify 必须为步骤列表')
    names = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError('workflow.verify 步骤必须为表')
        for key in ('run', 'reviewer_run', 'description', 'lock', 'builtin'):
            if key in step and (not isinstance(step[key], str) or any(c in step[key] for c in ('\r', '\n', '\0'))):
                raise ValueError('workflow.verify.' + key + ' 必须为单行文本')
        name = step.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[\w.-]+', name) or name in names:
            raise ValueError('workflow.verify.name 必须为唯一的步骤名')
        names.add(name)
        if bool(step.get('run')) == bool(step.get('builtin')):
            raise ValueError('workflow.verify 必须且只能指定 run 或 builtin')
        if step.get('builtin') and step['builtin'] != 'regression':
            raise ValueError('workflow.verify.builtin 目前只支持 regression')
        for key in ('for_types', 'when_paths'):
            value = step.setdefault(key, [])
            if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError('workflow.verify.' + key + ' 必须为字符串列表')
        if set(step['for_types']) - set(kinds):
            raise ValueError('workflow.verify.for_types 含未允许的类型')
        if type(step.setdefault('rerun_by_reviewer', False)) is not bool:
            raise ValueError('workflow.verify.rerun_by_reviewer 必须为布尔值')
        if step.get('lock') and not re.fullmatch(r'[\w.-]+', step['lock']):
            raise ValueError('workflow.verify.lock 必须为锁名称')
    metrics = workflow.setdefault('metrics', {})
    metrics.setdefault('complexity_cmd', '')
    metrics.setdefault('test_ratio_limit', 0)
    if not isinstance(metrics['complexity_cmd'], str):
        raise ValueError('workflow.metrics.complexity_cmd 必须为命令字符串')
    limit = metrics['test_ratio_limit']
    if type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0:
        raise ValueError('workflow.metrics.test_ratio_limit 必须为非负数')
    metrics.setdefault('test_min_lines', 40)
    metrics.setdefault('refactor_test_limit', 50)
    metrics.setdefault('coverage_cmd', '')
    for key in ('test_min_lines', 'refactor_test_limit'):
        if type(metrics[key]) is not int or metrics[key] < 0:
            raise ValueError('workflow.metrics.' + key + ' 必须为非负整数')
    if not isinstance(metrics['coverage_cmd'], str):
        raise ValueError('workflow.metrics.coverage_cmd 必须为命令字符串')
    project = config.setdefault('project', {})
    project.setdefault('test_dirs', [])
    if not isinstance(project['test_dirs'], list) or any(not isinstance(x, str) or not x for x in project['test_dirs']):
        raise ValueError('project.test_dirs 必须为目录列表')
    for key in ('approve_phrases', 'revoke_phrases'):
        value = workflow['merge'].setdefault(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() or '\n' in x for x in value):
            raise ValueError('workflow.merge.' + key + ' 必须为短语列表')
    config['fixer'].setdefault('adapter', 'hermes')
    if config['fixer']['adapter'] != 'hermes':
        raise ValueError('fixer.adapter 尚未提供该适配器，目前只支持 hermes')


def test_scale_text(config):
    """测试规模要求：按净增行数（新增减删除）计算，重构默认不新增测试。"""
    m = config['workflow']['metrics']
    if not m['test_ratio_limit']:
        return '本项目不限制测试与源码的行数比例。'
    text = (f"测试规模按净增行数（新增减删除）计算：bug、perf、robust 类新增测试不超过源码净增行数的 {m['test_ratio_limit']:g} 倍，"
            f"最少允许 {m['test_min_lines']} 行")
    if 'refactor' in config['workflow']['issue_types']:
        text += (f"；refactor 类默认不新增测试，只有被改函数完全没有测试覆盖时补最少用例，不超过 {m['refactor_test_limit']} 行")
    return text + '；超出时在 MR 写明理由，评审按 `git diff --numstat` 的净增行数核对，无合理理由判需修改。'


def requirements(config):
    lines = []
    for step in config['workflow']['verify']:
        scope = '所有类型' if not step['for_types'] else '、'.join(step['for_types'])
        line = f"- {step['name']}（{scope}）："
        if step.get('builtin') == 'regression':
            line += f"回归测试在 {config['branches']['base']} 上失败、在分支上通过，两组输出写进 MR；失败原因须对应问题。"
        else:
            line += '`' + step['run'] + '`；MR 必填执行命令、版本与完整结果。'
        if step.get('description'):
            line += ' ' + step['description']
        if step['when_paths']:
            line += ' 仅改动匹配 ' + '、'.join('`' + p + '`' for p in step['when_paths']) + ' 时要求。'
        if step.get('lock'):
            line += ' 全过程持有同名锁 `' + step['lock'] + '`，同名步骤串行。'
        if step['rerun_by_reviewer']:
            line += ' 评审方通过 runtest.sh 复核重跑。'
        lines.append(line)
    metrics = config['workflow']['metrics']
    if metrics['complexity_cmd'] and 'refactor' in config['workflow']['issue_types']:
        lines.append('- refactor：用 `' + metrics['complexity_cmd'] + '` 给出并复核改动前后的圈复杂度、行数、嵌套深度。')
    # 测试规模要求已写在规则"测试规则"与评审标准"测试规模"中，这里不重复
    return '\n'.join(lines) or '- 本项目未配置验证步骤；MR 明确记录验证范围及尚未验证的条件。'
