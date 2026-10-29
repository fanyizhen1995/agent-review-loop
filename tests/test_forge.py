"""统一 forge 的全部请求构造使用离线传输替身。"""
import unittest
from urllib.parse import parse_qs, urlsplit
from kit.forge import GitHubForge, GitLabForge


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.responses.pop(0)


class ForgeRequestTests(unittest.TestCase):
    def test_gitlab_all_read_and_write_endpoints(self):
        t = Recorder({'iid': 8}, [], {}, {}, [], [], {'username': 'a'}, {'expires_at': '2026-12-01'}, {}, {})
        f = GitLabForge({'forge': {'project': 46}}, t)
        f.get_item('mr', 8)
        f.list_notes('mr', 8, sort='desc', limit=20)
        f.close('issue', 8)
        f.changes(8)
        f.commits(8)
        f.search_issues('中文 条目')
        self.assertEqual(f.current_user()['username'], 'a')
        self.assertEqual(f.token_expiry(), '2026-12-01')
        f.create_label('review::待评审', '#123456')
        f.branch_protection('release/a')
        expected = [
            ('GET', '/projects/46/merge_requests/8'),
            ('GET', '/projects/46/merge_requests/8/notes'),
            ('PUT', '/projects/46/issues/8'),
            ('GET', '/projects/46/merge_requests/8/changes'),
            ('GET', '/projects/46/merge_requests/8/commits'),
            ('GET', '/projects/46/issues'),
            ('GET', '/user'), ('GET', '/personal_access_tokens/self'),
            ('POST', '/projects/46/labels'), ('GET', '/projects/46/protected_branches/release%2Fa'),
        ]
        self.assertEqual([(m, p.split('?')[0]) for m, p, b in t.calls], expected)
        self.assertEqual(t.calls[2][2], {'state_event': 'close'})
        self.assertEqual(parse_qs(urlsplit(t.calls[5][1]).query)['search'], ['中文 条目'])

    def test_github_pulls_use_pulls_endpoint_and_filter_labels(self):
        rows = [
            {'number': 1, 'title': '合入', 'state': 'closed', 'merged_at': '时间', 'labels': [{'name': 'source::agent'}], 'head': {'sha': 'abc', 'ref': 'fork'}, 'user': {'login': 'author'}, 'html_url': 'https://example.invalid/1'},
            {'number': 2, 'title': '关闭', 'state': 'closed', 'merged_at': None, 'labels': []},
        ]
        t = Recorder(rows, {}, [], [], {'items': [], 'total_count': 0}, {}, {})
        f = GitHubForge({'forge': {'project': 'o/r'}}, t)
        items = f.list_merged(['source::agent'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sha'], 'abc')
        self.assertEqual(items[0]['author']['username'], 'author')
        f.close('mr', 1)
        f.commits(1)
        f.list_notes('mr', 1)
        f.search_issues('故障')
        f.create_label('review::待评审', '#123456')
        f.branch_protection('main')
        paths = [(m, p.split('?')[0]) for m, p, b in t.calls]
        self.assertEqual(paths, [('GET', '/repos/o/r/pulls'), ('PATCH', '/repos/o/r/pulls/1'), ('GET', '/repos/o/r/pulls/1/commits'), ('GET', '/repos/o/r/issues/1/comments'), ('GET', '/search/issues'), ('POST', '/repos/o/r/labels'), ('GET', '/repos/o/r/branches/main/protection')])
        self.assertEqual(t.calls[5][2]['color'], '123456')

    def test_github_notes_are_normalized_and_descending(self):
        t = Recorder([{'id': 1, 'user': {'login': 'a'}}, {'id': 2, 'user': {'login': 'b'}}])
        f = GitHubForge({'forge': {'project': 'o/r'}}, t)
        notes = f.list_notes('mr', 1, sort='desc', limit=1)
        self.assertEqual(notes[0]['id'], 2)
        self.assertFalse(notes[0]['system'])
        self.assertEqual(notes[0]['author']['username'], 'b')

    def test_pagination_and_limit_are_separate(self):
        t = Recorder([{'iid': n} for n in range(100)], [{'iid': 100}])
        f = GitLabForge({'forge': {'project': 46}}, t)
        self.assertEqual(len(f.list_items('issue')), 101)
        self.assertIn('page=2', t.calls[1][1])
        t = Recorder([{'iid': n} for n in range(50)])
        f = GitLabForge({'forge': {'project': 46}}, t)
        self.assertEqual(len(f.list_items('mr', limit=50)), 50)
        self.assertEqual(len(t.calls), 1)
