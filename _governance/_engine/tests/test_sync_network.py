"""Real Git + second-process writer checks for the sync lock boundary."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

# A separate process is essential: a same-process probe cannot prove exclusion.
PROBE = r'''
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from osk._portalock import lock_exclusive, unlock
root, lock, action = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
with open(lock, 'a') as f:
    try:
        lock_exclusive(f, blocking=False)
    except OSError:
        print('locked')
        sys.exit(0)
    try:
        if action in ('fetch', 'push'):
            (root / (action + '.txt')).write_text(action)
        if action == 'push':
            for args in (['add', '-A'], ['commit', '-qm', 'later local writer']):
                subprocess.run(['git', '-C', str(root), *args], check=True,
                               capture_output=True, creationflags=0x08000000 if os.name == 'nt' else 0)
        print('free')
    finally:
        unlock(f)
'''


def run():
    with tempfile.TemporaryDirectory(prefix='osk-network-') as td:
        root = Path(td) / 'local'
        root.mkdir()
        os.environ['OSK_VAULT_ROOT'] = str(root)
        import sync_daemon as sync
        import vault_sync as vs
        bare, peer = Path(td) / 'remote.git', Path(td) / 'peer'
        original = vs._git

        def git(*args, at=root):
            r = original(at, list(args), 30)
            assert r.returncode == 0, (args, r.stdout, r.stderr)
            return r.stdout.strip()

        def configure(at):
            for key, value in [('user.name', 'fixture'), ('user.email', 'fixture@example.invalid'),
                               ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')]:
                git('config', key, value, at=at)

        git('init', '-q', '-b', 'main')
        configure(root)
        (root / 'note.txt').write_text('base')
        (root / '.gitignore').write_text('.osk/\n')
        git('add', '-A')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        git('init', '-q', '--bare', '-b', 'main', str(bare))
        git('remote', 'add', 'origin', str(bare))
        git('push', '-qu', 'origin', 'main')
        git('clone', '-q', str(bare), str(peer))
        configure(peer)
        (peer / 'remote.txt').write_text('remote')
        git('add', '-A', at=peer)
        git('commit', '-qm', 'remote', at=peer)
        git('push', '-q', at=peer)
        remote = git('rev-parse', 'HEAD', at=peer)

        def probe(action):
            r = subprocess.run([sys.executable, '-c', PROBE, str(ENGINE), str(root),
                                str(sync._lock_path(root, 'osk-mutation.lock')), action],
                               capture_output=True, text=True, timeout=20,
                               creationflags=0x08000000 if os.name == 'nt' else 0)
            assert r.returncode == 0, (r.stdout, r.stderr)
            return r.stdout.strip()

        seen = []

        def instrument(at, args, timeout, **kwargs):
            if args[0] == 'fetch':
                assert probe('fetch') == 'free', 'fetch excludes a writer'
                result = original(at, args, timeout, **kwargs)
                # Another actor's fetch must not replace the snapshot to apply.
                (root / '.git/FETCH_HEAD').write_text(base + '\n')
                git('update-ref', 'refs/remotes/origin/main', base)
                return result
            if args[0] == 'rebase':
                assert args == ['rebase', '--onto', remote, remote], args
                assert probe('apply') == 'locked', 'rebase is not exclusive'
            if args[0] == 'push':
                sha = args[-1].split(':')[0]
                assert sha == git('rev-parse', 'HEAD')
                assert probe('push') == 'free', 'push excludes a writer'
                assert sha != git('rev-parse', 'HEAD'), 'positive control did not advance HEAD'
                seen.append(sha)
            return original(at, args, timeout, **kwargs)

        with patch.object(vs, '_git', instrument):
            assert sync.once(root) == 'ok'
        assert seen == [git('rev-parse', 'main', at=bare)]
        assert git('show', 'main:fetch.txt', at=bare) == 'fetch'
        assert git('show', 'main:remote.txt', at=bare) == 'remote'
        assert 'push.txt' not in git('ls-tree', '-r', '--name-only', 'main', at=bare)
        assert sync.once(root) == 'ok'
        assert git('show', 'main:push.txt', at=bare) == 'push'
        assert not git('for-each-ref', 'refs/osk-sync/')

        # A transaction that starts during fetch must be rechecked after fetch.
        marker = root / '.osk/txn/manifest.json'
        before = git('rev-parse', 'HEAD'), git('ls-files', '--stage')

        def pending(at, args, timeout, **kwargs):
            result = original(at, args, timeout, **kwargs)
            if args[0] == 'fetch':
                marker.parent.mkdir(parents=True)
                marker.write_text('{}')
                (root / 'note.txt').write_text('half applied')
            return result

        with patch.object(vs, '_git', pending):
            assert sync.once(root) == 'pending-txn'
        assert before == (git('rev-parse', 'HEAD'), git('ls-files', '--stage'))
        assert git('show', 'main:note.txt', at=bare) == 'base'
        marker.unlink()
        git('checkout', '--', 'note.txt')

        def switch(at, args, timeout, **kwargs):
            result = original(at, args, timeout, **kwargs)
            if args[0] == 'fetch':
                git('checkout', '-qb', 'side')
                (root / 'note.txt').write_text('user work')
            return result

        with patch.object(vs, '_git', switch):
            assert '브랜치 고정 실패' in sync.once(root)
        assert git('branch', '--show-current') == 'side'
        assert (root / 'note.txt').read_text() == 'user work'
        git('checkout', '--', 'note.txt')
        git('checkout', '-q', 'main')

        # A real non-fast-forward rejection retries from a newly fetched commit.
        git('pull', '--rebase', '-q', at=peer)
        calls = []

        def reject(at, args, timeout, **kwargs):
            if args[0] == 'push':
                if not calls:
                    (peer / 'race.txt').write_text('racer')
                    git('add', '-A', at=peer)
                    git('commit', '-qm', 'racer', at=peer)
                    git('push', '-q', at=peer)
                result = original(at, args, timeout, **kwargs)
                calls.append(result.returncode)
                return result
            return original(at, args, timeout, **kwargs)

        with patch.object(vs, '_git', reject):
            assert sync.once(root) == 'ok'
        assert len(calls) == 2 and calls[0] != 0 and calls[1] == 0, calls
        assert (root / 'race.txt').read_text() == 'racer'
        assert git('rev-parse', 'HEAD') == git('rev-parse', 'main', at=bare)
        assert not git('for-each-ref', 'refs/osk-sync/')

        # Offline fetch still checkpoints local work without publishing it.
        remote_head = git('rev-parse', 'main', at=bare)
        (root / 'offline.txt').write_text('saved locally')

        def offline(at, args, timeout, **kwargs):
            if args[0] == 'fetch':
                return subprocess.CompletedProcess(args, 1, '', 'offline')
            assert args[0] != 'push', 'a failed fetch must not push'
            return original(at, args, timeout, **kwargs)

        with patch.object(vs, '_git', offline):
            assert 'pull 실패' in sync.once(root)
        assert git('show', 'HEAD:offline.txt') == 'saved locally'
        assert git('rev-parse', 'main', at=bare) == remote_head
        assert not git('for-each-ref', 'refs/osk-sync/')
        print('sync network boundary: PASS (real Git and independent writer)')

        # Each case uses only the disposable local/peer/bare fixture above.
        for case in ('rewrite', 'prefetched', 'rewind', 'expired-reflog', 'first-contact', 'local-moved'):
            git('reset', '--hard', base)
            git('reset', '--hard', base, at=peer)
            (peer / 'discarded.txt').write_text('old upstream B')
            git('add', '-A', at=peer)
            git('commit', '-qm', 'old upstream B', at=peer)
            old = git('rev-parse', 'HEAD', at=peer)
            git('push', '-q', '--force', at=peer)
            git('fetch', '-q', 'origin')
            git('reset', '--hard', old)
            (root / 'local.txt').write_text('local L')
            git('add', '-A')
            git('commit', '-qm', 'local L')
            local = git('rev-parse', 'HEAD')
            git('reset', '--hard', base, at=peer)
            if case != 'rewind':
                (peer / 'replacement.txt').write_text('new upstream C')
                git('add', '-A', at=peer)
                git('commit', '-qm', 'new upstream C', at=peer)
            git('push', '-q', '--force', at=peer)
            new = git('rev-parse', 'HEAD', at=peer)
            if case in ('prefetched', 'expired-reflog'):
                git('fetch', '-q', 'origin')
            if case == 'expired-reflog':
                git('reflog', 'expire', '--expire=now', 'refs/remotes/origin/main')
            if case == 'first-contact':
                git('update-ref', '-d', 'refs/remotes/origin/main')

            def move_local(at, args, timeout, **kwargs):
                result = original(at, args, timeout, **kwargs)
                if args[0] == 'fetch':
                    git('reset', '--hard', base)
                    (root / 'other.txt').write_text('independent local history')
                    git('add', '-A')
                    git('commit', '-qm', 'independent local history')
                return result

            if case == 'local-moved':
                with patch.object(vs, '_git', move_local):
                    assert 'pull 실패' in sync.once(root)
                assert git('rev-parse', 'main', at=bare) == new
                assert (root / 'other.txt').read_text() == 'independent local history'
            elif case in ('expired-reflog', 'first-contact'):
                for _ in range(2):
                    assert '분기점' in sync.once(root), case
                    assert git('rev-parse', 'HEAD') == local
                    assert git('rev-parse', 'main', at=bare) == new
            else:
                for _ in range(2):
                    assert sync.once(root) == 'ok', case
                    files = git('ls-tree', '-r', '--name-only', 'main', at=bare)
                    assert 'local.txt' in files and 'discarded.txt' not in files, (case, files)
                    assert git('show', 'main:local.txt', at=bare) == 'local L'
                    assert original(bare, ['merge-base', '--is-ancestor', old, 'main'], 30).returncode == 1
            assert not git('for-each-ref', 'refs/osk-sync/')
        print('sync replay boundary: PASS (rewrite, prefetched, rewind, expired reflog, first contact, local moved)')

        # An old peer can still write Markdown raw. Refuse it before rebase;
        # neither silently sanitize it nor publish another local legacy record.
        git('reset', '--hard', base)
        git('reset', '--hard', base, at=peer)
        legacy = '= Scope/W1/_raw/old.md'
        hidden = '= Scope/W1/_raw/.records/old.txt'
        bad = peer / legacy
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('## 1\n\n<!-- osk-capture: codex-user-items-v2 -->\n\nold capture\n')
        git('add', '-A', at=peer)
        git('commit', '-qm', 'old writer', at=peer)
        git('push', '-q', '--force', at=peer)
        before = git('rev-parse', 'HEAD')
        assert 'raw-storage (remote)' in sync.once(root)
        assert git('rev-parse', 'HEAD') == before and not (root / legacy).exists()
        bad.unlink()
        good = peer / hidden
        good.parent.mkdir(parents=True)
        good.write_text('## 1\n\n<!-- osk-capture: dialogue-v1 "turn-1" -->\n\nvisible dialogue\n')
        attachment = peer / '= Scope/W1/_raw/.records/attachment.bin'
        attachment.write_bytes(b'\x89\xff\x00')
        git('add', '-A', at=peer)
        git('commit', '-qm', 'repair storage', at=peer)
        git('push', '-q', at=peer)
        assert sync.once(root) == 'ok'
        assert (root / hidden).read_bytes() == good.read_bytes()
        assert (root / attachment.relative_to(peer)).read_bytes() == attachment.read_bytes()
        (root / legacy).write_text('new local legacy capture')
        before = git('rev-parse', 'HEAD'), git('rev-parse', 'main', at=bare)
        assert 'raw-storage (local)' in sync.once(root)
        assert before == (git('rev-parse', 'HEAD'), git('rev-parse', 'main', at=bare))
        assert (root / legacy).read_text() == 'new local legacy capture'
        (root / legacy).unlink()
        (root / hidden).write_text('## 1\n\n<!-- osk-capture: codex-user-items-v2 -->\n\nold capture\n')
        assert 'legacy capture codec' in sync.once(root)
        assert before == (git('rev-parse', 'HEAD'), git('rev-parse', 'main', at=bare))
        git('checkout', '--', hidden)
        (root / legacy).write_text('committed by an old local writer')
        git('add', '-A')
        git('commit', '-qm', 'old local commit')
        assert 'raw-storage (outgoing commits)' in sync.once(root)
        assert git('rev-parse', 'main', at=bare) == before[1]
        print('raw storage ingress/egress: PASS (reject legacy, accept dialogue, preserve rejected bytes)')


if __name__ == '__main__':
    run()
