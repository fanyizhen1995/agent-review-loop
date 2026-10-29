"""HTTP 与 SSH 链传输，凭据仅在进程内或标准输入中传递。"""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


class TransportError(RuntimeError):
    def __init__(self, status, message='API 请求失败'):
        self.status = status
        super().__init__(f'{message}（状态 {status}）')


def read_token(spec):
    def checked(value):
        value = value.strip()
        if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError('凭据必须为非空的单行 ASCII 字符串')
        return value
    if spec.get('env') and os.environ.get(spec['env']):
        return checked(os.environ[spec['env']])
    if spec.get('file'):
        return checked(Path(spec['file']).expanduser().read_text())
    if spec.get('dotenv_file') and spec.get('env'):
        for line in Path(spec['dotenv_file']).expanduser().read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and key.strip() == spec['env']:
                return checked(value.strip().strip('\"\''))
    raise ValueError('凭据来源不可用，请检查 file 或 env 配置')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportError(code, '拒绝重定向，避免向其他地址传递凭据')


def execute_request(envelope):
    url = envelope['url']
    body = envelope.get('body')
    try:
        request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
            method=envelope['method'], headers=envelope['headers'])
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            return {'status': response.status, 'data': json.loads(response.read() or b'null')}
    except urllib.error.HTTPError as exc:
        return {'status': exc.code, 'data': None}
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TransportError(0, '连接失败') from None
    except (ValueError, UnicodeError):
        raise TransportError(0, '请求或响应格式错误') from None


class HTTPTransport:
    def __init__(self, config, role='reviewer'):
        self.config, self.role = config, role
        forge = config['forge']
        self.kind = forge['kind']
        root = forge['base_url'].rstrip('/')
        self.base_url = root + ('/api/v4' if self.kind == 'gitlab' and not root.endswith('/api/v4') else '')
        self.spec = config.get('transport', {}).get(role, {'mode': 'direct'})

    def request(self, method, path, body=None):
        return self.request_with_status(method, path, body)[1]

    def request_with_status(self, method, path, body=None):
        if not path.startswith('/') or path.startswith('//') or urllib.parse.urlsplit(path).netloc:
            raise ValueError('API 路径必须为相对根路径')
        token = read_token(self.config['credentials'][self.role])
        headers = {'Content-Type': 'application/json'}
        if self.kind == 'gitlab':
            headers['PRIVATE-TOKEN'] = token
        else:
            headers.update({'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        envelope = {'url': self.base_url + urllib.parse.quote(path, safe='/?&=,:%+'), 'method': method, 'body': body, 'headers': headers}
        if self.spec['mode'] == 'direct':
            result = execute_request(envelope)
        else:
            result = self._ssh(envelope)
        if result['status'] >= 300:
            raise TransportError(result['status'])
        return result['status'], result['data']

    def _ssh_options(self):
        # 每个请求都会经过各跳 ssh；默认复用连接（ControlMaster），避免对共享跳板机频繁握手认证。
        # 控制套接字放在各跳主机自己的 ~/.ssh 下，可用 transport.<角色>.reuse_connections=false 关闭。
        options = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15']
        if self.spec.get('reuse_connections', True):
            options += ['-o', 'ControlMaster=auto', '-o', 'ControlPath=~/.ssh/arl-%C', '-o', 'ControlPersist=600']
        return options

    def _ssh(self, envelope):
        import inspect
        source = 'import json, sys, urllib.request, urllib.error\n' + inspect.getsource(TransportError) + '\n' + inspect.getsource(NoRedirect) + '\n' + inspect.getsource(execute_request) + '\nprint(json.dumps(execute_request(json.load(sys.stdin))))'
        encoded = base64.b64encode(source.encode()).decode()
        remote = shlex.join(['python3', '-c', f'import base64;exec(base64.b64decode({encoded!r}))'])
        hosts = self.spec['hosts']
        for host in reversed(hosts[1:]):
            if host.startswith('-'):
                raise ValueError('SSH 主机不得以 - 开头')
            remote = shlex.join(['ssh', *self._ssh_options(), host, remote])
        if hosts[0].startswith('-'):
            raise ValueError('SSH 主机不得以 - 开头')
        command = ['ssh', *self._ssh_options(), hosts[0], remote]
        # 共享主机上偶发的连接中断重试一次；复用连接后重试成本很低
        for attempt in range(2):
            proc = subprocess.run(command, input=json.dumps(envelope), capture_output=True, text=True, timeout=180)
            if proc.returncode == 0:
                break
            if attempt == 0:
                time.sleep(2)
        if proc.returncode:
            raise TransportError(0, 'SSH 链调用失败')
        try:
            return json.loads(proc.stdout)
        except ValueError:
            raise TransportError(0, 'SSH 链响应格式错误') from None


def make_transport(config, role='reviewer'):
    return HTTPTransport(config, role)
