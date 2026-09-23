"""Install a reversible knowledge-only Obsidian graph projection; retain all sources."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import os
import tempfile

QUERY = '(' + ' OR '.join(f'path:"{prefix}{kind}/"' for prefix in ("00_", "= ", "")
                         for kind in ("Scope", "Domain", "Person")) + ') [id] [summary] -path:"/_"'


def configure(root: Path, apply: bool = False) -> dict:
    root = root.resolve(strict=True)
    target = root / ".obsidian" / "graph.json"
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("Obsidian settings must be local to this vault")
    before = target.read_bytes() if target.exists() else None
    settings = json.loads(before.decode("utf-8-sig")) if before else {}
    if not isinstance(settings, dict):
        raise ValueError("graph.json must contain an object")
    updated = dict(settings, search=QUERY, hideUnresolved=True, showTags=False)
    after = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
    result = {"path": str(target), "before_search": settings.get("search"),
              "after_search": QUERY, "changed": updated != settings, "applied": False}
    if not apply or not result["changed"]:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if before is not None:
        digest = hashlib.sha256(before).hexdigest()
        backup = target.with_name("graph.before-osk-" + digest[:16] + ".json")
        if backup.exists() and backup.read_bytes() != before:
            raise ValueError("backup collision")
        if not backup.exists():
            with backup.open("xb") as f:
                f.write(before)
        result["backup"] = str(backup)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".graph-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(after)
        if (target.read_bytes() if target.exists() else None) != before:
            raise ValueError("graph settings changed during configuration; retry after inspection")
        os.replace(name, target)
        if target.read_bytes() != after:
            raise ValueError("graph settings read-back differs")
    finally:
        if Path(name).exists():
            Path(name).unlink()
    return dict(result, applied=True, sha256=hashlib.sha256(after).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(configure(args.vault_root, args.apply), ensure_ascii=False, indent=2))
