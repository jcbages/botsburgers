import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / 'hooks/session_ledger.sh'


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 't@t')
        self.git('config', 'user.name', 't')
        self.file = self.root / 'shared'
        self.file.write_text(''.join(f'line{i}\n' for i in range(40)))
        self.git('add', '.')
        self.git('commit', '-qm', 'base')

    def run_command(self, *args, env=None):
        return subprocess.check_output(args, cwd=self.root, env=env, text=True, stderr=subprocess.STDOUT)

    def git(self, *args):
        return self.run_command('git', *args)

    def payload(self, sid='A', tool='Edit', **args):
        return dict(session_id=sid, cwd=str(self.root), tool_use_id=sid + '-call', tool_name=tool, tool_input=args)

    def hook(self, mode, payload):
        return subprocess.check_output([str(HOOK), mode], input=json.dumps(payload), cwd=self.root, text=True)

    def edit(self, old, new, sid='A', path='shared'):
        payload = self.payload(sid, file_path=path, old_string=old, new_string=new)
        self.hook('pre', payload)
        file = self.root / path
        file.write_text(file.read_text().replace(old, new, 1))
        self.hook('post', payload)

    def command(self, command, sid='A', *args):
        return self.run_command(str(ROOT / 'skills' / 'gene' / 'scripts' / command), *args, env=dict(os.environ, LEDGER_SESSION=sid))

    def test_read_never_owns_concurrent_writer(self):
        payload = self.payload('reader', 'Bash', command='cat shared')
        self.hook('pre', payload)
        self.file.write_text('someone else\n')
        self.hook('post', payload)
        self.assertIn('no verified explicit edits', self.command('mine', 'reader', '--files'))

    def test_two_sessions_commit_only_their_edits(self):
        self.edit('line10\n', 'line10 A\n')
        self.edit('line13\n', 'line13 B\n', 'B')
        self.assertNotIn('+line13 B', self.command('mine', 'A'))
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('+line10 A', self.git('show', '--format=', '-U0'))
        self.assertNotIn('line13 B', self.git('show', '--format=', '-U0'))
        # B's hunk is still only in the work tree, never staged as a revert of A.
        self.assertEqual(' M shared\n', self.git('status', '--porcelain'))
        self.command('commit-mine', 'B', '-m', 'B')
        self.assertIn('+line13 B', self.git('show', '--format=', '-U0'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_commit_leaves_shared_index_at_new_head(self):
        self.edit('line10\n', 'line10 A\n')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertEqual('', self.git('diff', '--cached', '--name-only'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_staged_version_of_landed_path_is_kept(self):
        self.edit('line10\n', 'line10 A\n')
        staged = self.root / 'staged'
        staged.write_text('STAGED BY OTHER\n')
        blob = self.git('hash-object', '-w', 'staged').strip()
        staged.unlink()
        self.git('update-index', '--cacheinfo', f'100644,{blob},shared')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('line10 A', self.git('show', 'HEAD:shared'))
        self.assertNotIn('STAGED BY OTHER', self.git('show', 'HEAD:shared'))
        self.assertEqual('STAGED BY OTHER\n', self.git('show', ':shared'))
        self.assertIn('shared index not updated', output)

    def test_own_staging_is_not_reported(self):
        self.edit('line10\n', 'line10 A\n')
        self.git('add', 'shared')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertNotIn('shared index not updated', output)
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_index_flags_are_kept(self):
        self.git('update-index', '--assume-unchanged', 'shared')
        self.edit('line10\n', 'line10 A\n')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('line10 A', self.git('show', 'HEAD:shared'))
        self.assertIn('shared index not updated', output)
        self.assertTrue(self.git('ls-files', '-v', 'shared').startswith('h '))

    def test_busy_index_lands_commit_and_leaves_lock(self):
        self.edit('line10\n', 'line10 A\n')
        lock = self.root / '.git' / 'index.lock'
        lock.write_text('')
        index = self.git('show', ':shared')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('line10 A', self.git('show', 'HEAD:shared'))
        self.assertIn('shared — index busy', output)
        self.assertTrue(lock.exists())
        lock.unlink()
        self.assertEqual(index, self.git('show', ':shared'))

    def test_concurrent_staging_during_sync_is_refused_not_overwritten(self):
        # A wrapper stages for another writer at the moment commit-mine writes its
        # locked copy. The other writer must get git's lock error, not lose silently.
        self.edit('line10\n', 'line10 A\n')
        real_git = shutil.which('git')
        other = self.root / '.other-blob'
        other.write_text('B STAGED\n')
        blob = self.git('hash-object', '-w', '.other-blob').strip()
        other.unlink()
        bin_dir = Path(self.temp.name + '-bin')
        bin_dir.mkdir()
        self.addCleanup(shutil.rmtree, bin_dir)
        result = bin_dir / 'result'
        wrapper = bin_dir / 'git'
        wrapper.write_text(
            '#!/bin/sh\n'
            'case "${GIT_INDEX_FILE:-shared}" in *commit-mine-index*) ;; *)\n'
            '  if [ "$1" = update-index ] && [ "$2" = --add ]; then\n'
            f'    env -u GIT_INDEX_FILE "{real_git}" update-index --cacheinfo "100644,{blob},shared" 2>/dev/null\n'
            f'    echo $? > "{result}"\n'
            '  fi ;;\n'
            'esac\n'
            f'exec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
        env = dict(os.environ, LEDGER_SESSION='A', PATH=f'{bin_dir}:{os.environ["PATH"]}')
        self.run_command(str(ROOT / 'skills' / 'gene' / 'scripts' / 'commit-mine'), '-m', 'A', env=env)
        self.assertNotEqual('0', result.read_text().strip())
        self.assertIn('line10 A', self.git('show', ':shared'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def install_hook(self, name, body, directory='.git/hooks'):
        hook = self.root / directory / name
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('#!/bin/sh\n' + body)
        hook.chmod(0o755)

    def test_failing_pre_commit_blocks_and_no_verify_skips_it(self):
        self.edit('line10\n', 'line10 A\n')
        seen = self.root / '.git' / 'seen'
        self.install_hook('pre-commit', f'git diff --cached > "{seen}"\nexit 1\n')
        before = self.git('rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('pre-commit hook failed', failure.exception.output)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.assertIn('+line10 A', seen.read_text())
        self.assertEqual('shared\n', self.command('mine', 'A', '--files'))   # ledger kept
        self.command('commit-mine', 'A', '--no-verify', '-m', 'A')
        self.assertIn('line10 A', self.git('show', 'HEAD:shared'))

    def test_pre_commit_is_not_run_while_others_have_uncommitted_work(self):
        # A restaging or stash-and-restore hook would take or rewrite B's hunk.
        self.edit('line10\n', 'line10 A\n')
        self.edit('line13\n', 'line13 B\n', 'B')
        ran = self.root / '.git' / 'ran'
        self.install_hook('pre-commit', f': > "{ran}"\ngit checkout -- . && git add -u\n')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertFalse(ran.exists())
        self.assertIn('pre-commit hook not run', output)
        self.assertIn('line10 A', self.git('show', 'HEAD:shared'))
        self.assertNotIn('line13 B', self.git('show', 'HEAD:shared'))
        self.assertIn('line13 B', self.file.read_text())

    def test_pre_commit_staging_a_path_outside_the_commit_aborts(self):
        self.edit('line10\n', 'line10 A\n')
        self.install_hook('pre-commit', 'echo generated > other && git add other\n')
        before = self.git('rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('outside this commit', failure.exception.output)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.assertEqual('shared\n', self.command('mine', 'A', '--files'))

    def test_pre_commit_restaging_a_non_ascii_path_lands(self):
        payload = self.payload(tool='Write', file_path='café.txt', content='raw\n')
        self.hook('pre', payload)
        (self.root / 'café.txt').write_text('raw\n')
        self.hook('post', payload)
        self.install_hook('pre-commit', 'echo formatted > café.txt && git add café.txt\n')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('changed by the pre-commit hook', output)
        self.assertEqual('formatted\n', self.git('show', 'HEAD:café.txt'))

    def test_pre_commit_restaging_another_sessions_write_aborts(self):
        # Another session's Edit lands in this file while the hook runs; the hook's
        # `git add -u` must not carry it into this commit.
        self.edit('line10\n', 'line10 A\n')
        self.install_hook('pre-commit',
                          'mkdir -p .git/agent-ledger/B\n'
                          'printf "shared\\t-\\t-\\tverified\\t100644\\n" > .git/agent-ledger/B/state\n'
                          'sed -i.bak "s/^line13$/line13 B/" shared && rm shared.bak && git add -u\n')
        before = self.git('rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('another session wrote it', failure.exception.output)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_stale_ledger_of_a_finished_session_does_not_block_restaging(self):
        self.edit('line10\n', 'line10 A\n')
        stale = self.root / '.git' / 'agent-ledger' / 'OLD'
        stale.mkdir(parents=True)
        (stale / 'state').write_text('shared\t-\t-\tverified\t100644\n')
        self.install_hook('pre-commit', 'sed -i.bak "s/line10 A/line10 FORMATTED/" shared && rm shared.bak && git add shared\n')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('line10 FORMATTED', self.git('show', 'HEAD:shared'))

    def ledger(self, sid):
        return self.root / '.git' / 'agent-ledger' / sid

    def age(self, sid, days):
        old = time.time() - days * 24 * 3600
        for path in [self.ledger(sid), *self.ledger(sid).iterdir()]:
            os.utime(path, (old, old))

    def test_cleanup_drops_entries_committed_by_path(self):
        self.edit('line10\n', 'line10 B\n', 'B')
        self.git('commit', '-qm', 'B by path', '--', 'shared')
        (self.root / 'a.txt').write_text('a\n')
        self.git('add', 'a.txt')
        self.git('commit', '-qm', 'a')
        self.edit('line20\n', 'line20 A\n')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertFalse((self.ledger('B') / 'state').exists())
        self.assertEqual('', self.git('for-each-ref', 'refs/agent-ledger/main/B'))

    def test_cleanup_keeps_another_sessions_uncommitted_work(self):
        self.edit('line10\n', 'line10 A\n')
        self.edit('line13\n', 'line13 B\n', 'B')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('+line13 B', self.command('mine', 'B'))
        self.command('commit-mine', 'B', '-m', 'B')
        self.assertIn('line13 B', self.git('show', 'HEAD:shared'))

    def test_cleanup_removes_abandoned_and_idle_ledgers(self):
        self.edit('line13\n', 'line13 OLD\n', 'OLD')
        self.age('OLD', 31)
        empty = self.ledger('EMPTY')
        empty.mkdir(parents=True)
        (empty / 'lock').write_text('')
        self.age('EMPTY', 2)
        self.edit('line10\n', 'line10 A\n')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertFalse(self.ledger('OLD').exists())
        self.assertFalse(empty.exists())
        self.assertEqual('', self.git('for-each-ref', 'refs/agent-ledger/main/OLD'))
        self.assertTrue((self.ledger('A')).exists())

    def test_cleanup_skips_a_busy_ledger(self):
        self.edit('line10\n', 'line10 B\n', 'B')
        self.git('commit', '-qm', 'B by path', '--', 'shared')
        self.edit('line20\n', 'line20 A\n')
        with (self.ledger('B') / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.command('commit-mine', 'A', '-m', 'A')
        self.assertTrue((self.ledger('B') / 'state').exists())

    def test_damaged_ledger_of_another_session_does_not_block_a_commit(self):
        broken = self.ledger('BROKEN')
        broken.mkdir(parents=True)
        (broken / 'state').write_bytes(b'\xff\xfe not utf-8\n')
        self.edit('line10\n', 'line10 A\n')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('cleanup skipped BROKEN', output)
        self.assertIn('line10 A', self.git('show', 'HEAD:shared'))

    def test_cleanup_waits_for_a_settled_head(self):
        # On a detached HEAD, B's entry can look landed against an unrelated commit.
        self.edit('line10\n', 'line10 B\n', 'B')
        self.git('commit', '-qm', 'B by path', '--', 'shared')
        self.git('checkout', '-q', '--detach')
        self.edit('line20\n', 'line20 A\n')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertTrue((self.ledger('B') / 'state').exists())

    def test_saving_moves_older_refs(self):
        self.git('update-ref', 'refs/agent-ledger/A', 'HEAD^{tree}')
        self.git('update-ref', 'refs/worktree/agent-ledger/A', 'HEAD^{tree}')
        self.edit('line10\n', 'line10 A\n')
        self.assertEqual('', self.git('for-each-ref', 'refs/agent-ledger/A'))
        self.assertEqual('', self.git('for-each-ref', 'refs/worktree/agent-ledger/A'))
        self.assertNotEqual('', self.git('for-each-ref', 'refs/agent-ledger/main/A'))

    def linked_worktree(self):
        linked = Path(self.temp.name + '-linked')
        self.git('worktree', 'add', '-q', '--detach', str(linked))
        self.addCleanup(shutil.rmtree, linked, True)
        git_dir = Path(self.run_command('git', '-C', str(linked), 'rev-parse', '--absolute-git-dir').strip())
        return linked, git_dir

    def linked_edit(self, linked, sid, path, content):
        payload = dict(session_id=sid, cwd=str(linked), tool_use_id=sid + '-' + path, tool_name='Write',
                       tool_input=dict(file_path=path, content=content))
        subprocess.check_output([str(HOOK), 'pre'], input=json.dumps(payload), cwd=linked, text=True)
        (linked / path).write_text(content)
        subprocess.check_output([str(HOOK), 'post'], input=json.dumps(payload), cwd=linked, text=True)

    def test_gc_in_one_worktree_keeps_another_worktrees_ledger_blobs(self):
        linked, git_dir = self.linked_worktree()
        self.edit('line10\n', 'line10 MAIN ONLY\n')
        self.linked_edit(linked, 'W', 'w.txt', 'linked only\n')
        main_shadow = (self.ledger('A') / 'state').read_text().split('\t')[2]
        linked_shadow = (git_dir / 'agent-ledger' / 'W' / 'state').read_text().split('\t')[2]
        self.run_command('git', '-C', str(linked), 'gc', '-q', '--prune=now')
        self.git('cat-file', '-e', main_shadow)
        self.git('gc', '-q', '--prune=now')
        self.git('cat-file', '-e', linked_shadow)

    def test_cleanup_in_a_linked_worktree_keeps_the_main_trees_anchor(self):
        linked, git_dir = self.linked_worktree()
        self.edit('line13\n', 'line13 A\n')                 # main tree's live ledger for A
        anchor = self.git('rev-parse', 'refs/agent-ledger/main/A')
        old = git_dir / 'agent-ledger' / 'A'                 # the same session, long idle in the linked tree
        old.mkdir(parents=True)
        (old / 'lock').write_text('')
        stamp = time.time() - 31 * 24 * 3600
        os.utime(old / 'lock', (stamp, stamp))
        self.linked_edit(linked, 'C', 'y', 'y\n')
        subprocess.check_output([str(ROOT / 'skills/gene/scripts/commit-mine'), '-m', 'C'], cwd=linked,
                                env=dict(os.environ, LEDGER_SESSION='C'), text=True, stderr=subprocess.STDOUT)
        self.assertFalse(old.exists())
        self.assertEqual(anchor, self.git('rev-parse', 'refs/agent-ledger/main/A'))

    def test_a_session_records_again_after_its_ledger_was_removed(self):
        self.edit('line13\n', 'line13 OLD\n', 'OLD')
        self.age('OLD', 31)
        self.edit('line10\n', 'line10 A\n')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertFalse(self.ledger('OLD').exists())
        self.edit('line20\n', 'line20 OLD\n', 'OLD')
        self.assertEqual('shared\n', self.command('mine', 'OLD', '--files'))

    def test_commit_cleanup_strip_removes_comments(self):
        self.edit('line10\n', 'line10 A\n')
        self.git('config', 'commit.cleanup', 'strip')
        self.command('commit-mine', 'A', '-m', 'Subject\n# a comment\n')
        self.assertEqual('Subject\n', self.git('log', '-1', '--format=%B').rstrip('\n') + '\n')

    def test_emptied_message_commits_nothing(self):
        self.edit('line10\n', 'line10 A\n')
        self.install_hook('prepare-commit-msg', ': > "$1"\n')
        before = self.git('rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('message is empty', failure.exception.output)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_commit_msg_hook_edits_or_blocks_the_message(self):
        self.edit('line10\n', 'line10 A\n')
        self.install_hook('commit-msg', 'grep -q BLOCK "$1" && exit 1\necho "Signed-off-by: hook" >> "$1"\n')
        before = self.git('rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError):
            self.command('commit-mine', 'A', '-m', 'BLOCK me')
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.command('commit-mine', 'A', '-m', 'Subject')
        self.assertEqual('Subject\nSigned-off-by: hook\n', self.git('log', '-1', '--format=%B').rstrip('\n') + '\n')

    def test_pre_commit_restaging_lands_and_syncs(self):
        self.edit('line10\n', 'line10 A\n')
        (self.root / 'fmt.sed').write_text('s/line10 A/line10 FORMATTED/\n')
        self.git('add', 'fmt.sed')
        self.git('commit', '-qm', 'formatter')
        self.install_hook('pre-commit', 'sed -i.bak -f fmt.sed shared && rm shared.bak && git add shared\n')
        output = self.command('commit-mine', 'A', '-m', 'A')
        self.assertIn('line10 FORMATTED', self.git('show', 'HEAD:shared'))
        self.assertIn('changed by the pre-commit hook', output)
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_post_commit_runs_and_hooks_path_is_honored(self):
        self.edit('line10\n', 'line10 A\n')
        ran = self.root / '.git' / 'post-ran'
        self.git('config', 'core.hooksPath', 'myhooks')
        self.install_hook('post-commit', f'git rev-parse HEAD > "{ran}"\n', 'myhooks')
        (self.root / 'myhooks' / 'post-commit').chmod(0o755)
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertEqual(self.git('rev-parse', 'HEAD'), ran.read_text())

    def test_signal_during_sync_keeps_shared_index_whole(self):
        for i in range(3):
            (self.root / f'f{i}').write_text('x\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'more')
        tracked = self.git('ls-files')
        self.edit('line10\n', 'line10 A\n')
        payload = self.payload(tool='Write', file_path='zz_new', content='n\n')
        self.hook('pre', payload)
        (self.root / 'zz_new').write_text('n\n')
        self.hook('post', payload)
        bin_dir = Path(self.temp.name + '-bin')
        bin_dir.mkdir()
        self.addCleanup(shutil.rmtree, bin_dir)
        killed = bin_dir / 'killed'
        wrapper = bin_dir / 'git'
        wrapper.write_text(
            '#!/bin/sh\n'
            'case "${GIT_INDEX_FILE:-}" in *commit-mine-sync*)\n'
            f'  if [ "$1" = update-index ] && [ "$2" = --add ] && [ ! -e "{killed}" ]; then\n'
            f'    : > "{killed}"; kill -TERM $PPID\n'
            '  fi ;;\n'
            'esac\n'
            f'exec "{shutil.which("git")}" "$@"\n')
        wrapper.chmod(0o755)
        env = dict(os.environ, LEDGER_SESSION='A', PATH=f'{bin_dir}:{os.environ["PATH"]}')
        run = subprocess.run([str(ROOT / 'skills' / 'gene' / 'scripts' / 'commit-mine'), '-m', 'A'],
                             cwd=self.root, env=env, text=True, capture_output=True)
        self.assertTrue(killed.exists())
        self.assertNotEqual(0, run.returncode)
        self.assertIn('line10 A', self.git('show', 'HEAD:shared'))
        self.assertEqual(tracked, self.git('ls-files'))   # old index, nothing dropped
        self.assertFalse((self.root / '.git' / 'index.lock').exists())
        self.assertEqual([], list((self.root / '.git').glob('commit-mine-sync.*')))

    def test_same_target_concurrent_write_is_contested(self):
        payload = self.payload(file_path='shared', old_string='line10\n', new_string='line10 A\n')
        self.hook('pre', payload)
        self.file.write_text(self.file.read_text().replace('line10\n', 'line10 A\n').replace('line13\n', 'line13 OTHER\n'))
        self.hook('post', payload)
        before = self.git('rev-parse', 'HEAD')
        self.assertIn('tangled', self.command('commit-mine', 'A', '-m', 'A'))
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_unrelated_concurrent_write_is_not_recorded(self):
        payload = self.payload(file_path='shared', old_string='line10\n', new_string='line10 A\n')
        self.hook('pre', payload)
        (self.root / 'other').write_text('OTHER')
        self.file.write_text(self.file.read_text().replace('line10\n', 'line10 A\n'))
        self.hook('post', payload)
        self.assertEqual('shared\n', self.command('mine', 'A', '--files'))

    def test_write_creation_and_staged_index_preserved(self):
        payload = self.payload(tool='Write', file_path='with space', content='new\n')
        self.hook('pre', payload)
        (self.root / 'with space').write_text('new\n')
        self.hook('post', payload)
        self.file.write_text('OTHER STAGED\n')
        self.git('add', 'shared')
        index = self.git('show', ':shared')
        self.command('commit-mine', 'A', '-m', 'A')
        self.assertEqual('new\n', self.git('show', 'HEAD:with space'))
        self.assertEqual(index, self.git('show', ':shared'))
        self.assertNotIn('OTHER STAGED', self.git('show', 'HEAD:shared'))

    def test_overlap_with_uncommitted_change_cannot_land(self):
        self.edit('line5\n', 'line5 FIRST\n')
        self.edit('line5 FIRST\n', 'line5 SECOND\n', 'B')
        before = self.git('rev-parse', 'HEAD')
        self.assertIn('conflicts', self.command('commit-mine', 'B', '-m', 'B'))
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_undo_own_edit_no_commit(self):
        before = self.git('rev-parse', 'HEAD')
        self.edit('line5\n', 'line5 A\n')
        self.edit('line5 A\n', 'line5\n')
        self.assertIn('nothing to commit', self.command('commit-mine', 'A', '-m', 'A'))
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_codex_patch_update_add_delete(self):
        patch = '*** Begin Patch\n*** Update File: shared\n@@\n line9\n-line10\n+line10 CODEX\n line11\n*** Add File: new\n+new content\n*** End Patch\n'
        payload = self.payload(tool='apply_patch', input=patch)
        self.hook('codex-pre', payload)
        self.file.write_text(self.file.read_text().replace('line10\n', 'line10 CODEX\n'))
        (self.root / 'new').write_text('new content\n')
        self.hook('post', payload)
        self.command('commit-mine', 'A', '-m', 'patch')
        self.assertEqual('new content\n', self.git('show', 'HEAD:new'))
        self.assertEqual('', self.git('status', '--porcelain'))
        payload = self.payload(tool='apply_patch', patch='*** Begin Patch\n*** Delete File: new\n*** End Patch\n')
        self.hook('pre', payload)
        (self.root / 'new').unlink()
        self.hook('post', payload)
        self.command('commit-mine', 'A', '-m', 'delete')
        self.assertNotIn('new', self.git('ls-tree', '--name-only', 'HEAD'))
        self.assertEqual('', self.git('ls-files', 'new'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_codex_absolute_patch_through_symlinked_workspace(self):
        alias = Path(self.temp.name) / 'workspace'
        alias.symlink_to(self.root, target_is_directory=True)
        patch = f'*** Begin Patch\n*** Add File: {alias}/new\n+new\n*** End Patch'
        payload = self.payload(tool='apply_patch', command=patch)
        payload['cwd'] = str(alias)
        self.hook('codex-pre', payload)
        (self.root / 'new').write_text('new\n')
        self.hook('post', payload)
        self.assertEqual('new\n', self.command('mine', 'A', '--files'))

    def test_codex_patch_cannot_escape_repository(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + '-outside')
        outside.write_text('original\n')
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        patch = f'*** Begin Patch\n*** Update File: ../{outside.name}\n@@\n-original\n+changed\n*** End Patch'
        payload = self.payload(tool='apply_patch', command=patch)
        self.hook('codex-pre', payload)
        self.hook('post', payload)
        self.assertEqual('original\n', outside.read_text())
        self.assertIn('no verified explicit edits recorded', self.command('mine', 'A', '--files'))

    def test_recreated_file_not_deleted(self):
        payload = self.payload(tool='apply_patch', patch='*** Begin Patch\n*** Delete File: shared\n*** End Patch\n')
        self.hook('pre', payload)
        self.file.unlink()
        self.hook('post', payload)
        self.file.write_text('recreated')
        self.assertIn('present in the tree', self.command('commit-mine', 'A', '-m', 'delete'))

    def test_deletion_does_not_erase_concurrent_mode_change(self):
        payload = self.payload(tool='apply_patch', patch='*** Begin Patch\n*** Delete File: shared\n*** End Patch\n')
        self.hook('pre', payload)
        blob = self.git('rev-parse', 'HEAD:shared').strip()
        self.git('update-index', '--cacheinfo', f'100755,{blob},shared')
        self.git('commit', '-m', 'make executable')
        self.file.unlink()
        self.hook('post', payload)
        before = self.git('rev-parse', 'HEAD')
        output = self.command('commit-mine', 'A', '-m', 'delete')
        self.assertIn('changed in HEAD since deletion was recorded', output)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.assertTrue(self.git('ls-tree', 'HEAD', 'shared').startswith('100755'))

    def test_deletion_is_unverified_when_worktree_mode_differs_from_head(self):
        self.file.chmod(0o755)
        payload = self.payload(tool='apply_patch', patch='*** Begin Patch\n*** Delete File: shared\n*** End Patch\n')
        self.hook('pre', payload)
        self.file.unlink()
        self.hook('post', payload)
        before = self.git('rev-parse', 'HEAD')
        output = self.command('commit-mine', 'A', '-m', 'delete')
        self.assertIn('unverified or tangled', output)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_codex_prompt_context_is_once_without_permission_override(self):
        payload = dict(session_id='Codex', cwd=str(self.root))
        output = json.loads(subprocess.check_output([str(HOOK), 'codex-context'], input=json.dumps(payload), cwd=self.root, text=True))
        context = output['hookSpecificOutput']['additionalContext']
        self.assertIn("LEDGER_SESSION=Codex", context)
        self.assertEqual('UserPromptSubmit', output['hookSpecificOutput']['hookEventName'])
        self.assertNotIn('permissionDecision', output['hookSpecificOutput'])
        self.assertNotIn('updatedInput', output['hookSpecificOutput'])
        self.assertEqual('', subprocess.check_output([str(HOOK), 'codex-context'], input=json.dumps(payload), cwd=self.root, text=True))

    def test_codex_prompt_context_is_silent_outside_git(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        payload = dict(session_id='Codex', cwd=directory.name)
        self.assertEqual('', subprocess.check_output([str(HOOK), 'codex-context'], input=json.dumps(payload), cwd=directory.name, text=True))

    def test_codex_patch_capture_does_not_emit_context_or_change_permissions(self):
        payload = self.payload('Codex', 'apply_patch', command='*** Begin Patch\n*** Add File: new\n+new\n*** End Patch\n')
        self.assertEqual('', self.hook('codex-pre', payload))
        (self.root / 'new').write_text('new\n')
        self.hook('post', payload)
        self.assertEqual('new\n', self.command('mine', 'Codex', '--files'))
        second = self.payload('Codex', 'apply_patch', command='*** Begin Patch\n*** Add File: next\n+next\n*** End Patch\n')
        self.assertEqual('', self.hook('codex-pre', second))

    def test_codex_bash_is_not_rewritten(self):
        payload = self.payload('Codex', 'Bash', command='cat shared')
        self.assertEqual('', self.hook('codex-pre', payload))

    def test_nested_cwd(self):
        nested = self.root / 'nested'
        nested.mkdir()
        payload = self.payload(tool='Write', file_path='new', content='nested\n')
        payload['cwd'] = str(nested)
        self.hook('pre', payload)
        (nested / 'new').write_text('nested\n')
        self.hook('post', payload)
        self.assertEqual('nested/new\n', self.command('mine', 'A', '--files'))

    def test_symlink_commit_mode_preserved(self):
        (self.root / 'new-link').symlink_to('missing-target')
        sha = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], input=b'missing-target', cwd=self.root).decode().strip()
        directory = self.root / '.git/agent-ledger/A'
        directory.mkdir(parents=True)
        (directory / 'state').write_text(f'new-link\t-\t{sha}\tverified\t120000\n')
        self.command('commit-mine', 'A', '-m', 'link')
        self.assertTrue(self.git('ls-tree', 'HEAD', 'new-link').startswith('120000'))
        self.assertEqual('missing-target', self.git('show', 'HEAD:new-link'))

    def test_new_file_mode_does_not_follow_later_symlink(self):
        payload = self.payload(tool='Write', file_path='new', content='regular contents')
        self.hook('pre', payload)
        path = self.root / 'new'
        path.write_text('regular contents')
        self.hook('post', payload)
        path.unlink()
        path.symlink_to('another-target')
        self.command('commit-mine', 'A', '-m', 'regular')
        self.assertTrue(self.git('ls-tree', 'HEAD', 'new').startswith('100644'))
        self.assertEqual('regular contents', self.git('show', 'HEAD:new'))

    def test_legacy_snapshot_record_is_not_committed(self):
        sha = self.git('rev-parse', 'HEAD:shared').strip()
        directory = self.root / '.git/agent-ledger/A'
        directory.mkdir(parents=True)
        (directory / 'state').write_text(f'shared\t-\t{sha}\tok\n')
        before = self.git('rev-parse', 'HEAD')
        self.assertIn('unverified', self.command('commit-mine', 'A', '-m', 'legacy'))
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_ambiguous_patch_is_not_attributed(self):
        self.file.write_text('same\nsame\n')
        payload = self.payload(tool='apply_patch', command='*** Begin Patch\n*** Update File: shared\n@@\n-same\n+changed\n*** End Patch\n')
        self.hook('pre', payload)
        self.file.write_text('changed\nsame\n')
        self.hook('post', payload)
        self.assertIn('no verified explicit edits', self.command('mine', 'A', '--files'))

    def test_codex_command_payload_and_failed_post(self):
        payload = self.payload(tool='apply_patch', command='*** Begin Patch\n*** Update File: shared\n@@\n-line10\n+line10 A\n*** End Patch\n')
        self.hook('codex-pre', payload)
        self.hook('post', payload)
        self.assertIn('tangled', self.command('commit-mine', 'A', '-m', 'failed'))

    def test_parallel_post_updates_preserve_both_entries(self):
        payloads = []
        for name in ('one', 'two'):
            payload = self.payload(tool='Write', file_path=name, content=name)
            payload['tool_use_id'] = name
            self.hook('pre', payload)
            (self.root / name).write_text(name)
            payloads.append(payload)
        processes = [subprocess.Popen([str(HOOK), 'post'], stdin=subprocess.PIPE, cwd=self.root, text=True) for _ in payloads]
        for process, payload in zip(processes, payloads):
            process.stdin.write(json.dumps(payload))
            process.stdin.close()
        for process in processes:
            self.assertEqual(0, process.wait())
        self.assertEqual({'one', 'two'}, set(self.command('mine', 'A', '--files').splitlines()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
