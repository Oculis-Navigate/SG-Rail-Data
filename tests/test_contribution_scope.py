"""Exercise the actual local hooks without running a live pipeline."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ContributionScopeTests(unittest.TestCase):
    def test_regular_and_mixed_commits_and_pushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(Path(__file__).resolve().parents[1] / '.githooks', root / '.githooks')
            # A hook trying to run a dataset command will fail with this sentinel.
            uv = root / 'uv'
            uv.write_text('#!/bin/sh\nexit 72\n')
            uv.chmod(0o755)
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']}

            def cmd(*args, stdin=None, check=True):
                return subprocess.run(args, cwd=root, env=env, text=True, capture_output=True,
                                      input=stdin, check=check)

            def commit():
                cmd('git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=test', '-c',
                    'user.email=test@example.org', 'commit', '--allow-empty', '-qm', 'test')
                return cmd('git', 'rev-parse', 'HEAD').stdout.strip()

            cmd('git', 'init', '-q')
            base = commit()
            cmd('git', 'update-ref', 'refs/remotes/origin/main', base)
            (root / 'notes.md').write_text('ordinary docs')
            cmd('git', 'add', 'notes.md')
            cmd('sh', '.githooks/pre-commit')
            regular = commit()
            zero = '0' * 40
            cmd('sh', '.githooks/pre-push', stdin=f'refs/heads/topic {regular} refs/heads/topic {zero}\n')
            (root / 'config').mkdir()
            (root / 'config/stations.json').write_text('{}')
            (root / 'notes.md').write_text('mixed data and docs')
            cmd('git', 'add', 'config', 'notes.md')
            self.assertEqual(cmd('sh', '.githooks/change-scope', '--cached').stdout.strip(), 'dataset')
            self.assertEqual(cmd('sh', '.githooks/pre-commit', check=False).returncode, 72)
            mixed = commit()
            self.assertEqual(cmd('sh', '.githooks/pre-push', check=False,
                                 stdin=f'refs/heads/topic {mixed} refs/heads/topic {regular}\n').returncode, 72)
            (root / 'audit').mkdir()
            (root / 'audit/decision-ledger.jsonl').write_text('evidence')
            cmd('git', 'add', 'audit')
            self.assertEqual(cmd('sh', '.githooks/change-scope', '--cached').stdout.strip(), 'audit')
