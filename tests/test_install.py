"""安装过程的副作用和配置保留测试。"""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'install' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallTests(unittest.TestCase):
    def test_cron_only_replaces_owned_line(self):
        mod = load_module('install_reviewer')
        text = '0 1 * * * other\n*/3 * * * * old # agent-review-loop:p\n'
        actual = mod.merge_crontab(text, '*/5 * * * * new', 'p')
        self.assertEqual(actual, '0 1 * * * other\n*/5 * * * * new # agent-review-loop:p\n')

    def test_yaml_merge_preserves_other_agent_settings_and_top_level(self):
        mod = load_module('install_fixer')
        original = 'model: local\napprovals:\n  mode: ask\nagent:\n  max_turns: 20\n  temperature: 0.3\nother:\n  enabled: true\n'
        fragment = 'approvals:\n  mode: "off"\ndelegation:\n  worktree_isolation: true\nagent:\n  max_turns: 90\n'
        actual = mod.merge_config(original, fragment)
        self.assertIn('temperature: 0.3', actual)
        self.assertIn('max_turns: 90', actual)
        self.assertIn('model: local', actual)
        self.assertIn('other:\n  enabled: true', actual)
        self.assertNotIn('max_turns: 20', actual)

    def test_yaml_merge_rejects_unrelated_patch_keys(self):
        mod = load_module('install_fixer')
        with self.assertRaises(ValueError):
            mod.merge_config('model: local\n', 'model: changed\n')

    def test_reviewer_dry_run_never_mutates_or_runs_commands(self):
        mod = load_module('install_reviewer')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            root = Path(temp)
            source = root / 'build' / 'reviewer'
            source.mkdir(parents=True)
            (source / 'poll.py').write_text('新文件\n')
            (source / 'crontab.line').write_text('* * * * * python poll.py\n')
            target = root / 'target'
            target.mkdir()
            (target / 'poll.py').write_text('旧文件\n')
            config = {'project': {'name': 'p'}, 'reviewer': {'install_dir': str(target), 'ref_dir': str(root / 'refs')}}
            with patch.object(mod.subprocess, 'run', side_effect=AssertionError('禁止调用')), contextlib.redirect_stdout(io.StringIO()) as output:
                mod.install(config, root / 'build', dry_run=True)
            self.assertEqual((target / 'poll.py').read_text(), '旧文件\n')
            self.assertIn('-旧文件', output.getvalue())
            self.assertFalse((root / 'refs').exists())

    def test_fixer_dry_run_never_runs_ssh(self):
        mod = load_module('install_fixer')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            root = Path(temp)
            source = root / 'fixer'
            source.mkdir()
            (source / 'hermes-config-patch.yaml').write_text('agent:\n  max_turns: 90\n')
            (source / 'cron-jobs.json').write_text('{"jobs": []}')
            config = {'project': {'name': 'p'}, 'fixer': {'host': 'example.invalid', 'hermes_home': '/tmp/hermes', 'review_dir': '/tmp/review', 'rules_file': '/tmp/ref/rules.md'}}
            with patch.object(mod.subprocess, 'run', side_effect=AssertionError('禁止调用')), contextlib.redirect_stdout(io.StringIO()) as output:
                mod.install(config, root, dry_run=True)
            self.assertIn('ssh', output.getvalue())
            self.assertIn('正式安装时读取', output.getvalue())

if __name__ == '__main__':
    unittest.main()


class InstallationReviewTests(unittest.TestCase):
    def test_agent_four_space_indentation_retains_one_max_turns(self):
        mod = load_module('install_fixer')
        original = 'agent:\n    max_turns: 10\n    temperature: 0.3\n'
        merged = mod.merge_config(original, 'agent:\n  max_turns: 90\n')
        self.assertEqual(merged, 'agent:\n    max_turns: 90\n    temperature: 0.3\n')

    def test_agent_duplicate_and_complex_yaml_rejected(self):
        mod = load_module('install_fixer')
        for original in ('agent:\n    max_turns: 10\n    max_turns: 20\n', 'agent:\n    <<: *defaults\n', 'agent:\n    settings: {max_turns: 20}\n'):
            with self.subTest(original=original), self.assertRaises(ValueError):
                mod.merge_config(original, 'agent:\n  max_turns: 90\n')

    def test_remote_install_copies_all_reference_files(self):
        import base64
        import json
        import tarfile
        mod = load_module('install_fixer')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp).resolve()
            package = root / 'package'
            (package / 'claude-ref').mkdir(parents=True)
            (package / 'scripts').mkdir()
            for name in ('complexity.py', 'standards.md', 'CHANGELOG.md'):
                (package / 'claude-ref' / name).write_text('参考文件：' + name)
            (package / 'hermes-config-patch.yaml').write_text('agent:\n  max_turns: 90\n')
            (package / 'cron-jobs.json').write_text('{"jobs": []}')
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w:gz') as archive:
                for path in package.rglob('*'):
                    if path.is_file():
                        archive.add(path, arcname=str(path.relative_to(package)))
            conf = {'hermes_home': str(root / 'home'), 'review_dir': str(root / 'review'), 'ref_dir': str(root / 'different-refs')}
            def fake_run(argv, **kwargs):
                output = '[]' if '--json' in argv else '--json --name --schedule'
                return type('Result', (), {'stdout': output})()
            with patch('subprocess.run', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                mod.remote_install({'config': conf, 'archive': base64.b64encode(data.getvalue()).decode()})
            for name in ('complexity.py', 'standards.md', 'CHANGELOG.md'):
                self.assertEqual((Path(conf['ref_dir']) / name).read_text(), '参考文件：' + name)


class DoctorTests(unittest.TestCase):
    def test_github_protection_rejects_missing_human_approval(self):
        mod = load_module('doctor')
        self.assertFalse(mod.check_protection('github', {'required_pull_request_reviews': {'required_approving_review_count': 0}}, 'fixer')[0])

    def test_gitlab_protection_rejects_developer_push(self):
        mod = load_module('doctor')
        protection = {'push_access_levels': [{'access_level': 30}], 'merge_access_levels': [{'access_level': 40}]}
        self.assertFalse(mod.check_protection('gitlab', protection, 'fixer')[0])

    def test_token_permissions_checked_without_printing_secret(self):
        mod = load_module('doctor')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            path = Path(temp) / 'token'
            path.write_text('测试凭据内容')
            path.chmod(0o600)
            ok, detail = mod.check_token_reference({'file': str(path)})
            self.assertTrue(ok)
            self.assertNotIn('测试凭据内容', detail)
            path.chmod(0o644)
            self.assertFalse(mod.check_token_reference({'file': str(path)})[0])

    def test_fixer_api_probe_uses_remote_stdin_and_script_compiles(self):
        mod = load_module('doctor')
        config = {'fixer': {'host': 'example.invalid'}}
        captured = {}
        def fake_run(argv, **kwargs):
            captured['argv'] = argv
            captured['code'] = kwargs['input']
            compile(kwargs['input'], '<远端检查>', 'exec')
            return type('Result', (), {'stdout': '{"login": "bot"}'})()
        with patch.object(mod.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(mod.remote_snapshot(config)['login'], 'bot')
        self.assertEqual(captured['argv'][-1], 'python3 -')
        self.assertIn('role_snapshot', captured['code'])


class ProtectionReviewTests(unittest.TestCase):
    def test_github_bypass_allowances_require_manual_review(self):
        mod = load_module('doctor')
        for category in ('users', 'teams', 'apps'):
            protection = {'required_pull_request_reviews': {'required_approving_review_count': 1, 'bypass_pull_request_allowances': {category: [{'id': 1}]}}, 'enforce_admins': {'enabled': True}}
            ok, detail = mod.check_protection('github', protection, 'fixer')
            with self.subTest(category=category):
                self.assertFalse(ok)
                self.assertIn('人工核实', detail)


class CredentialReviewTests(unittest.TestCase):
    def test_remote_token_value_error_does_not_return_secret(self):
        mod = load_module('doctor')
        config = {'fixer': {'host': 'example.invalid'}}
        original_temp = tempfile.TemporaryDirectory
        def fake_run(argv, **kwargs):
            output = io.StringIO()
            def repository_temp(*args, **options):
                options['dir'] = str(Path(__file__).parent)
                return original_temp(*args, **options)
            with patch('tempfile.TemporaryDirectory', side_effect=repository_temp), patch('kit.forge.make_forge', side_effect=ValueError('禁止回显的凭据')), contextlib.redirect_stdout(output):
                exec(compile(kwargs['input'], '<远端检查>', 'exec'), {})
            self.assertNotIn('禁止回显的凭据', output.getvalue())
            return type('Result', (), {'stdout': output.getvalue()})()
        with patch.object(mod.subprocess, 'run', side_effect=fake_run):
            result = mod.remote_snapshot(config)
        self.assertEqual(result, {'error': 'ValueError'})


class FinalInstallReviewTests(unittest.TestCase):
    def test_matching_legacy_unmarked_cron_is_replaced(self):
        mod = load_module('install_reviewer')
        original = '0 1 * * * other\n*/3 * * * * python poll.py\n'
        merged = mod.merge_crontab(original, '*/3 * * * * python poll.py # agent-review-loop:p', 'p')
        self.assertEqual(merged, '0 1 * * * other\n*/3 * * * * python poll.py # agent-review-loop:p\n')

    def test_reviewer_skill_target_is_in_preview(self):
        mod = load_module('install_reviewer')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            source = root / 'build' / 'reviewer'
            source.mkdir(parents=True)
            (source / 'SKILL.md').write_text('技能文件\n')
            (source / 'crontab.line').write_text('* * * * * python poll.py\n')
            config = {'project': {'name': 'p'}, 'reviewer': {'install_dir': str(root / 'install'), 'ref_dir': str(root / 'ref'), 'skill_dir': str(root / 'skill')}}
            with patch.object(mod.subprocess, 'run', side_effect=AssertionError('禁止调用')), contextlib.redirect_stdout(io.StringIO()) as output:
                mod.install(config, root / 'build', dry_run=True)
            self.assertIn(str(root / 'skill' / 'SKILL.md'), output.getvalue())
            self.assertFalse((root / 'skill').exists())

    def test_fixer_preview_lists_remote_mappings_and_hash(self):
        mod = load_module('install_fixer')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            source = root / 'fixer'
            (source / 'scripts').mkdir(parents=True)
            (source / 'scripts' / 'notify.py').write_text('print("通知")\n')
            (source / 'hermes-config-patch.yaml').write_text('agent:\n  max_turns: 90\n')
            (source / 'cron-jobs.json').write_text('{"jobs": []}')
            config = {'fixer': {'host': 'example.invalid', 'hermes_home': '/remote/hermes', 'review_dir': '/remote/review'}}
            with patch.object(mod.subprocess, 'run', side_effect=AssertionError('禁止调用')), contextlib.redirect_stdout(io.StringIO()) as output:
                mod.install(config, root, dry_run=True)
            self.assertIn('/remote/hermes/scripts/notify.py', output.getvalue())
            self.assertIn('sha256=', output.getvalue())
            self.assertIn('远端内容未知', output.getvalue())


class FinalConfigAndStateTests(unittest.TestCase):
    def test_quoted_top_level_key_is_preserved(self):
        mod = load_module('install_fixer')
        original = 'approvals:\n  mode: ask\n"model": local-model\ndelegation:\n  worktree_isolation: false\n'
        merged = mod.merge_config(original, 'approvals:\n  mode: "off"\n')
        self.assertIn('"model": local-model\n', merged)

    def test_unrecognized_top_level_yaml_is_rejected(self):
        mod = load_module('install_fixer')
        for unknown in ('? [complex, key]\n: value\n', '---\n', '"escaped\\key": value\n'):
            with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                mod.merge_config('approvals:\n  mode: ask\n' + unknown, 'approvals:\n  mode: "off"\n')

    def test_fresh_install_initializes_state_and_upgrade_keeps_records(self):
        import base64
        import tarfile
        mod = load_module('install_fixer')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp).resolve()
            package = root / 'package'
            package.mkdir()
            (package / 'hermes-config-patch.yaml').write_text('agent:\n  max_turns: 90\n')
            (package / 'cron-jobs.json').write_text('{"jobs": []}')
            archive_data = io.BytesIO()
            with tarfile.open(fileobj=archive_data, mode='w:gz') as archive:
                for path in package.iterdir():
                    archive.add(path, arcname=path.name)
            conf = {'hermes_home': str(root / 'home'), 'review_dir': str(root / 'review'), 'scratch_dir': str(root / 'scratch'), 'notify_state_file': str(root / 'notify' / 'state.json'), 'token_notice_file': str(root / 'expiry' / 'notice')}
            payload = {'config': conf, 'archive': base64.b64encode(archive_data.getvalue()).decode()}
            def fake_run(argv, **kwargs):
                return type('Result', (), {'stdout': '[]' if '--json' in argv else '--json --name --schedule'})()
            with patch('subprocess.run', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                mod.remote_install(payload)
            for name in ('findings.md', 'reviewed-files.txt'):
                path = root / 'review' / name
                self.assertEqual(path.read_text(), '')
                path.write_text('已有记录：' + name)
            for key in ('scratch_dir', 'notify_state_file', 'token_notice_file'):
                directory = Path(conf[key]) if key == 'scratch_dir' else Path(conf[key]).parent
                self.assertTrue(directory.is_dir())
            with patch('subprocess.run', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                mod.remote_install(payload)
            for name in ('findings.md', 'reviewed-files.txt'):
                self.assertEqual((root / 'review' / name).read_text(), '已有记录：' + name)


class HermesHomeGuardTests(unittest.TestCase):
    def test_existing_other_project_is_rejected_before_copying(self):
        import base64
        import tarfile
        mod = load_module('install_fixer')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp).resolve()
            existing = root / 'home' / 'scripts' / 'project.toml'
            existing.parent.mkdir(parents=True)
            existing.write_text('[project]\nname = "old-project"\n')
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w:gz') as archive:
                entries = {'project.toml': b'[project]\nname = "new-project"\n', 'cron-jobs.json': b'{"jobs": []}', 'hermes-config-patch.yaml': b'agent:\n  max_turns: 90\n'}
                for name, content in entries.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            def fake_run(argv, **kwargs):
                return type('Result', (), {'stdout': '[]' if '--json' in argv else '--json --name --schedule'})()
            conf = {'hermes_home': str(root / 'home'), 'review_dir': str(root / 'review')}
            with patch('subprocess.run', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, '其他项目'):
                mod.remote_install({'config': conf, 'archive': base64.b64encode(data.getvalue()).decode()})
            self.assertEqual(existing.read_text(), '[project]\nname = "old-project"\n')


class RegisterCronTests(unittest.TestCase):
    def test_two_marked_lines_update_idempotently_and_remove_disabled_register(self):
        mod = load_module('install_reviewer')
        old = '0 1 * * * other\n*/3 * * * * old # agent-review-loop:p\n0 21 * * * old-register # agent-review-loop:p:register\n0 2 * * * other-project # agent-review-loop:q:register\n'
        lines = '*/5 * * * * poll # agent-review-loop:p\n0 22 * * * register # agent-review-loop:p:register\n'
        result = mod.merge_crontab(old, lines, 'p')
        self.assertEqual(mod.merge_crontab(result, lines, 'p'), result)
        self.assertEqual(result.count('# agent-review-loop:p\n'), 1)
        self.assertEqual(result.count('# agent-review-loop:p:register\n'), 1)
        self.assertIn('other-project', result)
        disabled = mod.merge_crontab(result, lines.splitlines()[0], 'p')
        self.assertNotIn('# agent-review-loop:p:register', disabled)


class AutoMergeDoctorTests(unittest.TestCase):
    def test_github_comment_consent_allows_zero_approve_with_pr_protection(self):
        mod = load_module('doctor')
        protection = {'required_pull_request_reviews': {'required_approving_review_count': 0},
                      'enforce_admins': {'enabled': True}}
        ok, text = mod.check_protection('github', protection, 'reviewer', auto_merge=True)
        self.assertTrue(ok)
        self.assertIn('成员评论同意', text)
        protection['required_pull_request_reviews']['required_approving_review_count'] = 1
        ok, text = mod.check_protection('github', protection, 'reviewer', auto_merge=True)
        self.assertFalse(ok)
        self.assertIn('自动合入', text)
