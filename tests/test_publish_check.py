"""公开发布检查：只使用临时本地仓库与虚构规则。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / 'install' / 'publish_check.py'
HOOK = ROOT / 'hooks' / 'pre-push.sample'


class PublishCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.deny = self.base / 'private-deny.txt'
        self.deny.write_text('# 虚构示例\n\nPRIVATE_[A-Z]+\n', encoding='utf-8')
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', '发布测试')
        self.write('readme.md', '公开说明\n')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', '本地测试')

    def check(self):
        return subprocess.run([sys.executable, str(CHECKER), '--deny-file', str(self.deny)],
                              cwd=self.repo, text=True, capture_output=True)

    def test_clean_workspace_reports_zero_matches(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('零命中', result.stdout)

    def test_untracked_content_is_scanned_and_gitignored_build_is_skipped(self):
        # 被 .gitignore 忽略的文件不会被推送；强行加入跟踪后会在已跟踪文件扫描中命中
        self.write('.gitignore', 'build/\n')
        self.write('untracked.txt', '公开\nPRIVATE_DRAFT\n')
        self.write('build/rendered.txt', 'PRIVATE_RENDER\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('untracked.txt:2', result.stdout)
        self.assertNotIn('build/rendered.txt', result.stdout)
        self.assertNotIn('PRIVATE_DRAFT', result.stdout)

    def test_sensitive_path_is_reported_without_sensitive_content(self):
        self.write('PRIVATE_FILENAME/clean.txt', '公开\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PRIVATE_FILENAME/clean.txt:0', result.stdout)

    def test_committed_addition_cannot_be_hidden_by_clean_worktree(self):
        self.write('new.txt', '公开\nPRIVATE_COMMITTED\n')
        self.commit()
        self.write('new.txt', '公开\n已修改工作区\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('new.txt:2', result.stdout)
        self.assertIn('origin/main..HEAD', result.stdout)

    def test_staged_content_cannot_be_hidden_by_clean_worktree(self):
        self.write('staged.txt', 'PRIVATE_STAGED\n')
        self.git('add', 'staged.txt')
        self.write('staged.txt', '公开\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('索引 staged.txt:1', result.stdout)

    def test_new_lines_starting_with_plus_keep_correct_line_numbers(self):
        self.write('plus.txt', '第一行\n+++PRIVATE_ADDED\n')
        self.commit()
        self.write('plus.txt', '安全\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('origin/main..HEAD plus.txt:2', result.stdout)

    def test_sensitive_old_name_of_renamed_file_does_not_block_cleanup(self):
        self.write('PRIVATE_OLD.txt', '公开\n')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('mv', 'PRIVATE_OLD.txt', 'public.txt')
        self.commit()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unchanged_head_files_are_scanned(self):
        self.write('baseline.txt', 'PRIVATE_BASELINE\n')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.write('baseline.txt', '工作区已改为公开\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('HEAD baseline.txt:1', result.stdout)

    def test_deleted_old_content_and_path_do_not_block_cleanup_commit(self):
        self.write('PRIVATE_OLD.txt', 'PRIVATE_OLD\n')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('rm', 'PRIVATE_OLD.txt')
        self.commit()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('零命中', result.stdout)

    def test_deleted_lines_of_modified_file_do_not_block_cleanup_commit(self):
        self.write('cleaned.txt', 'PRIVATE_OLD\n公开\n')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.write('cleaned.txt', '公开\n')
        self.commit()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_origin_scans_head_and_announces_offline_fallback(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.write('hidden.txt', 'PRIVATE_HEAD\n')
        self.commit()
        self.write('hidden.txt', '公开\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('origin/main', result.stdout)
        self.assertIn('离线降级', result.stdout)
        self.assertIn('HEAD hidden.txt:1', result.stdout)

    def test_missing_head_file_is_pending_deletion_even_without_origin(self):
        self.write('PRIVATE_OLD.txt', 'PRIVATE_OLD\n')
        self.commit()
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        (self.repo / 'PRIVATE_OLD.txt').unlink()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('零命中', result.stdout)

    def test_cache_git_metadata_and_binary_are_ignored(self):
        self.write('.git/private-note', 'PRIVATE_METADATA\n')
        self.write('__pycache__/cached.txt', 'PRIVATE_CACHE\n')
        self.write('.pytest_cache/private.txt', 'PRIVATE_CACHE\n')
        (self.repo / 'binary.dat').write_bytes(b'\x00PRIVATE_BINARY\xff')
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_symlink_does_not_read_external_target(self):
        (self.base / 'outside.txt').write_text('PRIVATE_OUTSIDE\n')
        (self.repo / 'link.txt').symlink_to('../outside.txt')
        self.commit()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_gitlink_checks_path_without_reading_a_commit_as_a_file(self):
        self.write('deleted.txt', 'PRIVATE_HISTORY\n')
        self.commit()
        self.git('rm', 'deleted.txt')
        self.commit()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        revision = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.git('update-index', '--add', '--cacheinfo', f'160000,{revision},vendor')
        self.git('commit', '-qm', '添加 gitlink')
        (self.repo / 'vendor').mkdir()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_regex_fails_without_disclosing_pattern(self):
        self.deny.write_text('# 注释\nPRIVATE_INVALID[\n', encoding='utf-8')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('第 2 行', result.stderr)
        self.assertIn('正则', result.stderr)
        self.assertNotIn('PRIVATE_INVALID', result.stdout + result.stderr)

    def test_missing_deny_file_fails_without_absolute_path(self):
        self.deny.unlink()
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('规则文件', result.stderr)
        self.assertNotIn(str(self.base), result.stdout + result.stderr)

    def test_hook_requires_private_deny_file(self):
        env = os.environ.copy()
        env.pop('ARL_DENY_FILE', None)
        result = subprocess.run(['sh', str(HOOK)], cwd=self.repo, env=env,
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ARL_DENY_FILE', result.stderr)

    def test_hook_runs_checker_with_external_rule_file(self):
        (self.repo / 'hooks').mkdir()
        (self.repo / 'install').mkdir()
        shutil.copy2(HOOK, self.repo / 'hooks' / 'pre-push.sample')
        shutil.copy2(CHECKER, self.repo / 'install' / 'publish_check.py')
        self.write('leak.txt', 'PRIVATE_HOOK\n')
        env = dict(os.environ, ARL_DENY_FILE=str(self.deny))
        result = subprocess.run(['sh', str(self.repo / 'hooks' / 'pre-push.sample')],
                                cwd=self.repo, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('leak.txt:1', result.stdout)


if __name__ == '__main__':
    unittest.main()
