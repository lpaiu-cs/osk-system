"""osk.transcript_files — 하네스가 둔 전사 파일.

대화 하나의 전사가 어느 파일들인지(`native_files`), 지금 디스크에 어떤 꼴로 있는지(`on_disk` — Codex는 이레
묵은 rollout을 제자리에서 `.zst`로 줄인다, #203), 그 평문 바이트(`open_native`·`native_lines`)와 지문
(`native_fingerprint`). 바이트 경계·커서·지문은 디스크의 꼴과 무관하게 평문 바이트를 가리킨다."""
from __future__ import annotations
import io
import json
import os
import re
from pathlib import Path


class ZstdMissing(OSError):
    """This device's environment cannot decompress a Codex rollout that is there (#203)."""


def _zstd():
    try:
        import zstandard
    except ImportError as exc:
        raise ZstdMissing("Codex compressed rollout (.jsonl.zst) needs the zstandard package; "
                          "reinstall _governance/_engine/requirements.txt into this vault's .venv") from exc
    return zstandard


def on_disk(path) -> Path | None:
    """The rollout at `path` in the form on disk now, the plain file while both exist."""
    p = Path(path)
    plain = p.with_suffix("") if p.suffix == ".zst" else p
    return next((f for f in (plain, plain.with_name(plain.name + ".zst")) if f.exists()), None)


def rollouts(folder: Path, pattern: str) -> list[Path]:
    """Files matching the `.jsonl` glob `pattern`, plain or compressed, one per rollout."""
    return list(folder.glob(pattern)) + [
        p for p in folder.glob(pattern + ".zst") if not p.with_suffix("").exists()]


def open_native(path):
    """A native transcript's plain bytes as a binary stream."""
    p = on_disk(path) or Path(path)
    if p.suffix != ".zst":
        return p.open("rb")
    zstandard = _zstd()
    return io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(p.open("rb"), read_across_frames=True))


def _stat(p: Path) -> dict:
    """Size in plain bytes and mtime: a compressed rollout keeps both, so it is no new turn."""
    if p.suffix != ".zst":
        stat = p.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    zstandard = _zstd()
    with p.open("rb") as f:
        try:
            size = zstandard.frame_content_size(f.read(18))
        except zstandard.ZstdError as exc:
            raise ValueError(f"unreadable Codex compressed rollout: {exc}") from exc
    if size < 0:  # Codex always declares it; count the bytes of a file that does not.
        with open_native(p) as stream:
            size = sum(map(len, iter(lambda: stream.read(1 << 20), b"")))
    return {"size": size, "mtime_ns": p.stat().st_mtime_ns}


def native_files(path: str, harness: str, sid: str) -> list[tuple[Path, int | None]]:
    """Follow declared Codex pages, never neighbouring tasks or filename order."""
    current, limit, pages = (on_disk(path) or Path(path)).resolve(), None, []
    allowed = {sid}
    while True:
        if any(current.samefile(p) for p, _ in pages):
            raise ValueError("cyclic Codex history_base")
        pages.append((current, limit))
        if harness != "codex":
            return pages
        with open_native(current) as stream:
            first = next((line for line in stream if line.strip()), b"")
            row = json.loads(first)
            meta = row.get("payload") if isinstance(row, dict) else None
            if (not isinstance(meta, dict) or row.get("type") != "session_meta"
                    or not isinstance(meta.get("id"), str) or meta["id"] not in allowed):
                raise ValueError("Codex history identity mismatch: session_meta.id does not match this conversation")
            if limit is not None and limit < stream.tell():
                raise ValueError("Codex history byte boundary excludes its identity")
        base = meta.get("history_base")
        if base is None:
            return list(reversed(pages))
        # A page may continue its own conversation or the explicitly declared
        # fork parent. A history filename alone never authorizes a foreign ID.
        allowed = {meta["id"]}
        parent = meta.get("forked_from_id")
        if parent is not None:
            if not isinstance(parent, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", parent):
                raise ValueError("invalid Codex fork parent identity")
            allowed.add(parent)
        if (not isinstance(base, dict)
                or not isinstance(base.get("thread_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", base["thread_id"])
                or type(base.get("end_byte_offset")) is not int or base["end_byte_offset"] <= 0):
            raise ValueError("invalid Codex history_base identity/byte boundary")
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        pattern = f"*[-_]{base['thread_id']}.jsonl"
        matches = []
        for folder, glob in (
            (current.parent, pattern), (home / "sessions", "*/*/*/" + pattern),
            (home / "archived_sessions", pattern)):
            for p in rollouts(folder, glob):
                # Windows extended paths and ordinary paths can name one file.
                if p.is_file() and not any(p.samefile(other) for other in matches):
                    matches.append(p.resolve())
        if len(matches) != 1:
            raise ValueError("Codex history_base source missing or ambiguous")
        current, limit = matches.pop(), base["end_byte_offset"]


def workspace(path: str, harness: str) -> dict | None:
    """이 대화의 작업 폴더와 하네스 판본 — 일회성 작업 폴더의 판별에만 쓴다(Mechanism §6-2 6항).
    앞쪽 행만 읽는다. Claude는 행마다, Codex는 첫 `session_meta`가 담는다. 모르면 None이다."""
    try:
        with open_native(path) as f:
            for n, line in enumerate(f):
                if n >= 50:
                    break
                row = json.loads(line)
                if harness == "claude" and isinstance(row, dict) and isinstance(row.get("cwd"), str):
                    return {"cwd": row["cwd"], "version": row.get("version")}
                if harness == "codex" and isinstance(row, dict) and row.get("type") == "session_meta":
                    meta = row.get("payload") or {}
                    return {"cwd": meta.get("cwd"), "version": meta.get("cli_version")}
    except (OSError, ValueError):
        pass
    return None


def native_fingerprint(path: str, harness: str, sid: str) -> dict:
    pages = native_files(path, harness, sid)
    result = _stat(pages[-1][0])
    if len(pages) > 1:
        # A page is named in its plain form: compressing it is no change.
        result["history"] = [{"path": str(p.with_suffix("") if p.suffix == ".zst" else p),
                              "through": limit, **_stat(p)} for p, limit in pages[:-1]]
    return result


def page_lines(native: Path, remaining: int | None):
    with open_native(native) as stream:
        while remaining is None or remaining > 0:
            line = stream.readline(-1 if remaining is None else remaining)
            if not line:
                if remaining:
                    raise ValueError("Codex history byte boundary exceeds source")
                break
            if remaining is not None:
                if not line.endswith(b"\n"):
                    raise ValueError("Codex history byte boundary splits a record")
                remaining -= len(line)
            yield line


def reverted(path: str, harness: str, sid: str) -> list[str]:
    """Turns Codex `thread/revert` took out of this conversation (#208). The revert leaves
    the old page as it was and starts a new one that keeps it only up to a byte bound: a
    turn the conversation's own earlier page started past that bound is reverted. A fork
    parent's later turns are the parent's, not reverted."""
    turns = []
    for page, limit in native_files(path, harness, sid)[:-1]:
        offset, owner = 0, None
        for line in page_lines(page, None):
            start, offset = offset, offset + len(line)
            if owner is None and line.strip():
                owner = json.loads(line).get("payload", {}).get("id")
                if owner != sid:
                    break
            elif start >= limit:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # An unreadable tail row can only refuse more, never accept.
                p = row.get("payload") if isinstance(row, dict) else None
                if (row.get("type") == "event_msg" and isinstance(p, dict)
                        and p.get("type") == "task_started" and p.get("turn_id")):
                    turns.append(p["turn_id"])
    return turns


def native_lines(path: str, harness: str, sid: str):
    for native, remaining in native_files(path, harness, sid):
        yield from page_lines(native, remaining)
