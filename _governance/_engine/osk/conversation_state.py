"""osk.conversation_state — 대화 하나의 커서 파일.

대화마다 기기 로컬에 상태 파일 하나를 둔다: 어디까지 포착했고 어디까지 검토했는가. 키와 자리,
잠금, 읽기와 쓰기, 그리고 라운드 id를 줄여 적는 법이 여기 있다. 무엇을 포착하고 검토하는지는
위의 모듈이 정한다."""
from __future__ import annotations
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from . import core, nodefile
from . import harness as adapters
from ._portalock import lock_exclusive, unlock
from .vault import Vault


MAX_REVIEW_ROUNDS = 15


def conversation_key(vault: Vault, harness: str, conversation_id: str) -> str:
    if harness not in adapters.NAMES or not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", conversation_id):
        raise ValueError(f"explicit {'/'.join(adapters.NAMES)} harness and actual conversation_id required")
    return hashlib.sha256(f"{vault.root.resolve()}\n{harness}\n{conversation_id}".encode()).hexdigest()


def state_path(vault: Vault, harness: str, conversation_id: str) -> Path:
    key = conversation_key(vault, harness, conversation_id)
    root_key = hashlib.sha256(str(vault.root.resolve()).encode()).hexdigest()[:16]
    return core.local_lock_path(f"osk-integration-{root_key}-{key}.json", vault.root).with_suffix(".json")


def state_lock(vault: Vault, harness: str, conversation_id: str):
    return locked_path(state_path(vault, harness, conversation_id))


@contextmanager
def locked_path(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.with_suffix(".lock").open("a+b") as f:
        lock_exclusive(f)
        try:
            yield p
        finally:
            unlock(f)


def load_state(vault: Vault, p: Path, harness: str, sid: str) -> dict:
    if not p.exists():
        # Raw identity follows the native conversation across vault copies;
        # operational ownership remains ROOT-specific in state_path.
        return {"version": 1, "root": str(vault.root.resolve()), "harness": harness,
                "conversation_id": sid, "session": None, "space": None, "transcript_path": None,
                "record": record_name(harness, sid),
                "rounds": [], "reviewed_count": 0, "prompt_count": 0,
                "reviewed_prompt_count": 0, "snapshots": {}, "reviews": [],
                "capture_pending": False, "capture_error": None,
                "native_fingerprint": None, "coverage": None}
    s = json.loads(p.read_text(encoding="utf-8"))
    return validate_state(vault, s, harness, sid)


def record_name(harness: str, sid: str) -> str:
    key = hashlib.sha256((harness + "\n" + sid).encode()).hexdigest()[:32]
    return f"{harness}-{key}"


def validate_state(vault: Vault, s: dict, harness: str, sid: str) -> dict:
    """Validate already read bytes; status reports must not reread or repair them."""
    if (s.get("version"), s.get("root"), s.get("harness"), s.get("conversation_id")) != (
            1, str(vault.root.resolve()), harness, sid):
        raise ValueError("integration state identity/version mismatch; not reset")
    if not (isinstance(s.get("rounds"), list) and isinstance(s.get("snapshots"), dict)
            and isinstance(s.get("reviewed_count"), int)
            and 0 <= s["reviewed_count"] <= len(s["rounds"])):
        raise ValueError("damaged integration cursor; not reset")
    return s


def save_state(p: Path, s: dict) -> None:
    nodefile.write_file(p, json.dumps(s, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def snapshot(s: dict) -> str | None:
    if not s["rounds"]:
        return None
    return "sha256:" + hashlib.sha256(json.dumps(
        [s["root"], s["harness"], s["conversation_id"], s["rounds"]],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def blocked(vault: Vault, limit: int = 10) -> dict:
    """Conversations whose capture is stuck on this device, newest first (§9-3 3항).
    `observed` is the cursor's last save; the hooks keep only a one-line notice."""
    from datetime import datetime
    probe = state_path(vault, "claude", "probe")
    rows = []
    for p in probe.parent.glob("-".join(probe.name.split("-")[:3]) + "-*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            if s.get("capture_error") and s.get("root") == str(vault.root.resolve()):
                rows.append({"harness": s["harness"], "conversation_id": s["conversation_id"],
                             "error": s["capture_error"], "observed": datetime.fromtimestamp(
                                 p.stat().st_mtime, core.KST).strftime(core.TS_FMT)})
        except (OSError, ValueError, KeyError):
            continue
    rows.sort(key=lambda r: r["observed"], reverse=True)
    return {"count": len(rows), "conversations": rows[:limit]}


def memory_seen(vault: Vault, harness: str, conversation_id: str, digest: str, scope: str | None = None) -> bool:
    """Was this conversation last given the shared memory at exactly `digest`? A hook then
    carries one line instead of the whole text; a changed hash carries the whole again.
    `scope` names a bound scope other than the primary — each keeps its own last digest."""
    with state_lock(vault, harness, conversation_id) as p:
        s = load_state(vault, p, harness, conversation_id)
        shown = (s.get("memory_shown_bound") or {}).get(scope) if scope else s.get("memory_shown")
        return shown == digest


def note_memory(vault: Vault, harness: str, conversation_id: str, digest: str, scope: str | None = None) -> None:
    """The hook's output really carried the whole shared memory at `digest`."""
    with state_lock(vault, harness, conversation_id) as p:
        s = load_state(vault, p, harness, conversation_id)
        if scope:
            shown = dict(s.get("memory_shown_bound") or {})
            if shown.get(scope) == digest:
                return
            s["memory_shown_bound"] = {**shown, scope: digest}
        elif s.get("memory_shown") == digest:
            return
        else:
            s["memory_shown"] = digest
        save_state(p, s)


def shown_before(vault: Vault, harness: str, conversation_id: str, slot: str, mark: str) -> bool:
    """Was this conversation last given the hook block `slot` at exactly `mark`? Then a hook
    carries one line instead of the same whole block again (#275)."""
    with state_lock(vault, harness, conversation_id) as p:
        return (load_state(vault, p, harness, conversation_id).get("shown") or {}).get(slot) == mark


def note_shown(vault: Vault, harness: str, conversation_id: str, slot: str, mark: str) -> None:
    """The hook's output really carried the whole block `slot` at `mark`."""
    with state_lock(vault, harness, conversation_id) as p:
        s = load_state(vault, p, harness, conversation_id)
        shown = dict(s.get("shown") or {})
        if shown.get(slot) != mark:
            s["shown"] = {**shown, slot: mark}
            save_state(p, s)


def turn_key(harness: str, round_id: str) -> str:
    """The original turn's identity for citations and review refs. A Claude round is
    `<first user row>:<final message>` and an Antigravity round `<user input step>:<last
    step>`; the first part already names the turn while it is still open, so a citation of
    the open turn and of the finished round agree."""
    return round_id.split(":", 1)[0] if harness in ("claude", "antigravity") else round_id


SHORT_ID = 8  # the least prefix a citation may name a turn by (#219)


def short_ids(s: dict):
    """A turn ref → the shortest prefix of its turn ID, at least SHORT_ID, that tells this
    conversation's tracked turns apart. Codex IDs share time-ordered prefixes, so the length
    grows until every tracked turn differs; an untracked ref keeps SHORT_ID."""
    keys = {r["ref"]: turn_key(s["harness"], r["id"]) for r in s["rounds"]}
    n = SHORT_ID
    while len({k[:n] for k in keys.values()}) < len(set(keys.values())):
        n += 1
    return lambda ref: keys[ref][:n] if ref in keys else ref.rsplit(":", 1)[-1][:n]


def range_text(s: dict, refs) -> str:
    """Where a review's turns sit in the conversation, by position and short ID."""
    at = {r["ref"]: i + 1 for i, r in enumerate(s["rounds"])}
    refs = sorted(refs, key=lambda ref: at.get(ref, 0))
    pos, short, total = [at.get(ref, 0) for ref in refs], short_ids(s), f"전체 {len(s['rounds'])}턴"
    if len(refs) == 1:
        return f"{pos[0]}번째 턴 1개(`{short(refs[0])}`, {total})"
    if pos == list(range(pos[0], pos[0] + len(pos))):
        return f"{pos[0]}–{pos[-1]}번째 턴 {len(refs)}개(`{short(refs[0])}` … `{short(refs[-1])}`, {total})"
    return f"{'·'.join(map(str, pos))}번째 턴 {len(refs)}개({total})"


def review_refs(s: dict, through: str) -> list:
    """The turns a review of `through` covers — what its ACK closes and its batch read shows."""
    snap = s["snapshots"].get(through)
    repair = s.get("repair_pending", {}).get(through)
    if not snap or snap["count"] <= s["reviewed_count"] and not repair:
        raise ValueError("through is not an unreviewed snapshot of this conversation")
    return list(repair["refs"]) if repair else [r["ref"] for r in s["rounds"][s["reviewed_count"]:snap["count"]]]


def notice_once(vault: Vault, harness: str, conversation_id: str, text: str, *, settled: bool = False) -> bool:
    """Should a hook carry this standing diagnostic? Once per text: the same wait read again
    every prompt only fills the context, and `integration status` keeps it (#192). `settled`
    (nothing stands now) forgets it, so a later recurrence is news again."""
    with state_lock(vault, harness, conversation_id) as p:
        s = load_state(vault, p, harness, conversation_id)
        if not text:
            if settled and s.pop("notice", None) is not None:
                save_state(p, s)
            return False
        if s.get("notice") == text:
            return False
        s["notice"] = text
        save_state(p, s)
        return True
