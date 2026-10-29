"""评审端白名单和分支拉取规则的本地验证。"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ReviewerPolicyTests(unittest.TestCase):
    def load_cli(self):
        spec = importlib.util.spec_from_file_location('reviewer_forge_cli', ROOT / 'reviewer/forge_cli.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_gitlab_whitelist_rejects_merge_and_foreign_project(self):
        cli = self.load_cli()
        for method, path, body in [('PUT', '/projects/123/merge_requests/2/merge', {}),
                                   ('GET', '/projects/124/issues', None),
                                   ('PUT', '/projects/123/issues/2', {'title': '变更'})]:
            with self.assertRaises(SystemExit):
                cli.check_request({'method': method, 'path': path, 'body': body}, 'gitlab', '/projects/123')

    def test_labels_are_limited_to_original_review_scope(self):
        cli = self.load_cli()
        cli.check_request({'method': 'PUT', 'path': '/projects/123/issues/2',
                           'body': {'add_labels': 'scope::确认修复,review::已确认'}}, 'gitlab', '/projects/123')
        with self.assertRaises(SystemExit):
            cli.check_request({'method': 'PUT', 'path': '/projects/123/issues/2',
                               'body': {'add_labels': '等待前置'}}, 'gitlab', '/projects/123')

    def test_github_whitelist_blocks_approve_and_contents(self):
        cli = self.load_cli()
        for path in ['/repos/org/repo/pulls/2/reviews', '/repos/org/repo/contents/file']:
            with self.assertRaises(SystemExit):
                cli.check_request({'method': 'POST', 'path': path, 'body': {}}, 'github', '/repos/org/repo')
        cli.check_request({'method': 'PATCH', 'path': '/repos/org/repo/issues/2',
                           'body': {'state': 'closed'}}, 'github', '/repos/org/repo')

    def test_readonly_rejects_all_writes(self):
        cli = self.load_cli()
        for env in ['GL_READONLY', 'AGENT_REVIEW_READONLY', 'DRY_RUN']:
            with patch.dict(os.environ, {env: '1'}):
                with self.assertRaises(SystemExit):
                    cli.check_request({'method': 'POST', 'path': '/projects/123/issues/2/notes',
                                       'body': {'body': '评论'}}, 'gitlab', '/projects/123')

    def test_rejects_query_and_traversal_bypass(self):
        cli = self.load_cli()
        for path in ['/projects/123/../124/issues', '/projects/123/issues/2/notes?sudo=other',
                     '/projects/123/%2e%2e/124/issues']:
            with self.assertRaises(SystemExit):
                cli.check_request({'method': 'POST', 'path': path, 'body': {'body': '评论'}}, 'gitlab', '/projects/123')

    def test_github_rejects_replace_all_labels(self):
        cli = self.load_cli()
        with self.assertRaises(SystemExit):
            cli.check_request({'method': 'PUT', 'path': '/repos/org/repo/issues/2/labels',
                               'body': {'labels': ['review::已确认']}}, 'github', '/repos/org/repo')

    def test_github_own_comment_uses_user_field(self):
        cli = self.load_cli()
        class FakeForge:
            def request(self, method, path):
                return {'id': 4, 'user': {'login': 'reviewer'}}
        cli._CONFIG = {'accounts': {'reviewer': 'reviewer'}}
        cli._FORGE = FakeForge()
        cli._ownership('/repos/org/repo/issues/comments/4')
        cli._CONFIG['accounts']['reviewer'] = 'other'
        with self.assertRaises(SystemExit):
            cli._ownership('/repos/org/repo/issues/comments/4')


class PollModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('reviewer_poll', ROOT / 'reviewer/poll.py')
        cls.poll = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'AGENT_REVIEW_CONFIG': str(ROOT / 'examples/projects/example-gitlab/project.toml')}):
            spec.loader.exec_module(cls.poll)

    def test_github_fork_uses_pull_ref(self):
        with patch.dict(self.poll.CONFIG['forge'], {'kind': 'github'}):
            self.assertEqual(self.poll.fetch_ref({'iid': 8, 'source_branch': 'fix/other'}), 'refs/pull/8/head')

    def test_gitlab_keeps_remote_tracking_ref(self):
        with patch.dict(self.poll.CONFIG['forge'], {'kind': 'gitlab'}):
            self.assertEqual(self.poll.fetch_ref({'iid': 8, 'source_branch': 'fix/other'}), 'refs/remotes/origin/fix/other')

    def test_reply_and_unchanged_sha_use_light_review(self):
        self.assertEqual(self.poll.is_light({'mode': 'reply'}), '回复评论')
        self.assertEqual(self.poll.is_light({'kind': 'mr', 'sha': 'abc', 'prev_key': 'abc|旧时间'}), 'MR 无新提交')
        self.assertIsNone(self.poll.is_light({'kind': 'mr', 'sha': 'def', 'prev_key': 'abc|旧时间'}))
        self.assertIsNone(self.poll.is_light({'mode': 'triage'}))

    def test_issue_light_review_requires_latest_non_system_text_only_verdict(self):
        reviewer = self.poll.CONFIG['accounts']['reviewer']
        cases = [
            ([], None),
            ([{'system': False, 'author': {'username': reviewer},
               'body': '评审结论：需修改\n修改类型：仅文字'}], '上次只要求文字修改'),
            ([{'system': False, 'author': {'username': reviewer},
               'body': '评审结论：已确认\n修改类型：仅文字'}], None),
            ([{'system': True, 'author': {'username': reviewer},
               'body': '评审结论：需修改\n修改类型：仅文字'}], None),
            ([{'system': False, 'author': {'username': 'fixer-bot'},
               'body': '评审结论：需修改\n修改类型：仅文字'}], None),
            ([{'system': False, 'author': {'username': reviewer}, 'body': '评审结论：需修改\n请修复代码'},
              {'system': False, 'author': {'username': reviewer},
               'body': '评审结论：需修改\n修改类型：仅文字'}], None),
        ]
        for notes, expected in cases:
            with self.subTest(notes=notes):
                class FakeForge:
                    def list_notes(self, kind, iid, sort, limit):
                        return notes
                with patch.object(self.poll.gl, 'forge', return_value=FakeForge()):
                    self.assertEqual(self.poll.is_light({'kind': 'issue', 'iid': 1}), expected)


class ReviewerQueueTests(unittest.TestCase):
    def setUp(self):
        self.cli = ReviewerPolicyTests().load_cli()
        self.cli._CONFIG = {'accounts': {'user': 'maintainer-a', 'fixer': 'fixer-bot',
            'reviewer': 'review-bot', 'user_note_since': '2026-10-27T15:20:00Z',
            'legacy_fixer_author': 'maintainer-a（历史执行账号）'},
            'labels': {'source': 'source::agent'}}
        self.issues = [{'iid': 1, 'title': '问题', 'updated_at': '2026-10-28T00:00:00Z',
                        'labels': ['source::agent', 'review::待评审']}]
        self.mrs = [{'iid': 2, 'title': '修复', 'updated_at': '2026-10-28T00:01:00Z',
                     'labels': ['source::agent', 'review::待评审'], 'sha': 'abc', 'source_branch': 'fix/a'}]

    def test_pending_uses_timestamp_and_sha_change_keys(self):
        outer = self
        calls = []
        class FakeForge:
            def list_items(self, kind, state, labels, limit):
                calls.append((kind, state, labels, limit))
                return outer.issues if kind == 'issue' else outer.mrs
        self.cli._FORGE = FakeForge()
        self.assertEqual(self.cli.pending(), [
            {'iid': 1, 'title': '问题', 'updated_at': '2026-10-28T00:00:00Z', 'kind': 'issue',
             'key': '2026-10-28T00:00:00Z'},
            {'iid': 2, 'title': '修复', 'updated_at': '2026-10-28T00:01:00Z', 'kind': 'mr',
             'sha': 'abc', 'source_branch': 'fix/a', 'key': 'abc|2026-10-28T00:01:00Z'}])
        self.assertEqual([call[2] for call in calls], [['source::agent', 'review::待评审']] * 2)

    def test_merged_list_only_queries_mrs_and_preserves_merge_commit(self):
        outer = self
        calls = []
        self.mrs[0]['merge_commit_sha'] = 'merged-sha'
        class FakeForge:
            kind = 'gitlab'
            def list_items(self, kind, state, labels, limit, **kwargs):
                calls.append((kind, state, labels, kwargs))
                return outer.mrs
        self.cli._FORGE = FakeForge()
        self.assertEqual(self.cli.hermes_items('merged'), [{
            'iid': 2, 'title': '修复', 'labels': ['source::agent', 'review::待评审'],
            'updated_at': '2026-10-28T00:01:00Z', 'kind': 'mr', 'merge_commit_sha': 'merged-sha'}])
        self.assertEqual(calls, [('mr', 'merged', ['source::agent'], {'order_by': 'updated_at'})])

    def test_reply_cutoff_settled_state_and_system_notes(self):
        cases = [
            ('用户截止后', 'maintainer-a', '2026-10-28T00:00:00Z', [], False, '用户'),
            ('用户截止前已确认', 'maintainer-a', '2026-10-26T00:00:00Z', ['review::已确认'], False, 'maintainer-a（历史执行账号）'),
            ('用户截止前待评审', 'maintainer-a', '2026-10-26T00:00:00Z', ['review::待评审'], False, None),
            ('执行方已确认', 'fixer-bot', '2026-10-28T00:00:00Z', ['review::已确认'], False, 'hermes'),
            ('执行方可合入', 'fixer-bot', '2026-10-28T00:00:00Z', ['review::可合入'], False, 'hermes'),
            ('执行方待评审', 'fixer-bot', '2026-10-28T00:00:00Z', ['review::待评审'], False, None),
            ('评审方评论', 'review-bot', '2026-10-28T00:00:00Z', ['review::已确认'], False, None),
            ('忽略较新系统评论', 'maintainer-a', '2026-10-28T00:00:00Z', [], True, '用户'),
        ]
        for title, who, at, labels, system_prefix, expected_author in cases:
            with self.subTest(title=title):
                issues = [dict(self.issues[0], labels=['source::agent'] + labels)]
                notes = [{'id': 9, 'author': {'username': who}, 'created_at': at, 'system': False, 'body': '说明'}]
                if system_prefix:
                    notes.insert(0, dict(notes[0], id=10, system=True, author={'username': 'review-bot'}))
                class FakeForge:
                    def human_members(self):
                        return {'maintainer-a'}
                    def list_items(self, kind, state, labels, limit):
                        return issues if kind == 'issue' else []
                    def list_notes(self, kind, iid, sort, limit):
                        return notes
                self.cli._FORGE = FakeForge()
                expected = [] if expected_author is None else [{
                    'kind': 'issue', 'iid': 1, 'title': '问题', 'mode': 'reply', 'author': expected_author,
                    'updated_at': at, 'note_id': 9, 'sha': None, 'source_branch': None, 'key': 'note-9'}]
                self.assertEqual(self.cli.replies(), expected)


if __name__ == '__main__':
    unittest.main()
