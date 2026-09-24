"""Physical Space roots. Existing vaults keep their recorded path identities.

Changing a directory name also changes approval-tree hashes and raw coordinates.
Do not migrate that evidence as a side effect of importing or updating the engine.
New vaults use the ``00_`` prefix: it sorts the roots first and needs no shell quoting.
"""
from pathlib import Path
import stat

KINDS = ("Scope", "Domain", "Person")
PREFIXES = ("00_", "= ", "")
# Not vault data: the skeleton marker and the metadata a file manager drops into
# any folder it shows (``._*`` is macOS AppleDouble on non-Apple volumes).
# A directory is data whatever its name.
_NOT_DATA = {".gitkeep", ".ds_store", "thumbs.db", "desktop.ini"}


def _is_data(entry: Path) -> bool:
    name = entry.name.lower()
    return not ((name in _NOT_DATA or name.startswith("._")) and entry.is_file())


def space_roots(root: Path) -> dict[str, str]:
    roots = {}
    for kind in KINDS:
        existing = []
        for prefix in PREFIXES:
            name = prefix + kind
            try:
                info = (root / name).lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise RuntimeError(f"Space root must be a local directory: {name}")
            existing.append(name)
        # An old updater may have installed empty skeletons alongside real data,
        # and a file manager may have left its metadata in them.
        populated = [name for name in existing
                     if any(map(_is_data, (root / name).iterdir()))]
        if len(populated) > 1:
            raise RuntimeError(f"Ambiguous Space roots for {kind}: {populated}; no data moved")
        roots[kind] = populated[0] if populated else (existing[0] if existing else "00_" + kind)
    return roots


def adapt_path(path: str, roots: dict[str, str]) -> str:
    """Resolve only an exact first component; never rewrite payloads or history."""
    head, sep, tail = path.replace("\\", "/").partition("/")
    for kind in KINDS:
        if head in (prefix + kind for prefix in PREFIXES):
            return roots[kind] + sep + tail
    return path
