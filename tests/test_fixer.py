"""执行端迁移的行为约束。"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FixerTests(unittest.TestCase):
    def test_rules_preserve_required_boundaries(self):
        self.assertTrue((ROOT / 'fixer/rules.base.md.tmpl').exists(), '缺少执行规则模板')
        text = (ROOT / 'fixer/rules.base.md.tmpl').read_text()
        for value in ('同一项往返 {{ fixer.max_review_rounds }} 轮仍未收敛', '不 approve、不合入', '不重启、不停止系统服务', 'review::需修改'):
            self.assertIn(value, text)
        self.assertIn('{{ project.name }}', text)
        self.assertIn('{{ _generated.fixer_credential_description }}', text)

    def test_notify_still_only_reports_user_reply_and_mergeable(self):
        text = (ROOT / 'fixer/adapters/hermes/scripts/notify.py').read_text()
        self.assertIn('first = st.get("version") != 2', text)
        self.assertIn('review::可合入', text)
        self.assertIn('startswith(USER_MENTION)', text)
        self.assertNotIn('urllib.request', text)

    def test_actionable_retains_scope_and_waiting_gate(self):
        text = (ROOT / 'fixer/adapters/hermes/scripts/actionable.py').read_text()
        self.assertIn('"review::已确认" in labels and "scope::确认修复" in labels', text)
        self.assertIn('"等待前置" not in', text)
        self.assertIn('make_forge', text)
        self.assertNotIn('urllib.request', text)

    def test_example_has_both_forge_kinds(self):
        import tomllib
        for kind in ('gitlab', 'github'):
            with self.subTest(kind=kind):
                data = tomllib.loads((ROOT / 'examples/projects' / ('example-' + kind) / 'project.toml').read_text())
                self.assertEqual(data['forge']['kind'], kind)


def script_module(name):
    spec = importlib.util.spec_from_file_location('fixer_' + name, ROOT / 'fixer/adapters/hermes/scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixerBehaviorTests(unittest.TestCase):
    def test_actionable_consumes_normalized_forge_changes(self):
        """公共 changes 返回带 changes 数组的 dict；文件冲突信息必须完整。"""
        import copy
        import io
        import tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from kit.config import load_config
        module = script_module('actionable')
        cfg = copy.deepcopy(load_config(ROOT / 'examples/projects/example-gitlab/project.toml'))
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            repo = Path(directory) / 'repo'
            (repo / 'src').mkdir(parents=True)
            (repo / 'src/free.py').write_text('')
            (repo / 'src/excluded.py').write_text('')
            cfg['project']['excluded_source_files'] = ['excluded.py', '__init__.py']
            review = Path(directory) / 'review'
            review.mkdir()
            (review / 'findings.md').write_text('')
            (review / 'reviewed-files.txt').write_text('')
            cfg['fixer']['repo'] = str(repo)
            cfg['fixer']['review_dir'] = str(review)
            own = {'iid': 11, 'labels': [cfg['forge']['source_label']], 'description': 'Closes #3', 'title': '', 'source_branch': 'fix/3-done'}
            issues = [{'iid': n, 'state': 'opened', 'labels': labels, 'created_at': '2026-01-01'} for n, labels in (
                (1, ['review::已确认', 'scope::确认修复']),
                (2, ['review::已确认']),
                (3, ['review::已确认', 'scope::确认修复']))]

            class FakeForge:
                def list_items(self, kind, state, labels, limit=None):
                    if kind == 'issue':
                        return issues
                    return [] if state == 'merged' else [own]

                def list_notes(self, *args, **kwargs):
                    return []

                def changes(self, iid):
                    return {'changes': [{'new_path': 'src/active.py', 'diff': ''}]}

            stdout = io.StringIO()
            with patch.object(module, 'configuration', return_value=cfg), patch.object(module, 'make_forge', return_value=FakeForge()), patch('subprocess.run') as run, redirect_stdout(stdout):
                run.return_value.stdout = ''
                module.main()
            output = stdout.getvalue()
            self.assertIn('open_mr_files: !11:src/active.py', output)
            self.assertIn('dev_slot: #1 | 可开发（待开发：#1）', output)
            self.assertIn('下一个文件 src/free.py', output)

    def test_notify_baseline_then_reports_only_reply_and_mergeable(self):
        import copy
        import io
        import tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from kit.config import load_config
        module = script_module('notify')
        cfg = copy.deepcopy(load_config(ROOT / 'examples/projects/example-gitlab/project.toml'))
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            cfg['fixer']['notify_state_file'] = str(Path(directory) / 'state.json')
            item = {'iid': 7, 'title': '修复示例', 'labels': [], 'web_url': 'https://example.invalid/7'}
            note = {'id': 1, 'system': False, 'author': {'username': cfg['accounts']['reviewer']}, 'body': cfg['accounts']['user_mention'] + ' 已回复'}

            class FakeForge:
                def list_items(self, kind, state, labels, limit=None):
                    return [item] if kind == 'mr' else []

                def list_notes(self, *args, **kwargs):
                    return [note]

            with patch.object(module, 'configuration', return_value=cfg), patch.object(module, 'make_forge', return_value=FakeForge()):
                baseline = io.StringIO()
                with redirect_stdout(baseline):
                    module.main()
                self.assertEqual(baseline.getvalue(), '')
                note['id'] = 2
                item['labels'] = ['review::可合入']
                changed = io.StringIO()
                with redirect_stdout(changed):
                    module.main()
                self.assertIn('[你的评论有回复] !7', changed.getvalue())
                self.assertIn('[可合入] !7', changed.getvalue())
                repeated = io.StringIO()
                with redirect_stdout(repeated):
                    module.main()
                self.assertEqual(repeated.getvalue(), '')

    def test_legacy_author_before_identity_cutover_is_own(self):
        from unittest.mock import patch
        from kit.config import load_config
        module = script_module('actionable')
        cfg = load_config(ROOT / 'examples/projects/example-gitlab/project.toml')
        with patch.object(module, 'make_forge'):
            module.configure(cfg)
        self.assertTrue(module.is_own({'author': {'username': cfg['accounts']['fixer']}, 'created_at': '2099-01-01'}))
        self.assertTrue(module.is_own({'author': {'username': cfg['accounts']['legacy_fixer_username']}, 'created_at': '2026-01-01'}))
        self.assertFalse(module.is_own({'author': {'username': cfg['accounts']['legacy_fixer_username']}, 'created_at': '2099-01-01'}))

    def test_cleanup_uses_configured_test_temporary_prefix(self):
        import os
        import tempfile
        import time
        from unittest.mock import patch
        from kit.config import load_config
        cfg = load_config(ROOT / 'examples/projects/example-gitlab/project.toml')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            cfg['fixer']['scratch_dir'] = directory
            cfg['fixer']['scratch_suffixes'] = ['.tar']
            cfg['fixer']['scratch_prefixes'] = ['check-']
            expected = Path(directory) / 'check-old'
            unrelated = Path(directory) / 'pytest-of-other'
            for path in (expected, unrelated):
                path.mkdir()
                old = time.time() - cfg['fixer']['scratch_min_age_seconds'] - 1
                os.utime(path, (old, old))
            with patch('kit.config.load_config', return_value=cfg):
                module = script_module('cleanup')
            with patch.object(module, 'git', return_value=''), patch.object(module, 'busy_paths', return_value=set()), patch.object(module, 'token_expiry', return_value=None), patch.object(module.subprocess, 'run') as run, patch('builtins.print'):
                module.main()
            run.assert_called_once_with(['rm', '-rf', '--', str(expected)])


class ForgeApiBehaviorTests(unittest.TestCase):
    def test_created_response_keeps_http_status(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import Mock, patch
        module = script_module('forge_api')
        forge = Mock()
        forge.request.return_value = {'id': 8}
        forge.transport.request_with_status.return_value = (201, {'id': 8})
        stdout = io.StringIO()
        with patch.object(module, 'configuration', return_value={}), patch.object(module, 'make_forge', return_value=forge), patch('sys.stdin', io.StringIO('{"title":"示例"}')), redirect_stdout(stdout):
            module.main(['post', '/items', '-'])
        self.assertEqual(stdout.getvalue().splitlines()[0], '201')
        forge.transport.request_with_status.assert_called_once_with('POST', '/items', {'title': '示例'})

    def test_http_error_prints_status_without_response_body(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import Mock, patch
        from kit.transport import TransportError
        module = script_module('forge_api')
        forge = Mock()
        forge.request.side_effect = TransportError(403, '敏感响应不得输出')
        forge.transport.request_with_status.side_effect = TransportError(403, '敏感响应不得输出')
        stdout = io.StringIO()
        with patch.object(module, 'configuration', return_value={}), patch.object(module, 'make_forge', return_value=forge), redirect_stdout(stdout):
            module.main(['get', '/items'])
        self.assertEqual(stdout.getvalue().splitlines()[0], '403')
        self.assertIn('API 请求失败', stdout.getvalue())
        self.assertNotIn('敏感响应', stdout.getvalue())

    def test_file_only_credentials_render_all_fixer_templates(self):
        import tempfile
        spec = importlib.util.spec_from_file_location('install_render', ROOT / 'install/render.py')
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as directory:
            base = Path(directory)
            text = (ROOT / 'examples/projects/example-gitlab/project.toml').read_text()
            text = text.replace('env = "GITLAB_TOKEN"', 'file = "/home/agent/token-file"')
            text = text.replace('dotenv_file = "/home/agent/.hermes/.env"', '')
            config = base / 'project.toml'
            config.write_text(text)
            renderer.render_project(config, base / 'output')
            for name in ('SKILL.md', 'dispatcher.md', 'rules.md'):
                with self.subTest(template=name):
                    self.assertIn('/home/agent/token-file', (base / 'output/fixer' / name).read_text())


class GitHubApiVerbTests(unittest.TestCase):
    def test_patch_is_available_for_github_issue_and_pr_updates(self):
        import contextlib
        import io
        from unittest.mock import patch
        module = script_module('forge_api')
        fake = type('Forge', (), {})()
        calls = []
        class Transport:
            def request_with_status(self, method, path, body):
                calls.append((method, path, body))
                return 200, {'state': 'closed'}
        fake.transport = Transport()
        with patch.object(module, 'configuration', return_value={}), patch.object(module, 'make_forge', return_value=fake), patch('sys.stdin', io.StringIO('{"state":"closed"}')), contextlib.redirect_stdout(io.StringIO()):
            module.main(['patch', '/repos/o/r/issues/1', '-'])
        self.assertEqual(calls, [('PATCH', '/repos/o/r/issues/1', {'state': 'closed'})])
