"""Three physical layouts preserve graph, raw coordinates and approval trees."""
import itertools
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parents[1]


class SpaceLayoutTests(unittest.TestCase):
    def test_distribution_roots(self):
        for name in ("Scope", "Domain", "Person"):
            self.assertTrue((REPO / ("00_" + name) / ".gitkeep").is_file())
            self.assertFalse((REPO / name).exists())
            self.assertFalse((REPO / ("= " + name)).exists())

    def test_existing_layout_preserves_graph_raw_and_authority(self):
        for prefix in ("00_", "= ", ""):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                for name in ("Scope", "Domain", "Person"):
                    (root / (prefix + name)).mkdir()
                script = r'''
from osk import core, graph, validate, approvals, raw, update
from osk.vault import Vault
VAULT = Vault.open()
from osk.layout import space_roots
validate.make_mini_vault(VAULT, VAULT.root)
assert VAULT.scope == PREFIX + 'Scope', VAULT.scope
assert VAULT.ledger == VAULT.root / (PREFIX + 'Scope/Workbench/_ledger')
region = PREFIX + 'Person/Delegation'
approvals.protect(VAULT, region)
before = approvals.ledger_path(VAULT).read_bytes()
snapshot = approvals.working_tree_hash(VAULT, region)
idx = graph.Index(VAULT)
assert idx.nodes and not idx.broken
assert idx.resolve('00_Scope/W1/W1')[0] == 'node'
assert graph.scope_of_space(VAULT, '00_Scope/W1') == 'W1'
assert graph.scope_of_space(VAULT, '= Scope/W1') == 'W1'
assert raw.record_path(VAULT, 'W1', 'fixture').is_relative_to(VAULT.root / '_cited')   # 루트 `_cited/` (헌법 4조 3항)
assert update._allowed_skel(VAULT, '00_Scope') == VAULT.root / VAULT.scope
assert approvals.state(VAULT, region) == 'clean'
assert approvals.working_tree_hash(VAULT, region) == snapshot
assert approvals.ledger_path(VAULT).read_bytes() == before
assert space_roots(VAULT.root)['Scope'] == VAULT.scope
# The validator's isolated fixture builds its mini-vault in this vault's layout.
import tempfile
with tempfile.TemporaryDirectory() as fx:
    problems = validate.fixture_approval_lifecycle(VAULT, fx)
assert problems == [], problems
'''
                result = subprocess.run([sys.executable, '-c', f'PREFIX={prefix!r}\n' + script],
                                        env=dict(os.environ, OSK_VAULT_ROOT=td, PYTHONPATH=str(ENGINE)),
                                        capture_output=True, text=True, encoding='utf-8', timeout=180)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_ambiguous_roots_refused_and_empty_skeleton_does_not_split_ledger(self):
        sys.path.insert(0, str(ENGINE))
        from osk.layout import space_roots
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / '= Scope').mkdir()
            (root / '= Scope/existing').write_text('data')
            (root / '00_Scope').mkdir()
            (root / '00_Scope/.gitkeep').touch()
            self.assertEqual(space_roots(root)['Scope'], '= Scope')
            (root / '00_Scope/new-ledger').write_text('conflicting data')
            with self.assertRaisesRegex(RuntimeError, 'Ambiguous Space roots'):
                space_roots(root)

    def test_os_metadata_does_not_populate_a_root(self):
        # Opening an empty root in Finder or Explorer must not stop the engine.
        sys.path.insert(0, str(ENGINE))
        from osk.layout import space_roots
        metadata = ('.gitkeep', '.DS_Store', 'Thumbs.db', 'desktop.ini', '._.DS_Store')
        for data, stray in itertools.permutations(('00_', '= ', ''), 2):
            for name in metadata:
                with self.subTest(data=data, stray=stray, name=name), \
                        tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    (root / (data + 'Scope/proj')).mkdir(parents=True)
                    (root / (data + 'Scope/proj/n.md')).write_text('data')
                    (root / (stray + 'Scope')).mkdir()
                    (root / (stray + 'Scope') / name).touch()
                    self.assertEqual(space_roots(root)['Scope'], data + 'Scope')
                    # Control: real data in both roots is still refused.
                    (root / (stray + 'Scope/other.md')).write_text('data')
                    with self.assertRaisesRegex(RuntimeError, 'Ambiguous Space roots'):
                        space_roots(root)

    def test_metadata_name_on_a_directory_is_still_data(self):
        sys.path.insert(0, str(ENGINE))
        from osk.layout import space_roots
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / '= Scope/proj').mkdir(parents=True)
            (root / '= Scope/proj/n.md').write_text('data')
            (root / '00_Scope/._proj').mkdir(parents=True)
            (root / '00_Scope/._proj/n.md').write_text('data')
            with self.assertRaisesRegex(RuntimeError, 'Ambiguous Space roots'):
                space_roots(root)

    def test_broken_root_link_is_not_treated_as_an_empty_vault(self):
        sys.path.insert(0, str(ENGINE))
        from osk.layout import space_roots
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            try:
                (root / '00_Scope').symlink_to(root / 'missing', target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))
            with self.assertRaisesRegex(RuntimeError, 'local directory'):
                space_roots(root)


    def test_every_entry_point_stops_on_ambiguous_roots(self):
        # The engine does not pick between two populated roots of one Space: every entry point stops
        # there. That held by itself while importing the engine opened the vault. Each entry point
        # opens it now, so each one is held to it here — a daemon that goes on syncing such a vault
        # spreads the state to the other devices.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('00_Scope/a', '= Scope/b'):
                (root / name).mkdir(parents=True)
                (root / name / 'n.md').write_text('data')
            env = {**os.environ, 'OSK_VAULT_ROOT': str(root), 'PYTHONPATH': str(ENGINE), 'PYTHONUTF8': '1',
                   'SYNC_ENABLED': '1', 'OSK_UPDATE_CHECK': '0', 'OSK_HOOK_WORKER': '0'}

            def run(argv, stdin=None):
                return subprocess.run([sys.executable, '-B', *argv], env=env, input=stdin, capture_output=True,
                                      text=True, encoding='utf-8', timeout=120,
                                      **({} if stdin is not None else {'stdin': subprocess.DEVNULL}))
            def left():
                return sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
            before = left()
            # The arguments keep a failure offline and local: `update --ack` records on this device
            # where plain `update` asks the upstream, and `OSK_UPDATE_CHECK=0` turns the check off.
            for argv in (['-m', 'osk.cli', 'status'], ['-m', 'osk.validate'],
                         [str(ENGINE / 'sync_daemon.py'), '--once'], [str(ENGINE / 'mcp_server.py')],
                         ['-m', 'osk.update', '--ack', 'v0.0.0'], ['-m', 'osk.setup'], ['-m', 'osk.publish'],
                         ['-m', 'osk.release', '--version', 'v0.0.0'], ['-m', 'osk.update_check']):
                with self.subTest(argv=argv[:2] if argv[0] == '-m' else argv[-2:]):
                    done = run(argv)
                    self.assertNotEqual(done.returncode, 0, done.stdout[-300:])
                    self.assertIn('Ambiguous Space roots', done.stderr)
            self.assertEqual(left(), before)            # nothing was written: no ledger row, no `.git`
            # A hook never fails the host's turn: it says what is wrong and exits 0. Input it cannot
            # read is still reported as that, as it was when the hook met the vault only later.
            event = '{"session_id": "s", "cwd": "%s", "hook_event_name": "SessionStart"}' % root.as_posix()
            for script, stdin, want in (('claude_session_start.py', event, 'Ambiguous Space roots'),
                                        ('claude_session_start.py', 'not json', 'JSONDecodeError'),
                                        ('claude_prompt_submit.py', event, 'Ambiguous Space roots'),
                                        ('claude_prompt_submit.py', 'not json', 'JSONDecodeError'),
                                        ('capture_stop.py', event, 'Ambiguous Space roots'),
                                        ('capture_stop.py', 'not json', 'JSONDecodeError')):
                with self.subTest(script=script, stdin=stdin[:8]):
                    done = run([str(ENGINE / 'scripts/hooks' / script)], stdin)
                    self.assertEqual(done.returncode, 0, done.stderr[-300:])
                    self.assertIn(want, done.stdout)
            self.assertEqual(left(), before)


if __name__ == '__main__':
    unittest.main()
