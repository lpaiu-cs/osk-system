"""Physical Space roots. Existing vaults keep their recorded path identities.

Changing a directory name also changes approval-tree hashes and raw coordinates.
Do not migrate that evidence as a side effect of importing or updating the engine.
New vaults use the ``00_`` prefix: it sorts the roots first and needs no shell quoting.
"""
from pathlib import Path
import stat

KINDS = ("Scope", "Domain", "Person")
PREFIXES = ("00_", "= ", "")


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
        # An old updater may have installed empty skeletons alongside real data.
        populated = [name for name in existing
                     if any(p.name != ".gitkeep" for p in (root / name).iterdir())]
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
