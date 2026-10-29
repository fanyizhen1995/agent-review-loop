#!/usr/bin/env python3
"""按函数统计圈复杂度、长度和最大嵌套深度，用于重构类 issue/MR 的前后对比。

圈复杂度按 McCabe 定义近似计算：1 + 分支节点数（if/for/while/except/with 条件、
布尔运算的附加操作数、条件表达式、推导式中的 if、match case、assert）。
用法: complexity.py <文件或目录>... [--top N] [--json]
"""
import ast
import json
import sys
from pathlib import Path

BRANCH = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.Assert)
NEST = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)


def complexity(fn):
    c = 1
    for n in ast.walk(fn):
        if isinstance(n, BRANCH):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            c += len(n.ifs)
        elif hasattr(ast, "match_case") and isinstance(n, ast.match_case):
            c += 1
    return c


def depth(node, d=0):
    best = d
    for ch in ast.iter_child_nodes(node):
        if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        best = max(best, depth(ch, d + 1 if isinstance(ch, NEST) else d))
    return best


def functions(path):
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    out = []

    def visit(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                visit(ch, prefix + ch.name + ".")
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append({"file": str(path), "function": prefix + ch.name, "line": ch.lineno,
                            "cc": complexity(ch), "lines": ch.end_lineno - ch.lineno + 1,
                            "depth": depth(ch)})
                visit(ch, prefix + ch.name + ".")
    visit(tree, "")
    return out


def main(argv):
    top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 20
    paths = [a for a in argv if not a.startswith("--") and not a.isdigit()]
    files = [f for p in paths for f in (sorted(Path(p).rglob("*.py")) if Path(p).is_dir() else [Path(p)])]
    rows = [r for f in files for r in functions(f)]
    rows.sort(key=lambda r: (-r["cc"], -r["lines"]))
    if "--json" in argv:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=1)
        return
    print(f"{'圈复杂度':>8} {'行数':>5} {'嵌套':>4}  位置")
    for r in rows[:top]:
        print(f"{r['cc']:>8} {r['lines']:>5} {r['depth']:>4}  {r['file']}:{r['line']} {r['function']}")
    print(f"\n函数总数 {len(rows)}；圈复杂度 >10 的 {sum(r['cc'] > 10 for r in rows)} 个，"
          f">20 的 {sum(r['cc'] > 20 for r in rows)} 个")


if __name__ == "__main__":
    main(sys.argv[1:])
