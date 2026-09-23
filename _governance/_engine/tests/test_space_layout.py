"""Three physical layouts preserve graph, raw coordinates and approval trees."""
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
from osk.layout import space_roots
validate.make_mini_vault(core.ROOT)
assert core.SCOPE == PREFIX + 'Scope', core.SCOPE
assert core.LEDGER == core.ROOT / (PREFIX + 'Scope/Workbench/_ledger')
region = PREFIX + 'Person/Delegation'
approvals.protect(region)
before = approvals.APPROVALS.read_bytes()
snapshot = approvals.working_tree_hash(region)
idx = graph.Index()
assert idx.nodes and not idx.broken
assert idx.resolve('00_Scope/W1/W1')[0] == 'node'
assert graph.scope_of_space('00_Scope/W1') == 'W1'
assert graph.scope_of_space('= Scope/W1') == 'W1'
assert raw.record_path('W1', 'fixture').is_relative_to(core.ROOT / core.SCOPE)
assert update._allowed_skel('00_Scope') == core.ROOT / core.SCOPE
assert approvals.state(region) == 'clean'
assert approvals.working_tree_hash(region) == snapshot
assert approvals.APPROVALS.read_bytes() == before
assert space_roots(core.ROOT)['Scope'] == core.SCOPE
'''
                result = subprocess.run([sys.executable, '-c', f'PREFIX={prefix!r}\n' + script],
                                        env=dict(os.environ, OSK_VAULT_ROOT=td, PYTHONPATH=str(ENGINE)),
                                        capture_output=True, text=True, encoding='utf-8', timeout=60)
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


if __name__ == '__main__':
    unittest.main()
