"""Distribution layout and legacy-vault fail-closed regression checks."""
import os
from pathlib import Path
import subprocess
import sys

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parents[1]


def test_distribution_roots():
    for name in ("Scope", "Domain", "Person"):
        assert (REPO / name / ".gitkeep").is_file()
        assert not (REPO / ("= " + name)).exists()


def test_legacy_vault_refused_without_writes(tmp_path):
    (tmp_path / "= Scope").mkdir()
    env = dict(os.environ, OSK_VAULT_ROOT=str(tmp_path), PYTHONPATH=str(ENGINE))
    result = subprocess.run([sys.executable, "-c", "import osk.core"], env=env,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "Legacy Space layout detected" in result.stderr
    assert list(tmp_path.iterdir()) == [tmp_path / "= Scope"]
