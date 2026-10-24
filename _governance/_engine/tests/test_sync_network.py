"""Real Git + second-process writer checks for the sync lock boundary."""
import json
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
        legacy = '00_Scope/W1/_raw/old.md'
        hidden = '00_Scope/W1/_raw/.records/old.txt'
        bad = peer / legacy
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('## 1\n\n### user\n\nquestion\n\n### agent\n\nvisible answer\n')
        git('add', '-A', at=peer)
        git('commit', '-qm', 'old writer', at=peer)
        git('push', '-q', '--force', at=peer)
        before = git('rev-parse', 'HEAD')
        assert 'raw-storage (remote)' in sync.once(root)
        assert git('rev-parse', 'HEAD') == before and not (root / legacy).exists()

        # A local migration of already shared raw must reach the remote, both
        # after an explicit commit and after the offline checkpoint path.
        shared = git('rev-parse', 'HEAD', at=peer)
        for offline_first in (False, True):
            git('reset', '--hard', shared, at=peer)
            git('push', '-q', '--force', at=peer)
            git('fetch', '-q', 'origin')
            git('reset', '--hard', shared)
            destination = root / hidden
            destination.parent.mkdir(parents=True, exist_ok=True)
            (root / legacy).rename(destination)
            expected = destination.read_bytes()
            if offline_first:
                with patch.object(vs, '_git', offline):
                    assert 'pull 실패' in sync.once(root)
            else:
                git('add', '-A')
                git('commit', '-qm', 'migrate local raw')
            assert sync.once(root) == 'ok', 'a local repair was mistaken for incoming legacy raw'
            remote_bytes = original(bare, ['show', 'main:' + hidden], 30, text=False)
            assert remote_bytes.returncode == 0 and remote_bytes.stdout == expected
            assert legacy not in git('ls-tree', '-r', '--name-only', 'main', at=bare)
        git('reset', '--hard', before)
        git('reset', '--hard', shared, at=peer)
        git('push', '-q', '--force', at=peer)
        bad.unlink()
        good = peer / hidden
        good.parent.mkdir(parents=True)
        good.write_text('## 1\n\n<!-- osk-capture: dialogue-v1 "turn-1" -->\n\nvisible dialogue\n')
        attachment = peer / '00_Scope/W1/_raw/.records/attachment.bin'
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


def keep_local():
    """루트 무시 규칙에 `.osk/`가 없는 옛 vault — 이미 실린 로컬 상태는 추적만 풀고, 뒤의 것은 싣지 않는다."""
    with tempfile.TemporaryDirectory(prefix='osk-local-') as td:
        root, bare = Path(td) / 'vault', Path(td) / 'remote.git'
        root.mkdir()
        import sync_daemon as sync
        import vault_sync as vs

        def git(*args, at=root):
            r = vs._git(at, list(args), 30)
            assert r.returncode == 0, (args, r.stdout, r.stderr)
            return r.stdout.strip()

        def local_paths(tree):
            return [p for p in tree.split('\n') if p.startswith('.osk/')]

        git('init', '-q', '-b', 'main')
        for key, value in [('user.name', 'fixture'), ('user.email', 'fixture@example.invalid'),
                           ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')]:
            git('config', key, value)
        run_log = root / '.osk/growth/runs/a/stdout.txt'
        run_log.parent.mkdir(parents=True)
        run_log.write_text('worker output')
        (root / '.osk/response-growth.json').write_text('{"claude": "C:/this-device/claude.exe"}')
        (root / 'note.txt').write_text('base')
        git('add', '-A')
        git('commit', '-qm', 'legacy vault tracked its local state')
        git('init', '-q', '--bare', '-b', 'main', str(bare))
        git('remote', 'add', 'origin', str(bare))
        git('push', '-qu', 'origin', 'main')

        assert sync.once(root) == 'ok'
        assert local_paths(git('ls-tree', '-r', '--name-only', 'HEAD')) == ['.osk/.gitignore']
        assert local_paths(git('ls-tree', '-r', '--name-only', 'main', at=bare)) == ['.osk/.gitignore']
        assert run_log.read_text() == 'worker output', 'untracking must keep this device file'
        later = root / '.osk/growth/runs/b/stdout.txt'
        later.parent.mkdir(parents=True)
        later.write_text('later output')
        assert sync.once(root) == 'ok'
        assert local_paths(git('ls-tree', '-r', '--name-only', 'main', at=bare)) == ['.osk/.gitignore']
        print('local state stays local: PASS (legacy vault untracked, later runs ignored)')


def receiving_device():
    """다른 기기(B)가 A의 추적 해제를 받는다 — B도 이 엔진이면 자기 `.osk/`를 지킨다.
    B에 아직 올리지 못한 `.osk/` 수정 커밋이 있어도 modify/delete 충돌로 멈추지 않는다."""
    import sync_daemon as sync
    import vault_sync as vs
    for unpushed in (False, True):
        with tempfile.TemporaryDirectory(prefix='osk-receive-') as td:
            bare, a, b = Path(td) / 'remote.git', Path(td) / 'a', Path(td) / 'b'

            def git(*args, at):
                r = vs._git(at, list(args), 30)
                assert r.returncode == 0, (args, r.stdout, r.stderr)
                return r.stdout.strip()

            def configure(at):
                for key, value in [('user.name', 'fixture'), ('user.email', 'fixture@example.invalid'),
                                   ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')]:
                    git('config', key, value, at=at)

            a.mkdir()
            git('init', '-q', '-b', 'main', at=a)
            configure(a)
            (a / '.osk').mkdir()
            (a / '.osk/response-growth.json').write_text('A paths')
            (a / 'note.txt').write_text('base')
            git('add', '-A', at=a)
            git('commit', '-qm', 'legacy vault', at=a)
            git('init', '-q', '--bare', '-b', 'main', str(bare), at=a)
            git('remote', 'add', 'origin', str(bare), at=a)
            git('push', '-qu', 'origin', 'main', at=a)
            git('clone', '-q', str(bare), str(b), at=a)
            configure(b)
            (b / '.osk/response-growth.json').write_text('B paths')        # B의 옛 엔진이 실은 B의 설정
            (b / '.osk/growth/runs/rb').mkdir(parents=True)
            (b / '.osk/growth/runs/rb/out.txt').write_text('B run')
            git('add', '-A', at=b)
            git('commit', '-qm', 'B config', at=b)
            git('push', '-q', at=b)
            git('pull', '-q', '--rebase', at=a)
            if unpushed:                                                    # B가 끊긴 사이 다시 고친 설정
                (b / '.osk/response-growth.json').write_text('B paths v2')
                git('add', '-A', at=b)
                git('commit', '-qm', 'B config v2', at=b)

            assert sync.once(a) == 'ok'                                     # A가 추적을 풀어 올린다
            assert sync.once(b) == 'ok', 'B stopped on its own local state'
            expected = 'B paths v2' if unpushed else 'B paths'
            assert (b / '.osk/response-growth.json').read_text() == expected
            assert (b / '.osk/growth/runs/rb/out.txt').read_text() == 'B run'
            tree = git('ls-tree', '-r', '--name-only', 'main', at=bare)
            assert [p for p in tree.split('\n') if p.startswith('.osk/')] == ['.osk/.gitignore'], tree
    print('receiving device keeps its local state: PASS (pushed and unpushed local commits)')


def local_only():
    """No origin, or an origin that is the public canonical repository: the daemon commits and
    never fetches or pushes. A remote under another name is no remote to it (#254 review)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / 'solo'
        root.mkdir()
        os.environ.setdefault('OSK_VAULT_ROOT', str(root))
        import sync_daemon as sync
        import vault_sync as vs
        original = vs._git

        def git(*args):
            r = original(root, list(args), 30)
            assert r.returncode == 0, (args, r.stdout, r.stderr)
            return r.stdout.strip()

        git('init', '-q', '-b', 'main')
        for key, value in [('user.name', 'fixture'), ('user.email', 'fixture@example.invalid'),
                           ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')]:
            git('config', key, value)
        (root / 'note.txt').write_text('base')
        (root / '.gitignore').write_text('.osk/\n')
        git('add', '-A')
        git('commit', '-qm', 'base')
        (root / 'note.txt').write_text('edited')
        assert sync.once(root) == 'ok (로컬 커밋만 — 원격 없음)'
        assert git('status', '--porcelain') == '' and git('rev-list', '--count', 'HEAD') == '2'
        git('remote', 'add', 'upstream', 'https://example.invalid/not-origin.git')
        (root / 'note.txt').write_text('again')
        assert sync.once(root) == 'ok (로컬 커밋만 — 원격 없음)', 'a remote that is not origin counted as one'
        touched = []

        def instrument(at, args, timeout, **kwargs):
            if args[0] in ('fetch', 'push', 'pull', 'ls-remote'):
                touched.append(args[0])
            return original(at, args, timeout, **kwargs)

        def tick(expect_count):
            touched.clear()
            with patch.object(vs, '_git', instrument):
                status = sync.once(root)
            assert git('rev-list', '--count', 'HEAD') == str(expect_count), status
            return status

        git('remote', 'add', 'origin', 'git@github.com:LPAIU-CS/osk-system.git')   # the public canonical
        (root / 'note.txt').write_text('never sent')
        status = tick(4)
        assert '공개 정본' in status and touched == [], (status, touched)
        # setup recorded "no transfer" on this device: a remote added afterwards is not used until
        # setup has checked it, however private it looks.
        bare = Path(td) / 'private.git'
        assert original(Path(td), ['init', '-q', '--bare', '-b', 'main', str(bare)], 30).returncode == 0
        git('remote', 'set-url', 'origin', str(bare))
        targets = root / '.osk' / 'sync-targets.json'
        targets.parent.mkdir(exist_ok=True)
        targets.write_text(json.dumps({'origin': None, 'push': []}), encoding='utf-8')
        (root / 'note.txt').write_text('still local')
        status = tick(5)
        assert '전송 보류' in status and 'setup' in status and touched == [], (status, touched)
        # Once setup has recorded these targets, the daemon transfers.
        targets.write_text(json.dumps({'origin': str(bare), 'push': [str(bare)]}), encoding='utf-8')
        git('push', '-q', 'origin', 'main')
        (root / 'note.txt').write_text('sent')
        assert tick(6) == 'ok'
        assert original(bare, ['rev-parse', 'main'], 30).stdout.strip() == git('rev-parse', 'HEAD')
        # A push URL that differs from the recorded one is not used either, and the public canonical never.
        git('config', 'remote.origin.pushurl', 'https://github.com/lpaiu-cs/osk-system.git')
        (root / 'note.txt').write_text('never')
        status = tick(7)
        assert '공개 정본' in status and touched == [], (status, touched)
        git('config', 'remote.origin.pushurl', str(Path(td) / 'elsewhere.git'))
        (root / 'note.txt').write_text('nor this')
        status = tick(8)
        assert '전송 보류' in status and touched == [], (status, touched)
    print('local-only vault: PASS (no origin, non-origin remote, public canonical, unconfirmed and changed targets)')


if __name__ == '__main__':
    run()
    keep_local()
    receiving_device()
    local_only()
