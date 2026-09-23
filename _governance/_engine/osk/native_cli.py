"""Resolve retired Codex Desktop binaries within the configured installation."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


def resolve(executable: str, *, version: str | None = None, env=None) -> str:
    path = Path(executable)
    # Only Desktop's versioned directory is relocatable. PATH entries and other
    # installations remain explicitly pinned; bin/codex.exe may be much older.
    managed = (path.is_absolute() and path.name.lower() == 'codex.exe'
               and re.fullmatch(r'[0-9a-f]{16}', path.parent.name)
               and [p.name.lower() for p in list(path.parents)[1:4]] == ['bin', 'codex', 'openai'])
    if not managed:
        return executable

    def installed(candidate):
        try:
            result = subprocess.run([str(candidate), '--version'], env=env, shell=False,
                                    capture_output=True, text=True, encoding='utf-8', timeout=5,
                                    creationflags=0x08000000 if os.name == 'nt' else 0)
            match = re.fullmatch(r'codex-cli (\d+\.\d+\.\d+(?:-[\w.]+)?)\s*', result.stdout)
            return match[1] if result.returncode == 0 and match else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    if version is not None and path.is_file() and installed(path) == version:
        return executable
    matches = []
    for directory in sorted(path.parent.parent.iterdir()):
        candidate = directory / path.name
        if (not re.fullmatch(r'[0-9a-f]{16}', directory.name) or not candidate.is_file()
                or candidate.resolve().parent.parent != path.parent.parent.resolve()):
            continue
        found = installed(candidate)
        if found and (version is None or found == version):
            # Prefer a release over its prereleases, then numeric alpha/beta order.
            base, _, suffix = found.partition('-')
            order = (*map(int, base.split('.')), not bool(suffix),
                     tuple((0, int(v)) if v.isdigit() else (1, v) for v in suffix.split('.')))
            matches.append((order, str(candidate)))
    if not matches:
        raise ValueError('matching Codex Desktop CLI is unavailable; review remains pending')
    return max(matches)[1]
