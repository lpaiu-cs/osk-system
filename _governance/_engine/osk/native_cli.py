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


def codex_ancestor() -> str | None:
    """The nearest Codex ancestor — the one that emitted this hook — when Desktop manages it."""
    for name, path in _ancestors():
        if name.lower() == 'codex.exe':
            return path if path and _managed_codex(Path(path)) else None
    return None


# A hook's detached worker no longer descends from Codex: the hook names the binary here.
DESKTOP_ENV = 'OSK_CODEX_DESKTOP'


def runtime_codex() -> str | None:
    """Codex hooks lack CODEX_VERSION. Only the nearest Codex ancestor emitted this hook;
    use it when it is Desktop-managed, otherwise stay with the transcript's version."""
    named = os.environ.get(DESKTOP_ENV)
    path = codex_ancestor() if named is None else named if _managed_codex(Path(named)) else None
    return _codex_version(path) if path else None


def _claude_desktop(base: Path) -> bool:
    """Desktop's `claude-code` folder: under `Claude` (an installer's or the MSIX LocalCache's
    Roaming folder) or under `Claude-Data/claude_<package>` (app 3.16 and later)."""
    owner = base.parent.name.lower()
    return base.name.lower() == 'claude-code' and (
        owner == 'claude' or bool(re.fullmatch(r'claude_[a-z0-9]+', owner))
        and base.parent.parent.name.lower() == 'claude-data')


def _moved(base: Path) -> Path:
    """The same installation after Desktop moved it. App 3.16 moved a store install's data from
    `Packages/Claude_<package>/LocalCache/Roaming/Claude` to `%LOCALAPPDATA%/Claude-Data/claude_<package>`
    and left the old folder empty, so a pin into it follows that package's new folder (10-21)."""
    package = next((p.name for p in base.parents if p.parent.name.lower() == 'packages'
                    and p.name.lower().startswith('claude_')), None)
    local = os.environ.get('LOCALAPPDATA')
    if base.is_dir() or not package or not local:
        return base  # ponytail: only the store install's move is known; follow others once one is seen.
    moved = Path(local) / 'Claude-Data' / package.lower() / 'claude-code'
    return moved if moved.is_dir() else base


def resolve(executable: str, *, version: str | None = None, env=None) -> str:
    path = Path(executable)
    # Desktop uses <version>/claude.exe or <version>/<build hash>/claude.exe.
    # Follow only the source's version within the configured installation.
    release = path.parent
    if re.fullmatch(r'[0-9a-f]{12}', release.name):
        release = release.parent
    base = release.parent
    if (path.is_absolute() and path.name.lower() in ('claude', 'claude.exe')
            and re.fullmatch(SEMVER, release.name) and _claude_desktop(base)):
        if version is not None and not re.fullmatch(SEMVER, version):
            raise ValueError('matching Claude Desktop CLI is unavailable; review remains pending')
        if (version is None or version == release.name) and path.is_file() and (
                path.resolve() == base.resolve() / path.relative_to(base)):
            return executable
        base = _moved(base)
        if not base.is_dir():
            raise ValueError('matching Claude Desktop CLI is unavailable; review remains pending')
        installed = []
        for directory in ([base / version] if version else base.iterdir()):
            if not re.fullmatch(SEMVER, directory.name):
                continue
            for candidate in [directory / path.name, *directory.glob('*/' + path.name)]:
                if candidate.parent != directory and not re.fullmatch(r'[0-9a-f]{12}', candidate.parent.name):
                    continue
                if candidate.is_file() and candidate.resolve() == base.resolve() / candidate.relative_to(base):
                    installed.append((_order(directory.name), str(candidate)))
        if not installed:
            raise ValueError('matching Claude Desktop CLI is unavailable; review remains pending')
        newest = max(v for v, _ in installed)
        choices = [p for v, p in installed if v == newest]
        if len(choices) != 1:
            raise ValueError('matching Claude Desktop CLI is ambiguous; pin the source build explicitly')
        return choices[0]
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
