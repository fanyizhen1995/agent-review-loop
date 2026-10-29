"""配置错误应在执行部署和定时任务前定位。"""
from pathlib import Path
import tempfile
import unittest
from kit.config import load_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def load_modified(self, old, new):
        original = (ROOT / 'examples/projects/example-gitlab/project.toml').read_text()
        self.assertIn(old, original, '配置替换目标不存在')
        text = original.replace(old, new, 1)
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            path = Path(directory) / 'p.toml'
            path.write_text(text)
            return load_config(path)

    def test_reject_cron_project_name_injection(self):
        with self.assertRaisesRegex(ValueError, 'project.name'):
            self.load_modified('name = "example-gitlab"', 'name = "bad\\n* * * * * command"')

    def test_reject_negative_gitlab_project(self):
        with self.assertRaisesRegex(ValueError, 'forge.project'):
            self.load_modified('project = 123', 'project = -1')

    def test_missing_credential_reference_is_early_error(self):
        with self.assertRaisesRegex(ValueError, 'credentials.reviewer'):
            self.load_modified('file = "~/.config/example-gitlab-review/token"', '')

    def test_source_label_default(self):
        cfg = self.load_modified('source = "source::agent"', '')
        self.assertEqual(cfg['labels']['source'], 'source::agent')

    def test_unknown_credential_key_rejected(self):
        with self.assertRaisesRegex(ValueError, 'credentials.reviewer'):
            self.load_modified('[credentials.reviewer]', '[credentials.reviewer]\naccess_token = "误填的内容"')

    def test_remote_tilde_uses_remote_home(self):
        cfg = self.load_modified('repo = "/home/agent/hermes/example-gitlab"', 'repo = "~/hermes/example-gitlab"')
        self.assertEqual(cfg['fixer']['repo'], '/home/agent/hermes/example-gitlab')

    def test_missing_runtime_path_is_early_error(self):
        with self.assertRaisesRegex(ValueError, 'reviewer.run_root'):
            self.load_modified('run_root = "/home/agent/example-gitlab-remote/.worktrees/hermes-review-runs"', '')

    def test_negative_quiet_period_rejected(self):
        with self.assertRaisesRegex(ValueError, 'quiet_minutes'):
            self.load_modified('quiet_minutes = 20', 'quiet_minutes = -1')
