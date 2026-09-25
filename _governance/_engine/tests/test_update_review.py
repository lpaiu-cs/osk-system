"""Update consent is a one-use checkpoint bound to the actual changeset."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]


class UpdateReviewTests(unittest.TestCase):
    def test_release_entrypoint_accepts_noninteractive_apply(self):
        sys.path.insert(0, str(ENGINE))
        from osk import release
        from unittest import mock
        with mock.patch.object(release.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(release, 'run', return_value={'applied': True}) as run, \
                mock.patch('builtins.print'), \
                mock.patch('builtins.input', side_effect=AssertionError('interactive prompt')):
            release.main(['--version', 'v9.21.0', '--apply'])
        run.assert_called_once_with('v9.21.0', True)

    def test_checkpoint_and_governance_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            script = r'''
import json, time
from pathlib import Path
from unittest import mock
from osk import approvals, core, update
root = core.ROOT
(root / '_governance').mkdir(parents=True)
(root / '_governance/Policy.md').write_text('old')
approvals.protect('_governance')
bundle = root.parent / 'bundle'
(bundle / '_governance/_engine/scripts').mkdir(parents=True)
manifest = bundle / '_governance/_engine/scripts/publish-manifest.txt'
manifest.write_text('MAP _governance/ -> _governance/\nSKEL 00_Scope/\n')
(bundle / '_governance/Policy.md').write_text('new')
def attest(version):
    files = {p.relative_to(bundle).as_posix(): core.sha256_file(p)
             for p in bundle.rglob('*') if p.is_file() and p.name != 'release.json'}
    (bundle / 'release.json').write_text(json.dumps({'version': version, 'files': files}))
attest('v9.21.0')
def run(**kw):
    return update.run(source='bundle', bundle=str(bundle), **kw)
preview = run(adopt=True)
assert preview['applied'] is False and 'review_id' in preview
assert not core.local_lock_path('osk-update-confirmation.json').exists()
before = approvals.APPROVALS.read_bytes()
# First call does not acquire the daemon lock or write instance payloads.
with mock.patch('sync_daemon._lock_path', side_effect=AssertionError('daemon touched')):
    first = run(apply=True, adopt=True)
assert first['approval_required'] and not first['ok']
assert '명시적 재승인' in first['instruction'] and '재시작' in first['instruction']
assert (root / '_governance/Policy.md').read_text() == 'old'
assert approvals.APPROVALS.read_bytes() == before
assert not update.TXN_MANIFEST.exists()
# Local drift invalidates the checkpoint even for the same command.
(root / '_governance/Policy.md').write_text('changed while waiting')
second = run(apply=True, adopt=True)
assert second['approval_required'] and second['review_id'] != first['review_id']
# This retry represents the explicit user response to the new plan.
applied = run(apply=True, adopt=True)
assert applied['applied'] and applied['governance_accepted'], applied
assert approvals.state('_governance') == 'clean'
assert (root / '_governance/Policy.md').read_text() == 'new'
assert (root / '_governance/Policy.md.local-v9.21.0').read_text() == 'changed while waiting'
assert not core.local_lock_path('osk-update-confirmation.json').exists()
# Each update asks again; a release label alone does not identify bytes.
(bundle / '_governance/Policy.md').write_text('next')
attest('v9.21.1')
third = run(apply=True)
assert third['approval_required']
(bundle / '_governance/Policy.md').write_text('different same version')
attest('v9.21.1')
fourth = run(apply=True)
assert fourth['approval_required'] and third['review_id'] != fourth['review_id']
# Expired or malformed tickets never authorize a write.
ticket = core.local_lock_path('osk-update-confirmation.json')
ticket.write_text(json.dumps({'review_id': fourth['review_id'], 'at': time.time()-3601}))
assert run(apply=True)['approval_required']
ticket.write_text('[]')
assert run(apply=True)['approval_required']
# Consent is consumed even on an apply-lock failure.
with mock.patch('sync_daemon._lock_path', side_effect=update.UpdateError('daemon busy')):
    try: run(apply=True)
    except update.UpdateError: pass
    else: raise AssertionError('expected lock failure')
assert run(apply=True)['approval_required']
# A failure after acceptance restores both approval and framework bytes.
old_ledger = approvals.APPROVALS.read_bytes()
old_policy = (root / '_governance/Policy.md').read_bytes()
real_accept = approvals._approve_locked
def fail_after_accept(*args, **kw):
    real_accept(*args, **kw)
    raise OSError('injected failure after approval')
with mock.patch.object(approvals, '_approve_locked', side_effect=fail_after_accept):
    try: run(apply=True)
    except update.UpdateError as e: assert '원상복구' in str(e)
    else: raise AssertionError('expected rollback')
assert approvals.APPROVALS.read_bytes() == old_ledger
assert (root / '_governance/Policy.md').read_bytes() == old_policy
assert approvals.state('_governance') == 'clean'
'''
            result = subprocess.run([sys.executable, '-c', script],
                                    env=dict(os.environ, OSK_VAULT_ROOT=str(Path(td) / 'vault'),
                                             PYTHONPATH=str(ENGINE)),
                                    capture_output=True, text=True, encoding='utf-8', timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_apply_stops_and_restarts_the_vault_daemon(self):
        with tempfile.TemporaryDirectory() as td:
            script = r'''
import subprocess, sys
from unittest import mock
from osk import core, update
from osk._portalock import lock_exclusive, unlock
engine = core.ROOT / '_governance/_engine'
engine.mkdir(parents=True)
fake = engine / 'sync_daemon.py'
fake.write_text("import time\nfrom osk import core\nfrom osk._portalock import lock_exclusive\n"
                "f = open(core.local_lock_path('osk-sync.lock'), 'w')\n"
                "lock_exclusive(f, blocking=False)\nprint('ready', flush=True)\ntime.sleep(300)\n")
daemon = subprocess.Popen([sys.executable, str(fake)], stdout=subprocess.PIPE, text=True)
try:
    assert daemon.stdout.readline().strip() == 'ready'
    assert daemon.pid in update._daemon_pids()
    info = {}
    with mock.patch.object(update, 'DAEMON_RESTART_WAIT', 1):
        with update._daemon_stopped(info):
            assert daemon.wait(timeout=30) is not None, 'daemon still running during apply'
            rival = open(core.local_lock_path('osk-sync.lock'), 'w')
            try:
                lock_exclusive(rival, blocking=False)
            except OSError:
                pass            # held by the update, so a relaunched daemon cannot start
            else:
                raise AssertionError('singleton not held during apply')
            finally:
                rival.close()
    assert daemon.pid in info['stopped'], info
    # A temp vault has no scheduled task or service, so the restart is reported, not faked.
    assert info['restarted'] is False and info['note'], info
    probe = open(core.local_lock_path('osk-sync.lock'), 'w')
    lock_exclusive(probe, blocking=False)
    unlock(probe)
    probe.close()
    # A held singleton with no matching daemon process is refused, not guessed at.
    holder = open(core.local_lock_path('osk-sync.lock'), 'w')
    lock_exclusive(holder, blocking=False)
    try:
        with update._daemon_stopped({}):
            raise AssertionError('entered without the singleton')
    except update.UpdateError as e:
        assert '프로세스를 찾지 못했다' in str(e), e
    finally:
        unlock(holder)
        holder.close()
finally:
    if daemon.poll() is None:
        daemon.kill()
'''
            result = subprocess.run([sys.executable, '-c', script],
                                    env=dict(os.environ, OSK_VAULT_ROOT=str(Path(td) / 'vault'),
                                             PYTHONPATH=str(ENGINE)),
                                    capture_output=True, text=True, encoding='utf-8', timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


    def test_governance_protection_is_part_of_the_transaction(self):
        with tempfile.TemporaryDirectory() as td:
            script = r'''
import json
from unittest import mock
from osk import approvals, core, update
root = core.ROOT
bundle = root.parent / 'bundle'
(bundle / '_governance/_engine/scripts').mkdir(parents=True)
(bundle / '_governance/_engine/scripts/publish-manifest.txt').write_text(
    'MAP _governance/ -> _governance/\nSKEL 00_Scope/\n')
(bundle / '_governance/Policy.md').write_text('v1')
files = {p.relative_to(bundle).as_posix(): core.sha256_file(p)
         for p in bundle.rglob('*') if p.is_file()}
(bundle / 'release.json').write_text(json.dumps({'version': 'v9.41.0', 'files': files}))
for rel in files:                      # a fresh clone: same bytes, no baseline yet
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes((bundle / rel).read_bytes())
def run():
    rep = update.run(source='bundle', bundle=str(bundle), apply=True)
    return update.run(source='bundle', bundle=str(bundle), apply=True) \
        if rep.get('approval_required') else rep
# An install that an older engine already baselined (no protect step) gets
# protected by rerunning the same release: nothing to write but the protect row.
real_review = update._governance_review
def older_engine(*a):
    out = real_review(*a)
    out.pop('protect', None)
    return out
with mock.patch.object(update, '_governance_review', side_effect=older_engine):
    rep = run()
assert rep['applied'] and rep['governance_protected'] is None, rep
assert approvals.state('_governance') == 'unprotected'
real = approvals.protect
def fail_after_protect(*a, **kw):
    real(*a, **kw)
    raise OSError('injected failure after protect')
with mock.patch.object(approvals, 'protect', side_effect=fail_after_protect):
    try: run()
    except update.UpdateError as e: assert '원상복구' in str(e), e
    else: raise AssertionError('expected rollback')
assert not approvals.APPROVALS.exists() and approvals.state('_governance') == 'unprotected'
assert update.current_version() == 'v9.41.0'
rep = run()
assert rep['governance_protected'] == 'established' and rep['applied_files'] == 0, rep
assert approvals.state('_governance') == 'clean'
# A later update of a protected region is acceptance, not a second protect.
(bundle / '_governance/Policy.md').write_text('v2')
files['_governance/Policy.md'] = core.sha256_file(bundle / '_governance/Policy.md')
(bundle / 'release.json').write_text(json.dumps({'version': 'v9.41.1', 'files': files}))
rep = run()
assert rep['governance_accepted'] and rep['governance_protected'] is None, rep
assert [r['kind'] for r in approvals.records()] == ['protect', 'approve']
# A region the user released is not re-protected by an update.
approvals.unprotect('_governance')
(bundle / '_governance/Policy.md').write_text('v3')
files['_governance/Policy.md'] = core.sha256_file(bundle / '_governance/Policy.md')
(bundle / 'release.json').write_text(json.dumps({'version': 'v9.41.2', 'files': files}))
rep = run()
assert rep['governance_protected'] == 'released', rep
assert approvals.state('_governance') == 'unprotected'
'''
            result = subprocess.run([sys.executable, '-c', script],
                                    env=dict(os.environ, OSK_VAULT_ROOT=str(Path(td) / 'vault'),
                                             PYTHONPATH=str(ENGINE)),
                                    capture_output=True, text=True, encoding='utf-8', timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

if __name__ == '__main__':
    unittest.main()
