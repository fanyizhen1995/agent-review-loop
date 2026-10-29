#!/usr/bin/env python3
"""执行端 API 入口；保留原动词并支持 GitHub 所需的 PATCH 和 DELETE。"""
import argparse
import json
import sys
from pathlib import Path
# 同时支持源码目录和安装后的 scripts/kit 目录。
for _parent in Path(__file__).resolve().parents:
    if (_parent / "kit").is_dir():
        sys.path.insert(0, str(_parent))
        break
from kit.config import load_config
from kit.forge import make_forge
from kit.transport import TransportError


def configuration():
    location = Path(__file__).resolve().parent / "project.toml"
    import os
    return load_config(os.environ.get("AGENT_REVIEW_CONFIG") or (str(location) if location.exists() else None))


def main(argv=None):
    parser = argparse.ArgumentParser(description="执行端 forge API")
    parser.add_argument("method", choices=("get", "post", "put", "patch", "delete"))
    parser.add_argument("path")
    parser.add_argument("body", nargs="?")
    args = parser.parse_args(argv)
    if args.method in ("post", "put", "patch") and args.body is None:
        parser.error("写操作需要 JSON 文件路径，或 - 表示标准输入")
    payload = None
    if args.body is not None:
        raw = sys.stdin.read() if args.body == "-" else Path(args.body).read_text()
        payload = json.loads(raw)
    forge = make_forge(configuration(), role="fixer")
    path = args.path if args.path.startswith("/") else "/" + args.path
    try:
        status, result = forge.transport.request_with_status(args.method.upper(), path, payload)
        if status >= 300:
            # 错误正文可能回显凭据或请求内容，统一输出固定说明。
            result = "API 请求失败，响应正文已省略"
    except TransportError as exc:
        status, result = exc.status, "API 请求失败，响应正文已省略"
    print(status)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
