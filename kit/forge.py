"""GitLab 与 GitHub 的统一条目接口。"""
import base64
import re

from .conventions import APPROVAL_RE, REVOKE_RE
from .permissions import require_merge, require_register
from urllib.parse import quote, urlencode

from .transport import TransportError, make_transport


def approval_patterns(settings=None):
    """空列表保留默认约定；配置短语视作字面量并保持原有词边界。"""
    settings = settings or {}
    def pattern(key, default):
        phrases = settings.get(key)
        if not phrases:
            return default
        if not isinstance(phrases, list) or any(not isinstance(p, str) or not p.strip() for p in phrases):
            raise ValueError(key + ' 必须为非空短语列表')
        return re.compile(r'^\s*(?:' + '|'.join(re.escape(p.strip()) for p in phrases) + r')(\W|$)', re.I)
    return pattern('approve_phrases', APPROVAL_RE), pattern('revoke_phrases', REVOKE_RE)


def _author(value):
    value = dict(value or {})
    value.setdefault('username', value.get('login', ''))
    return value


class Forge:
    def __init__(self, config, transport=None, role='reviewer'):
        self.config = config
        self._human_members = None
        self.transport = transport or make_transport(config, role)

    def request(self, method, path, body=None):
        return self.transport.request(method, path, body)

    def _pages(self, path, params=None, limit=None):
        params = dict(params or {})
        params.setdefault('per_page', min(limit or 100, 100))
        page, out = 1, []
        while True:
            query = dict(params, page=page)
            rows = self.request('GET', path + '?' + urlencode(query, safe=','))
            if not isinstance(rows, list):
                raise ValueError('列表 API 返回格式错误')
            out.extend(rows)
            if limit is not None and len(out) >= limit:
                return out[:limit]
            if len(rows) < params['per_page']:
                return out
            page += 1

    def list_merged(self, labels=None, limit=None):
        return self.list_items('mr', 'merged', labels, limit=limit)

    def fetch_ref(self, item):
        return item['source_branch']

    def human_members(self):
        accounts = self.config.get('accounts', {})
        excluded = {accounts.get('reviewer'), accounts.get('fixer')}
        explicit = self.config.get('workflow', {}).get('merge', self.config.get('reviewer', {}).get('merge', {})).get('approvers')
        if explicit:
            return set(explicit) - excluded
        if self._human_members is not None:
            return set(self._human_members) - excluded
        if self.kind == 'gitlab':
            rows = self._pages(self.base + '/members/all')
            names = {m['username'] for m in rows if m.get('access_level', 0) >= 30
                     and not m['username'].startswith('project_') and not m.get('bot')}
        else:
            rows = self._pages(self.base + '/collaborators', {'affiliation': 'all'})
            names = {m['login'] for m in rows if m.get('role_name') in ('write', 'maintain', 'admin')
                     or any(m.get('permissions', {}).get(p) for p in ('push', 'write', 'maintain', 'admin'))}
        self._human_members = names
        return names - excluded


class GitLabForge(Forge):
    kind = 'gitlab'

    def __init__(self, config, transport=None, role='reviewer'):
        super().__init__(config, transport, role)
        self.base = '/projects/' + quote(str(config['forge']['project']), safe='')

    def item_path(self, kind, iid=None):
        if kind not in ('issue', 'mr'):
            raise ValueError('kind 仅支持 issue/mr')
        path = self.base + ('/issues' if kind == 'issue' else '/merge_requests')
        return path if iid is None else path + '/' + str(int(iid))

    @staticmethod
    def normalize(item):
        out = dict(item)
        out.update(iid=item.get('iid', item.get('id')), number=item.get('iid', item.get('id')),
            title=item.get('title', ''), labels=item.get('labels', []), state=item.get('state', ''),
            updated_at=item.get('updated_at', ''), author=_author(item.get('author')),
            web_url=item.get('web_url', ''), description=item.get('description') or '')
        return out

    def list_items(self, kind, state='opened', labels=None, limit=None, order_by=None):
        params = {'state': state}
        if order_by:
            params['order_by'] = order_by
        if labels:
            params['labels'] = ','.join(labels) if not isinstance(labels, str) else labels
        return [self.normalize(row) for row in self._pages(self.item_path(kind), params, limit)]

    def get_item(self, kind, iid):
        return self.normalize(self.request('GET', self.item_path(kind, iid)))

    def list_notes(self, kind, iid, sort='asc', limit=100):
        notes = self._pages(self.item_path(kind, iid) + '/notes', {'sort': sort}, limit)
        return [dict(n, system=n.get('system', False), author=_author(n.get('author'))) for n in notes]

    def get_note(self, kind, iid, note_id):
        return self.request('GET', self.item_path(kind, iid) + '/notes/' + str(int(note_id)))

    def add_note(self, kind, iid, body):
        return self.request('POST', self.item_path(kind, iid) + '/notes', {'body': body})

    def edit_note(self, kind, iid, note_id, body):
        return self.request('PUT', self.item_path(kind, iid) + '/notes/' + str(int(note_id)), {'body': body})

    def set_labels(self, kind, iid, add=(), remove=()):
        return self.request('PUT', self.item_path(kind, iid), {'add_labels': ','.join(add), 'remove_labels': ','.join(remove)})

    def close(self, kind, iid):
        return self.request('PUT', self.item_path(kind, iid), {'state_event': 'close'})

    def changes(self, iid):
        return self.request('GET', self.item_path('mr', iid) + '/changes')

    def commits(self, iid):
        return self._pages(self.item_path('mr', iid) + '/commits')

    def search_issues(self, text):
        return [self.normalize(x) for x in self._pages(self.item_path('issue'), {'search': text, 'state': 'all'})]

    def current_user(self):
        return _author(self.request('GET', '/user'))

    def token_expiry(self):
        return self.request('GET', '/personal_access_tokens/self').get('expires_at')

    def list_labels(self):
        return self._pages(self.base + '/labels')

    def create_label(self, name, color='#6699cc', description=''):
        return self.request('POST', self.base + '/labels', {'name': name, 'color': color, 'description': description})

    def branch_protection(self, branch):
        return self.request('GET', self.base + '/protected_branches/' + quote(branch, safe=''))

    def membership(self, username):
        matches = self._pages(self.base + '/members/all', {'query': username})
        return next((x for x in matches if x.get('username') == username), None)

    def project_settings(self):
        return self.request('GET', self.base)

    def merge_status(self, iid):
        item = self.get_item('mr', iid)
        state = item.get('detailed_merge_status') or 'unknown'
        if item.get('has_conflicts') or state in ('conflict', 'need_rebase'):
            return 'conflict'
        if state == 'mergeable':
            return 'mergeable'
        if state in ('checking', 'unchecked', 'preparing', 'approvals_syncing'):
            return 'checking'
        return 'blocked:' + state

    def merge(self, iid, sha, remove_source_branch):
        require_merge()
        result = self.request('PUT', self.item_path('mr', iid) + '/merge',
                              {'sha': sha, 'should_remove_source_branch': remove_source_branch})
        return result.get('merge_commit_sha') or result.get('squash_commit_sha') or ''

    def branch_head(self, branch):
        return self.request('GET', self.base + '/repository/branches/' + quote(branch, safe=''))['commit']['id']

    def read_file(self, path, ref):
        data = self.request('GET', self.base + '/repository/files/' + quote(path, safe='') + '?' + urlencode({'ref': ref}))
        return {'content': base64.b64decode(data['content']).decode('utf-8'), 'last_commit_id': data['last_commit_id']}

    def commit_files(self, branch, message, files, expected_head):
        require_register(self.config, branch, files)
        if not expected_head or self.branch_head(branch) != expected_head:
            raise ValueError('目标分支已变化，停止登记')
        actions = []
        for path, content in files.items():
            try:
                snapshot = self.read_file(path, expected_head)
            except TransportError as exc:
                if exc.status != 404:
                    raise
                actions.append({'action': 'create', 'file_path': path, 'content': content})
            else:
                actions.append({'action': 'update', 'file_path': path, 'content': content,
                                'last_commit_id': snapshot['last_commit_id']})
        result = self.request('POST', self.base + '/repository/commits',
                              {'branch': branch, 'commit_message': message, 'actions': actions})
        return result['id']


class GitHubForge(Forge):
    kind = 'github'

    def __init__(self, config, transport=None, role='reviewer'):
        super().__init__(config, transport, role)
        self.base = '/repos/' + '/'.join(quote(part, safe='') for part in str(config['forge']['project']).split('/'))

    def item_path(self, kind, iid=None):
        if kind not in ('issue', 'mr'):
            raise ValueError('kind 仅支持 issue/mr')
        path = self.base + ('/issues' if kind == 'issue' else '/pulls')
        return path if iid is None else path + '/' + str(int(iid))

    @staticmethod
    def normalize(item):
        out = dict(item)
        out.update(iid=item.get('number'), number=item.get('number'), title=item.get('title', ''),
            labels=[x['name'] if isinstance(x, dict) else x for x in item.get('labels', [])],
            state='merged' if item.get('merged_at') or item.get('merged') else {'open': 'opened'}.get(item.get('state'), item.get('state', '')),
            updated_at=item.get('updated_at', ''), author=_author(item.get('user')),
            web_url=item.get('html_url', ''), description=item.get('body') or '')
        if 'head' in item:
            out.update(sha=item['head'].get('sha'), source_branch=item['head'].get('ref'))
        return out

    def list_items(self, kind, state='opened', labels=None, limit=None):
        wanted = set(labels.split(',') if isinstance(labels, str) else labels or [])
        params = {'state': {'opened': 'open', 'merged': 'closed'}.get(state, state), 'sort': 'updated', 'direction': 'desc'}
        if kind == 'issue' and wanted:
            params['labels'] = ','.join(sorted(wanted))
        # PR API 不支持 labels；在取回后过滤，避免把截断页误认为完整匹配结果。
        rows = self._pages(self.item_path(kind), params)
        result = []
        for row in rows:
            if kind == 'issue' and 'pull_request' in row:
                continue
            item = self.normalize(row)
            if state == 'merged' and item['state'] != 'merged':
                continue
            if wanted <= set(item['labels']):
                result.append(item)
        return result if limit is None else result[:limit]

    def get_item(self, kind, iid):
        return self.normalize(self.request('GET', self.item_path(kind, iid)))

    def list_notes(self, kind, iid, sort='asc', limit=100):
        # GitHub 评论 API 按创建时间升序返回；降序需先取全页再截取。
        rows = self._pages(self.item_path('issue', iid) + '/comments', limit=limit if sort == 'asc' else None)
        if sort == 'desc':
            rows = list(reversed(rows))[:limit] if limit else list(reversed(rows))
        return [dict(n, system=False, author=_author(n.get('user'))) for n in rows]

    def get_note(self, kind, iid, note_id):
        n = self.request('GET', self.base + '/issues/comments/' + str(int(note_id)))
        return dict(n, system=False, author=_author(n.get('user')))

    def add_note(self, kind, iid, body):
        return self.request('POST', self.item_path('issue', iid) + '/comments', {'body': body})

    def edit_note(self, kind, iid, note_id, body):
        return self.request('PATCH', self.base + '/issues/comments/' + str(int(note_id)), {'body': body})

    def set_labels(self, kind, iid, add=(), remove=()):
        path = self.item_path('issue', iid) + '/labels'
        result = None
        if add:
            result = self.request('POST', path, {'labels': list(add)})
        for name in remove:
            try:
                result = self.request('DELETE', path + '/' + quote(name, safe=''))
            except TransportError as exc:
                if exc.status != 404:
                    raise
        return result

    def close(self, kind, iid):
        return self.request('PATCH', self.item_path(kind, iid), {'state': 'closed'})

    def changes(self, iid):
        return {'changes': [dict(row, new_path=row['filename'], diff=row.get('patch', '')) for row in self._pages(self.item_path('mr', iid) + '/files')]}

    def commits(self, iid):
        return self._pages(self.item_path('mr', iid) + '/commits')

    def search_issues(self, text):
        params = {'q': f'repo:{self.config["forge"]["project"]} is:issue {text}', 'per_page': 100}
        result, page = [], 1
        while True:
            data = self.request('GET', '/search/issues?' + urlencode(dict(params, page=page)))
            rows = data.get('items', [])
            result.extend(self.normalize(row) for row in rows)
            if len(rows) < 100 or len(result) >= min(data.get('total_count', 0), 1000):
                return result
            page += 1

    def current_user(self):
        return _author(self.request('GET', '/user'))

    def token_expiry(self):
        return None

    def list_labels(self):
        return self._pages(self.base + '/labels')

    def create_label(self, name, color='#6699cc', description=''):
        return self.request('POST', self.base + '/labels', {'name': name, 'color': color.lstrip('#'), 'description': description})

    def branch_protection(self, branch):
        return self.request('GET', self.base + '/branches/' + quote(branch, safe='') + '/protection')

    def membership(self, username):
        return self.request('GET', self.base + '/collaborators/' + quote(username, safe='') + '/permission')

    def project_settings(self):
        return self.request('GET', self.base)

    def fetch_ref(self, item):
        return f'pull/{item["iid"]}/head'

    def merge_status(self, iid):
        item = self.get_item('mr', iid)
        state = item.get('mergeable_state') or 'unknown'
        if item.get('mergeable') is None or state == 'unknown':
            return 'checking'
        if state == 'dirty':
            return 'conflict'
        return 'mergeable' if state == 'clean' else 'blocked:' + state

    def merge(self, iid, sha, remove_source_branch):
        require_merge()
        item = self.get_item('mr', iid)
        method = self.config.get('reviewer', {}).get('merge', {}).get('github_method', 'merge')
        result = self.request('PUT', self.item_path('mr', iid) + '/merge', {'sha': sha, 'merge_method': method})
        if not result.get('merged'):
            raise ValueError('GitHub 未合入：' + str(result.get('message', '未知原因')))
        head_repo = (item.get('head', {}).get('repo') or {}).get('full_name')
        if remove_source_branch and head_repo == self.config['forge']['project']:
            # 合入已经成功；源分支清理失败不能让调用方误认为合入失败。
            try:
                self.request('DELETE', self.base + '/git/refs/heads/' + quote(item['source_branch'], safe=''))
            except TransportError:
                pass
        return result['sha']

    def branch_head(self, branch):
        return self.request('GET', self.base + '/git/ref/heads/' + quote(branch, safe=''))['object']['sha']

    def read_file(self, path, ref):
        data = self.request('GET', self.base + '/contents/' + quote(path, safe='/') + '?' + urlencode({'ref': ref}))
        return {'content': base64.b64decode(data['content']).decode('utf-8'), 'last_commit_id': data['sha']}

    def commit_files(self, branch, message, files, expected_head):
        require_register(self.config, branch, files)
        if not expected_head or self.branch_head(branch) != expected_head:
            raise ValueError('目标分支已变化，停止登记')
        parent = self.request('GET', self.base + '/git/commits/' + quote(expected_head, safe=''))
        entries = []
        for path, content in files.items():
            blob = self.request('POST', self.base + '/git/blobs', {'content': content, 'encoding': 'utf-8'})
            entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        tree = self.request('POST', self.base + '/git/trees', {'base_tree': parent['tree']['sha'], 'tree': entries})
        commit = self.request('POST', self.base + '/git/commits',
                              {'message': message, 'tree': tree['sha'], 'parents': [expected_head]})
        self.request('PATCH', self.base + '/git/refs/heads/' + quote(branch, safe=''), {'sha': commit['sha'], 'force': False})
        return commit['sha']


def make_forge(config, role='reviewer', transport=None):
    cls = GitLabForge if config['forge']['kind'] == 'gitlab' else GitHubForge
    return cls(config, transport, role)
