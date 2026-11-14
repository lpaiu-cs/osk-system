"""Run with python tests/test_growth.py; every check owns a subprocess mini-vault."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from growth_cases.support import BOOT, ENGINE
from growth_cases.rechecks import RecheckCases
from growth_cases.packets import PacketCases
from growth_cases.workers import WorkerCases
from growth_cases.queues import QueueCases


class GrowthTests(RecheckCases, PacketCases, WorkerCases, QueueCases, unittest.TestCase):

    def check_case(self, source):
        with tempfile.TemporaryDirectory(prefix="osk-growth-test-") as directory:
            env = dict(os.environ, OSK_VAULT_ROOT=directory, PYTHONPATH=str(ENGINE),
                       PYTHONDONTWRITEBYTECODE="1", TEMP=directory, TMP=directory, OSK_HOOK_WORKER="0")
            result = subprocess.run([sys.executable, "-B", "-c", BOOT + textwrap.dedent(source)],
                                    env=env, cwd=directory, capture_output=True,
                                    text=True, encoding="utf-8", timeout=90,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
