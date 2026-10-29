"""虚构项目完整产物的离线验收。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/projects/example-gitlab'


def renderer():
    spec = importlib.util.spec_from_file_location('installer_render', ROOT / 'install/render.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / 'build').mkdir(exist_ok=True)

    def test_example_outputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as directory:
            renderer().render_project(EXAMPLE, Path(directory))
            result = Path(directory)
            settings = json.loads((result / 'reviewer/settings.json').read_text())
            self.assertIn('Bash(ssh:*)', settings['permissions']['deny'])
            self.assertIn('*/3 * * * *', (result / 'reviewer/crontab.line').read_text())
            rules = (result / 'fixer/rules.md').read_text()
            self.assertIn('example-gitlab', rules)
            self.assertIn('同一项往返 3 轮', rules)
            self.assertEqual(len(json.loads((result / 'fixer/cron-jobs.json').read_text())['jobs']), 3)
            self.assertTrue((result / 'reviewer/kit/forge.py').is_file())
            self.assertTrue((result / 'fixer/scripts/kit/forge.py').is_file())
            self.assertFalse((result / 'PARITY.md').exists())

    def test_render_rejects_output_over_source(self):
        with self.assertRaises(ValueError):
            renderer().render_project(EXAMPLE, ROOT)

    def test_disabling_image_build_removes_stale_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as directory:
            output = Path(directory) / 'output'
            stale = output / 'fixer'
            stale.mkdir(parents=True)
            (stale / 'build-image.sh').write_text('旧构建脚本')
            (stale / 'registry.example.com.crt').write_text('旧证书')
            renderer().render_project(EXAMPLE, output)
            self.assertFalse((output / 'fixer/build-image.sh').exists())
            self.assertFalse((output / 'fixer/registry.example.com.crt').exists())

    def test_fixer_receives_referenced_review_tools(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as directory:
            renderer().render_project(EXAMPLE, Path(directory))
            self.assertEqual((Path(directory) / 'fixer/claude-ref/complexity.py').read_bytes(), (ROOT / 'reviewer/complexity.py').read_bytes())
            # 写作规则已并入唯一执行规则，不再单独安装另一份规则。
            self.assertFalse((Path(directory) / 'fixer/claude-ref/writing-rules.md').exists())

    def test_approval_deny_keeps_shared_host_and_project_boundaries(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as directory:
            renderer().render_project(EXAMPLE, Path(directory))
            deny = tomllib.loads((Path(directory) / 'fixer/config/approvals.toml').read_text())['approvals']['deny']
            for forbidden in ('*docker push*', '*--push*', '*systemctl restart*', '*systemctl stop*',
                              '*docker rm *', '*/etc/systemd/*', '*kubectl delete namespace*',
                              '*kubectl delete*--all-namespaces*', '*git push*--force*',
                              '*git push*origin main*', '*git push*:main*',
                              '*-n example-gitlab-system*', '*--namespace example-gitlab-system*'):
                with self.subTest(forbidden=forbidden):
                    self.assertIn(forbidden, deny)

    def test_github_and_file_only_credentials_render(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as directory:
            base = Path(directory)
            text = (ROOT / 'examples/projects/example-github/project.toml').read_text()
            text = text.replace('env = "GITHUB_TOKEN"', 'file = "/home/agent/token"').replace('dotenv_file = "/home/agent/.hermes/.env"', '')
            config = base / 'github.toml'
            config.write_text(text)
            renderer().render_project(config, base / 'output')
            self.assertIn('GitHub', (base / 'output/fixer/dispatcher.md').read_text())
            self.assertIn('/home/agent/token', (base / 'output/fixer/dispatcher.md').read_text())
