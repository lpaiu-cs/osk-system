"""Resolve retired Codex/Claude Desktop binaries within the configured installation."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


SEMVER = r'\d+\.\d+\.\d+(?:-[\w.]+)?'


def _managed_codex(path: Path) -> bool:
    return bool(path.is_absolute() and path.name.lower() == 'codex.exe'
                and re.fullmatch(r'[0-9a-f]{16}', path.parent.name)
                and [p.name.lower() for p in list(path.parents)[1:4]] == ['bin', 'codex', 'openai'])


def _order(version: str) -> tuple:
    """Prefer a release over its prereleases, then numeric alpha/beta order."""
    base, _, suffix = version.partition('-')
    return (*map(int, base.split('.')), not bool(suffix),
            tuple((0, int(v)) if v.isdigit() else (1, v) for v in suffix.split('.')))


def _codex_version(candidate, env=None) -> str | None:
    try:
        result = subprocess.run([str(candidate), '--version'], env=env, shell=False,
                                capture_output=True, text=True, encoding='utf-8', timeout=5,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
        match = re.fullmatch(r'codex-cli (' + SEMVER + r')\s*', result.stdout)
        return match[1] if result.returncode == 0 and match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ancestors():
    """(image name, full path or '') of this process's ancestors, nearest first."""
    if os.name != 'nt':
        return  # ponytail: managed Desktop binaries exist only in the Windows layout; add ps/proc with one.
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = k32.OpenProcess.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    snapshot = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return
    table, entry = {}, Entry(dwSize=ctypes.sizeof(Entry))
    try:
        ok = k32.Process32FirstW(snapshot, entry)
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snapshot, entry)
    finally:
        k32.CloseHandle(snapshot)
    pid, seen = os.getpid(), set()
    while pid in table and pid not in seen:
        seen.add(pid)
        pid = table[pid][0]
        if pid not in table or pid in seen:
            return
        path, handle = '', k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                if k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    path = buffer.value
            finally:
                k32.CloseHandle(handle)
        yield table[pid][1], path


def runtime_codex() -> str | None:
    """Codex hooks lack CODEX_VERSION. Only the nearest Codex ancestor emitted this hook;
    use it when it is Desktop-managed, otherwise stay with the transcript's version."""
    for name, path in _ancestors():
        if name.lower() == 'codex.exe':
            return _codex_version(path) if path and _managed_codex(Path(path)) else None
    return None


def resolve(executable: str, *, version: str | None = None, env=None) -> str:
    path = Path(executable)
    # Claude Desktop keeps each release in Claude/claude-code/<version>/. Follow only
    # the source's exact version; without one an existing configured file stays pinned.
    if (path.is_absolute() and path.name.lower() in ('claude', 'claude.exe')
            and re.fullmatch(SEMVER, path.parent.name)
            and [p.name.lower() for p in list(path.parents)[1:3]] == ['claude-code', 'claude']):
        if version is None:
            if path.is_file():
                return executable
            # Desktop prunes old releases. Only routing lacks a version (no inference);
            # command() always requires the transcript's exact version.
            installed = [(_order(c.parent.name), str(c)) for c in path.parent.parent.glob('*/' + path.name)
                         if re.fullmatch(SEMVER, c.parent.name) and c.is_file()
                         and c.resolve().parent.parent == path.parent.parent.resolve()]
            return max(installed)[1] if installed else executable
        candidate = path.parent.parent / version / path.name
        if (not re.fullmatch(SEMVER, version) or not candidate.is_file()
                or candidate.resolve().parent.parent != path.parent.parent.resolve()):
            raise ValueError('matching Claude Desktop CLI is unavailable; review remains pending')
        return str(candidate)
    # Only Desktop's versioned directory is relocatable. PATH entries and other
    # installations remain explicitly pinned; bin/codex.exe may be much older.
    if not _managed_codex(path):
        return executable

    if version is not None and path.is_file() and _codex_version(path, env) == version:
        return executable
    matches = []
    for directory in sorted(path.parent.parent.iterdir()):
        candidate = directory / path.name
        if (not re.fullmatch(r'[0-9a-f]{16}', directory.name) or not candidate.is_file()
                or candidate.resolve().parent.parent != path.parent.parent.resolve()):
            continue
        found = _codex_version(candidate, env)
        if found and (version is None or found == version):
            matches.append((_order(found), str(candidate)))
    if not matches:
        raise ValueError('matching Codex Desktop CLI is unavailable; review remains pending')
    return max(matches)[1]
