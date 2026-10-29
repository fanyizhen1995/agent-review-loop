"""公共接口的离线验证。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kit import config, render, transport, forge


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.responses.pop(0)


class CoreTests(unittest.TestCase):
    def test_missing_template_parameter(self):
        with self.assertRaisesRegex(ValueError, 'a.b'):
            render.render_text('{{ a.b }}', {})
        self.assertEqual(render.render_text('{{ a.b }}', {'a': {'b': 3}}), '3')

    def test_credential_environment_and_dotenv(self):
        with patch.dict(os.environ, {'KIT_TEST_TOKEN': 'secret'}):
            self.assertEqual(transport.read_token({'env': 'KIT_TEST_TOKEN'}), 'secret')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as d:
            p = Path(d) / 'env'
            p.write_text('KEY=value\n')
            self.assertEqual(transport.read_token({'env': 'KEY', 'dotenv_file': str(p)}), 'value')

    def test_gitlab_constructs_project_paths(self):
        t = FakeTransport([[{'iid': 3, 'title': '问题', 'labels': [], 'author': {'username': 'a'}}], {}, {}])
        f = forge.GitLabForge({'forge': {'project': 123}}, t)
        self.assertEqual(f.list_items('issue')[0]['number'], 3)
        f.edit_note('issue', 3, 8, '正文')
        f.set_labels('mr', 3, ['review::已确认'], ['review::待评审'])
        self.assertIn('/projects/123/issues?', t.calls[0][1])
        self.assertEqual(t.calls[1], ('PUT', '/projects/123/issues/3/notes/8', {'body': '正文'}))
        self.assertEqual(t.calls[2][2]['add_labels'], 'review::已确认')

    def test_github_issues_exclude_pulls_and_comments_use_issues(self):
        t = FakeTransport([[{'number': 2, 'title': 'PR', 'pull_request': {}}, {'number': 3, 'title': '问题', 'labels': [{'name': 'a'}], 'user': {'login': 'u'}}], {}, {}])
        f = forge.GitHubForge({'forge': {'project': 'a/b'}}, t)
        items = f.list_items('issue')
        self.assertEqual([x['iid'] for x in items], [3])
        f.add_note('mr', 3, '正文')
        f.edit_note('mr', 3, 7, '修改')
        self.assertEqual(t.calls[1], ('POST', '/repos/a/b/issues/3/comments', {'body': '正文'}))
        self.assertEqual(t.calls[2], ('PATCH', '/repos/a/b/issues/comments/7', {'body': '修改'}))
        self.assertIsNone(f.token_expiry())

    def test_github_changes_and_fork_ref(self):
        t = FakeTransport([[{'filename': 'a.py', 'patch': '@@'}]])
        f = forge.GitHubForge({'forge': {'project': 'a/b'}}, t)
        self.assertEqual(f.changes(4)['changes'][0]['new_path'], 'a.py')
        self.assertEqual(f.fetch_ref({'iid': 4, 'source_branch': 'x'}), 'pull/4/head')

    def test_github_label_removal_never_replaces_other_labels(self):
        t = FakeTransport([{}, {}])
        f = forge.GitHubForge({'forge': {'project': 'a/b'}}, t)
        f.set_labels('issue', 1, ['review::已确认'], ['review::待评审'])
        self.assertEqual([c[0] for c in t.calls], ['POST', 'DELETE'])

    def test_ssh_token_only_on_stdin(self):
        cfg = {'forge': {'kind': 'gitlab', 'base_url': 'https://example.invalid'}, 'credentials': {'reviewer': {'env': 'KIT_TEST_TOKEN'}}, 'transport': {'reviewer': {'mode': 'ssh_chain', 'hosts': ['jump', 'host']}}}
        with patch.dict(os.environ, {'KIT_TEST_TOKEN': 'hidden-token'}), patch('subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"status":200,"data":{}}'
            transport.make_transport(cfg).request('GET', '/user')
            self.assertNotIn('hidden-token', str(run.call_args.args))
            self.assertIn('hidden-token', run.call_args.kwargs['input'])


if __name__ == '__main__':
    unittest.main()

class CredentialSafetyTests(unittest.TestCase):
    def test_multiline_token_rejected_without_echo(self):
        with patch.dict(os.environ, {'KIT_TEST_TOKEN': 'sensitive\nheader'}):
            with self.assertRaises(ValueError) as result:
                transport.read_token({'env': 'KIT_TEST_TOKEN'})
            self.assertNotIn('sensitive', str(result.exception))

    def test_header_exception_never_echoes_token(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = ValueError('Invalid header value secret-token')
            with self.assertRaises(transport.TransportError) as result:
                transport.execute_request({'url': 'https://example.invalid', 'method': 'GET', 'headers': {}})
            self.assertNotIn('secret-token', str(result.exception))

    def test_http_status_preserved_for_fixer_cli(self):
        cfg = {'forge': {'kind': 'gitlab', 'base_url': 'https://example.invalid'}, 'credentials': {'fixer': {'env': 'KIT_TEST_TOKEN'}}, 'transport': {'fixer': {'mode': 'direct'}}}
        with patch.dict(os.environ, {'KIT_TEST_TOKEN': 'value'}), patch('kit.transport.execute_request', return_value={'status': 201, 'data': {'id': 1}}):
            self.assertEqual(transport.make_transport(cfg, 'fixer').request_with_status('POST', '/projects/1/issues', {}), (201, {'id': 1}))

    def test_github_remove_absent_label_is_idempotent(self):
        class MissingLabel:
            def request(self, method, path, body=None):
                raise transport.TransportError(404)
        f = forge.GitHubForge({'forge': {'project': 'a/b'}}, MissingLabel())
        self.assertIsNone(f.set_labels('issue', 1, remove=['review::待评审']))

    def test_gitlab_list_keeps_forge_default_order(self):
        t = FakeTransport([[]])
        f = forge.GitLabForge({'forge': {'project': 123}}, t)
        f.list_items('issue')
        self.assertNotIn('order_by', t.calls[0][1])
