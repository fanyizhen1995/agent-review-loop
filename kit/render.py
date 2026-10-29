"""严格的点分路径占位符渲染器。"""
import json
import re

TYPE_BLOCK = re.compile(r'{% if_type (\w+) %}(.*?){% endif_type %}', re.S)

PATTERN = re.compile(r'{{\s*([\w.]+)\s*}}')


def render_text(text, context):
    text = TYPE_BLOCK.sub(lambda match: match[2] if match[1] in context.get('workflow', {}).get('issue_types', []) else '', text)
    def replace(match):
        key = match[1]
        value = context
        for part in key.split('.'):
            if not isinstance(value, dict) or part not in value:
                raise ValueError(f'缺少模板参数：{key}')
            value = value[part]
        if isinstance(value, (dict, list, bool)):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            raise ValueError(f'模板参数为空：{key}')
        return str(value)
    return PATTERN.sub(replace, text)


def render_file(source, destination, context):
    from pathlib import Path
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_text(Path(source).read_text(), context), encoding='utf-8')
