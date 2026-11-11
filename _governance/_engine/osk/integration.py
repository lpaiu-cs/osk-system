"""osk.integration — 대화들을 가로질러 무엇이 밀려 있는지와 검토 안내 (Mechanism §9-4).

대화 하나의 커서는 `conversation_state`, 포착은 `conversation_capture`, 검토를 닫는 일은
`conversation_review`, 인용은 `conversation_cite`가 맡는다. 여기에는 여러 대화에 걸친 일이 남는다:
밀린 대화의 목록과 따라잡기(`list_pending`·`capture_known`·`catchup`), 그리고 세션에 싣는 검토
안내(`prompt`). 공유 scope 기억에 쓴 것은 검토를 닫은 것이 아니다.
"""
from __future__ import annotations

import hashlib
import json
import os

from . import (conversation_capture, conversation_review, conversation_state, core, distillation, node_checks,
               organization, raw, write, transcript_files)
from .vault import Vault


def unlanded(vault: Vault) -> str:
    """소속 scope가 없는 대화의 검토가 소속을 정하는 순서(시행령 §3 9항 · Mechanism §9-4 3항)."""
    return (
        "이 대화는 아직 소속 scope가 없다(시행령 §3 9항). 먼저 남길 것이 있는지 판정한다. 남길 것이 없으면 "
        "소속을 정하지 않고 no_value로 닫는다 — 닫음은 이번 범위에만 미치고, 대화가 이어지면 이어진 턴을 "
        "다시 판정한다. 남길 것이 있으면 다음 순서로 소속을 정한다. ① 그 활동의 scope가 있으면 그 scope다 — "
        "같은 활동의 다른 이름, 같은 저장소와 그 포크, 기존 scope의 활동 범위 안의 작업이 든다 — 다른 활동의 "
        "저장소를 모듈로 품은 프로젝트에서 그 모듈을 고친 작업은 그 모듈의 활동의 scope다. "
        "② 없고 고유한 작업 대상(저장소·연구 과제 등)을 가진, 이어지는 활동이면 그 정본 이름으로 scope를 "
        "새로 연다. ③ 둘 다 아니면 scope를 열지 않는다 — Person에 둘 것만 Person Space에 두고, 그 근거 "
        "라운드는 cite_round로 인용한다(인용 기록은 루트 `_cited/` 한 자리라 착지가 필요 없다). Person에 쓰기 전에 그 facet의 율령 노드"
        "(`<facet>-law`)를 먼저 읽고, 활성인 율령이 없으면 쓰지 않는다 — 대화 안에서는 율령 작성을 사용자에게 "
        "제안하고, 정기 실행은 deferred로 남긴다(시행령 §4 8항). "
        f"①·②의 노드는 create_node(space=\"{vault.scope}/<이름>\")로 그 scope에 쓴다 — 성공한 쓰기가 그 scope를 "
        "이 프로젝트의 결속에 더한다. 그 scope를 overview로 보고 허브 트리에서 노드의 자리를 정한다.\n")


def _known_pending(vault: Vault, limit: int | None, tried=None) -> tuple[list, int, list, list]:
    """처리할 대화를 고른다. 같은 자리에서 고른 횟수(`tried(s)`)가 적은 대화가 먼저다 — 진척
    없이 끝난 대화가 예산을 계속 차지하지 않는다. 횟수가 같으면 다음 미검토 턴이 이른 대화가
    먼저다 — 옛 내용이 새 내용 뒤에 쓰이지 않게 한다. 턴의 시각을 모르면 상태를 마지막으로
    고친 시각으로 본다. `limit`이 None이면 정렬한 대기 전체를 돌려준다."""
    if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= 100):
        raise ValueError("limit must be between 1 and 100")
    probe = conversation_state.state_path(vault, "claude", "inventory")
    prefix = "-".join(probe.name.split("-")[:3]) + "-"
    paths = sorted(probe.parent.glob(prefix + "*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    states, errors, awaiting_native = [], [], []
    for p in paths:
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
            with conversation_state.state_lock(vault, value["harness"], value["conversation_id"]) as locked_path:
                if p != locked_path:
                    raise ValueError("state filename identity mismatch")
                s = conversation_state.load_state(vault, p, value["harness"], value["conversation_id"])
                current = conversation_capture.current_view(vault, s, p)
            changed, missing, native_path = False, False, None
            try:
                native_path = conversation_capture.locate_transcript(vault,
                    s["harness"], s["conversation_id"], s.get("capture_path") or s.get("transcript_path"))
                if native_path:
                    changed = s.get("native_fingerprint") != transcript_files.native_fingerprint(
                        native_path, s["harness"], s["conversation_id"])
                else:
                    changed, missing = True, True
            except FileNotFoundError:
                changed, missing = True, True
            except (OSError, ValueError) as exc:
                changed = True
                errors.append({"state": str(p), "phase": "native_lookup", "error": str(exc)})
                # Capture still fails closed, but stored raw remains reviewable.
            if missing or native_path and ("source_missing_since" in s or "source_lost" in s):
                with conversation_state.state_lock(vault, s["harness"], s["conversation_id"]) as locked_path:
                    s = conversation_state.load_state(vault, locked_path, s["harness"], s["conversation_id"])
                    if conversation_capture.source_clock(s, missing):
                        conversation_state.save_state(locked_path, s)
                    current = conversation_capture.current_view(vault, s, locked_path)
            if missing and s.get("source_lost") and not current["pending_refs"]:
                continue  # 닫힌 커서는 원본이 돌아올 때까지 다시 포착하지도, 나열하지도 않는다
            if missing and not (s["prompt_count"] or s["rounds"] or s.get("native_fingerprint")):
                # SessionStart may precede file creation, or no user turn ever
                # follows. Keep the cursor; do not spend the bounded work slots.
                awaiting_native.append({"harness": s["harness"], "conversation_id": s["conversation_id"],
                                        "state": "awaiting_native", "transcript_path": s["transcript_path"]})
                continue
            # A worker without the conversation takes stored rounds, receipts, and the
            # unreviewed turns of a conversation that ended on this device (Mechanism §9-4 3항).
            pending = current["pending_refs"]
            # An ended Claude conversation whose last turn never finished is read once more:
            # the idle close turns that tail into an interrupted round to review.
            idle_tail = s["harness"] == "claude" and s["capture_pending"] and not s["capture_error"]
            # A missing-source error whose file is found again is retried even when it reads
            # as before: compressing a rollout keeps its fingerprint (#203). Other errors wait
            # for a change — retrying an unchanged source only repeats them.
            retry = current["capture_recovery"]["state"] == "retryable"
            if (changed or retry or current["repair_pending"]
                    or any(not raw.is_native(ref) for ref in pending)
                    or (pending or idle_tail) and conversation_capture.ended_source(vault, s)):
                at = conversation_capture.turn_at(s, native_path)
                states.append((tried(s) if tried else 0, p.stat().st_mtime if at is None else at, s))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"state": str(p), "error": str(exc)})
    states = [s for *_, s in sorted(states, key=lambda t: t[:2])]
    if limit is None:
        return states, 0, errors, awaiting_native
    return states[:limit], max(0, len(states) - limit), errors, awaiting_native


def list_pending(vault: Vault, limit: int = 20) -> dict:
    states, remaining, errors, awaiting_native = _known_pending(vault, limit)
    jobs = []
    for s in states:
        if not conversation_capture.state_view(vault, s)["pending_refs"]:
            continue
        job = prompt(vault, s["harness"], s["conversation_id"], offline=True)
        if not job["pending_refs"]:
            continue
        job["prompt"] = job.pop("text")
        jobs.append(job)
    return {"ok": not errors, "jobs": jobs, "remaining": remaining, "errors": errors,
            "awaiting_native": awaiting_native}


def capture_known(vault: Vault, tried=None) -> dict:
    """이 vault가 추적하는 대기 대화를 모두 포착한다. 모델을 부르지 않는다 — 하루 묶음은 공급자의
    한도와 무관하게 하루 시작에 한 번 부른다(#175). 실패마다 이번에 새로 생긴 것인지(`new_error`)를
    적는다 — 원본이 지워진 대화는 날마다 같은 오류로 다시 실패한다."""
    states, _, errors, awaiting_native = _known_pending(vault, None, tried)
    captures = []
    for s in states:
        try:
            result = conversation_capture.capture(vault, s["harness"], s["conversation_id"],
                             s.get("capture_path") or s["transcript_path"], s["session"], s.get("space"))
            if result.get("appended") or not result.get("ok"):   # 실행 기록에는 바뀐 것만 남긴다
                captures.append({**{k: result.get(k) for k in (
                    "harness", "conversation_id", "ok", "appended", "capture_error", "capture_recovery")},
                    "new_error": not result.get("ok") and result.get("capture_error") != s.get("capture_error")})
        except Exception as exc:
            errors.append({"harness": s["harness"], "conversation_id": s["conversation_id"], "error": str(exc)})
    return {"ok": not errors and all(r["ok"] for r in captures), "captures": captures, "errors": errors,
            "awaiting_native": awaiting_native}


def _copies_by_parent(vault: Vault) -> dict:
    """대화마다 그 앞부분을 이어받은 사본(resume·fork)의 커서와 갈래 지점 — 이어받은 마지막 턴의
    주인이 부모다. 커서 파일을 잠그지 않고 읽는다(검토 선택의 판단 재료일 뿐이다)."""
    probe = conversation_state.state_path(vault, "claude", "inventory")
    prefix = "-".join(probe.name.split("-")[:3]) + "-"
    out = {}
    for p in probe.parent.glob(prefix + "*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            last = ((c.get("inherited") or {}).get("rounds") or [None])[-1]
            if not (last and c.get("rounds") and raw.is_native(last.get("ref"))):
                continue
            _, harness, parent, _ = last["ref"].split(":", 3)
            if harness == c.get("harness") and parent != c.get("conversation_id"):
                out.setdefault((harness, parent), []).append((c, last["id"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return out


def _cut_before(c: dict, native_path: str | None, message: str) -> bool:
    """사본이 원래 대화의 그 메시지 바로 앞에서 잘렸다는 흔적. Claude 앱은 메시지로 되감을 때 그 메시지
    시점의 파일 이력 스냅숏(`file-history-snapshot`의 `messageId`)을 사본에 옮긴다 — 턴 경계에서 그냥
    가른 사본에는 없다. Codex는 되감기를 같은 스레드에 남기므로(`thread/revert`, #208) fork에 흔적이 없다."""
    if c["harness"] != "claude" or not native_path:
        return False
    try:
        for line in transcript_files.native_lines(native_path, c["harness"], c["conversation_id"]):
            if message.encode() not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "file-history-snapshot" and row.get("messageId") == message:
                return True
    except (OSError, ValueError):
        return False
    return False


def _fork_rewound(vault: Vault, s: dict, copies: list) -> dict | None:
    """사본으로 되감은 턴(#235). Claude 앱은 메시지로 되감으면 그 메시지 바로 앞에서 사본을 만들어 거기서
    잇는다 — 원래 대화에는 버린 턴이 남는다. 어느 사본도 이어받지 않은 원래 대화의 끝 턴들은, 갈래 다음
    메시지 앞에서 잘린 흔적(`_cut_before`)을 지닌 사본이 그 마지막 턴보다 뒤에 첫 턴을 열었고 원래 대화가
    조용해졌으면 버린 갈래다. 흔적 없이 턴 경계에서 가른 사본은 시각만으로 되감기와 가를 수 없어 판단에
    쓰지 않는다. 검토하지 않은 것만 돌려준다. 원래 대화가 다시 이어지면 이 판단은 서지 않는다
    (`_capture_apply`가 되살린다)."""
    ids = [r["id"] for r in s["rounds"]]
    kept = [ids.index(branch) for _, branch in copies if branch in ids]
    if not kept or s.get("repair_pending"):
        return None
    start = max(max(kept) + 1, s["reviewed_count"])
    native = conversation_capture.ended_source(vault, s)
    if start >= len(ids) or not native:
        return None
    last = conversation_capture.turn_at(s, native, refs=[s["rounds"][-1]["ref"]])
    for c, branch in copies:
        cut = ids.index(branch) + 1 if branch in ids else len(ids)
        if cut >= len(ids):
            continue
        try:
            own = conversation_capture.locate_transcript(vault, c["harness"], c["conversation_id"], c.get("capture_path") or c.get("transcript_path"))
        except (OSError, ValueError, KeyError):
            continue
        born = conversation_capture.turn_at(c, own, refs=[c["rounds"][0]["ref"]])
        if (last is not None and born is not None and born > last
                and _cut_before(c, own, ids[cut].split(":", 1)[0])):
            return {"start": start, "copy": c["conversation_id"], "born": born}
    return None


def _drop_fork_rewound(vault: Vault, harness: str, conversation_id: str, copies: list) -> int:
    """`_fork_rewound`의 턴을 커서에서 뺀다 — Codex의 되돌린 턴처럼, 추적한 적 없던 것처럼 둔다."""
    with conversation_state.state_lock(vault, harness, conversation_id) as p:
        s = conversation_state.load_state(vault, p, harness, conversation_id)
        found = _fork_rewound(vault, s, copies)
        if not found:
            return 0
        start = found["start"]
        dropped = [r["id"] for r in s["rounds"][start:]]
        s["rounds"] = s["rounds"][:start]
        s["snapshots"] = {t: v for t, v in s["snapshots"].items() if v.get("repair_refs") or v["count"] <= start}
        if s["rounds"]:
            s["snapshots"].setdefault(conversation_state.snapshot(s), {"count": len(s["rounds"]), "prompt_count": s["prompt_count"]})
        s["rewound"] = {"rounds": dropped, "copy": found["copy"], "at": core.now_iso()}
        conversation_state.save_state(p, s)
        return len(dropped)


def catchup(vault: Vault, limit: int = 20, *, max_rounds: int = conversation_state.MAX_REVIEW_ROUNDS, tried=None, capture_new: bool = True,
            exclude=frozenset(), bundle: int = 0) -> dict:
    """Bounded scheduler catch-up of known own vault states; never inject these in a normal session.
    포착(모델 비용이 없다)과 작업 선택을 나눈다. 대기 전체를 먼저 포착하고, 따라잡은 커서로
    다시 세운 순서에서 `limit`개를 고른다 — 작업이 차면 포착도 멈추던 때에는 뒤에 선 대화의 새
    턴이 추적되지 않아, 새 턴으로 다시 열린 대화가 순서에 오르지 못했다(#179). 작업을 만들지
    못한 대화(원본 유실·포착 실패 등)와 `exclude`의 작업(하루 묶음에서 그날 진척 없던 것)은 건너뛰고
    다음 대화로 `limit`을 채운다. `bundle`을 주면 그 뒤에 같은 scope의 작은 꼬리 — 대기 전체가 남은
    턴에 드는 대화 — 를 더해 모두 `bundle` 턴까지 고른다: 묶음을 띄우는 고정비가 턴 수보다 크다(#306
    D5). `capture_new=False`는 선택만 한다 — 하루 묶음은 하루 시작에 `capture_known`으로 한 번만
    포착한다(#175)."""
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    captured = capture_known(vault, tried) if capture_new else {"captures": [], "errors": [], "awaiting_native": []}
    captures, errors, awaiting_native, jobs = (captured["captures"], captured["errors"],
                                               captured["awaiting_native"], [])
    states, _, again, _ = _known_pending(vault, None, tried)
    errors += [e for e in again if e not in errors]
    copies = _copies_by_parent(vault)
    scope, room = None, 0          # 묶음의 scope와 남은 턴
    for s in states:
        if len(jobs) >= limit and room <= 0:
            break
        try:
            if (s["harness"], s["conversation_id"]) in copies and conversation_capture.state_view(vault, s)["pending_refs"]:
                # 사본으로 되감아 버린 턴은 검토에 넣지 않는다(#235)
                _drop_fork_rewound(vault, s["harness"], s["conversation_id"], copies[s["harness"], s["conversation_id"]])
            pending = conversation_capture.state_view(vault, s)["pending_refs"]
            if pending and len(jobs) >= limit and (len(pending) > room or conversation_review.review_scope(vault, s) != scope):
                continue
            if pending:
                job = prompt(vault, s["harness"], s["conversation_id"], include_organization=False,
                             max_rounds=max_rounds, offline=True)
                if job["pending_refs"] and job["key"] not in exclude:
                    job["prompt"] = job.pop("text")
                    jobs.append(job)
                    if bundle and len(jobs) == limit:
                        scope = job.get("review_scope")
                        room = bundle - len(job["pending_refs"]) if scope else 0
                    elif len(jobs) > limit:
                        room -= len(job["pending_refs"])
        except Exception as exc:
            errors.append({"harness": s["harness"], "conversation_id": s["conversation_id"], "error": str(exc)})
    return {"ok": not errors and all(r["ok"] for r in captures), "jobs": jobs,
            "captures": captures, "remaining": len(states) - len(jobs), "errors": errors,
            "awaiting_native": awaiting_native}


def prompt(vault: Vault, harness: str, conversation_id: str, *, include_organization: bool = True,
           max_rounds: int = conversation_state.MAX_REVIEW_ROUNDS, organization_in_text: bool = True,
           offline: bool = False) -> dict:
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= conversation_state.MAX_REVIEW_ROUNDS:
        raise ValueError(f"max_rounds must be between 1 and {conversation_state.MAX_REVIEW_ROUNDS}")
    with conversation_state.state_lock(vault, harness, conversation_id) as p:
        s = conversation_state.load_state(vault, p, harness, conversation_id)
        st = conversation_capture.current_view(vault, s, p)
        count = min(len(s["rounds"]), s["reviewed_count"] + max_rounds)
        if offline:
            # A worker without this conversation takes stored rounds, and original turns
            # only once the conversation has ended on this device (Mechanism §9-4 3항).
            ended = conversation_capture.ended_source(vault, s)
            stored = s["reviewed_count"]
            while stored < count and (ended or not raw.is_native(s["rounds"][stored]["ref"])):
                stored += 1
            count = stored
        if s.get("repair_pending"):
            token = min(s["repair_pending"], key=lambda t: (s["snapshots"][t]["count"], t))
            repair = s["repair_pending"][token]
            if len(repair["refs"]) > max_rounds:
                # Split the obligation itself, not just its displayed refs. Each
                # part needs its own explicit ACK; the parent verifies all parts.
                parts = []
                for start in range(0, len(repair["refs"]), max_rounds):
                    refs = repair["refs"][start:start + max_rounds]
                    child = "sha256:" + hashlib.sha256(json.dumps(
                        [token, repair["review_count"], refs], sort_keys=True).encode()).hexdigest()
                    s["snapshots"][child] = {k: s["snapshots"][token][k]
                                              for k in ("count", "prompt_count")}
                    s["snapshots"][child]["repair_refs"] = refs
                    s["repair_pending"][child] = {**repair, "refs": refs}
                    parts.append(child)
                s["snapshots"][token]["repair_parts"] = parts
                del s["repair_pending"][token]
                token = parts[0]
                conversation_state.save_state(p, s)
            st["through"] = token
            st["pending_refs"] = s["repair_pending"][token]["refs"]
            st["remaining_rounds"] = (len(s["rounds"]) - s["reviewed_count"]
                                      + sum(len(r["refs"]) for t, r in s["repair_pending"].items()
                                            if t != token))
            st["repair"] = s["repair_pending"][token]
        elif count > s["reviewed_count"]:
            selected = {**s, "rounds": s["rounds"][:count]}
            token = conversation_state.snapshot(selected)
            # A bounded review may end inside the initial catch-up batch. Its
            # prompt counter does not claim newer unreviewed prompts as done.
            s["snapshots"].setdefault(token, {"count": count, "prompt_count": min(
                s["prompt_count"], s["reviewed_prompt_count"] + count - s["reviewed_count"])})
            conversation_state.save_state(p, s)
            st["through"] = token
            st["pending_refs"] = [r["ref"] for r in s["rounds"][s["reviewed_count"]:count]]
            st["remaining_rounds"] = len(s["rounds"]) - count
        else:
            st["pending_refs"] = []  # an offline range that begins at an original turn is empty
        selected_refs = set(st["pending_refs"])
        st["raw_review"] = {"state": "none", "rounds": len(selected_refs), "error": None}
        if selected_refs:
            try:
                with core.mutation_lock(vault):
                    selected_rounds = [r for r in s["rounds"] if r["ref"] in selected_refs]
                    if {r["ref"] for r in selected_rounds} != selected_refs:
                        raise ValueError("selected raw coordinates are missing from the saved cursor")
                    conversation_review.verify_raw(vault, selected_rounds)
                st["raw_review"]["state"] = ("native" if all(raw.is_native(ref) for ref in selected_refs)
                                             else "verified")
            except (ValueError, OSError, write.WriteError) as exc:
                st["raw_review"].update(state="unavailable", error=str(exc))
    # One snapshot identity for ordinary hooks and dedicated workers alike.
    # A later snapshot must not collide with an immutable distillation request.
    st["key"] = "scope-review-" + st["through"].removeprefix("sha256:") if st["pending_refs"] else None
    if st.get("repair"):
        st["key"] += f"-repair-{st['repair']['review_count']}"
    text = (f"[osk 대화별 통합 대기 — {harness}/{conversation_id}]\n"
            f"공유 scope 기억 session={json.dumps(st['session'], ensure_ascii=False)}와 "
            "이 대화의 검토 상태는 별개다. 공유 기억이 비어 있어도 검토한다.\n")
    if st["capture_error"]:
        text += f"포착 진단: {st['capture_error']} — 미완료로 남았다. 본 작업은 계속할 수 있다.\n"
    if st["capture_recovery"]["next_action"]:
        text += f"포착 재개: {st['capture_recovery']['next_action']}\n"
    if st["raw_review"]["state"] == "native" and not offline:
        # The range by position and short ID: the reviewer's latest turn is usually past it (#219).
        # A Stop fork gets it as a field; its packet drops this text (#226).
        st["range"] = conversation_state.range_text(s, st["pending_refs"])
        text += (f"이번 검토 범위는 이 대화의 {st['range']}다. 원문은 vault에 "
                 "옮기지 않았다(시행령 §2 2항). 범위 밖의 턴은 이 검토의 근거가 되지 못한다 — 그 턴은 "
                 "자기 차례의 검토에서 다룬다.\n")
    elif st["raw_review"]["state"] == "verified":
        text += ("이번 검토 범위의 저장된 raw 좌표·해시는 일치한다. 원본 포착의 미완료와 구분해 "
                 "이 범위만 검토할 수 있다. 원본 소실을 no_value의 사유로 쓰거나 미포착 꼬리를 ACK하지 않는다.\n")
    elif st["raw_review"]["state"] == "unavailable":
        text += ("저장된 raw 검증 실패로 이번 범위의 증류·ACK를 보류한다. 원본 좌표·해시를 복구한 뒤 "
                 f"다시 검토한다: {st['raw_review']['error']}\n")
    if st["failed_rounds"] or st["interrupted_rounds"]:
        text += (f"원문에 실패 종료 {st['failed_rounds']}·종료 기록 없이 다음 턴으로 넘어간 부분 기록 "
                 f"{st['interrupted_rounds']}건이 포함된다. 관측 보존이며 작업 성공을 뜻하지 않는다.\n")
    if st["harness"] == "codex":
        text += ("native_trigger는 goal·heartbeat·다른 대화에서 전달된 메시지 또는 실패·중단 뒤 입력 없는 턴이다. 앞 입력은 문맥으로만 "
                 "보존하며 같은 의도의 재개라고 확정하지 않는다. 새 사용자 발화로 해석하지 말고, "
                 "명시 지시 증류와 자율 성장의 증거를 구분하라.\n")
    if st["inherited_rounds"] and st["harness"] == "codex":
        text += (f"fork 부모 대화의 과거 {st['inherited_rounds']}턴은 부모의 몫이다 — 이 대화의 검토 대상이 "
                 "아니고, 부모의 검토 대기도 그대로 남는다. 부모가 이 기기에서 추적되지 않으면 그 턴은 부모 "
                 "대화를 capture해 그쪽에서 검토한다.\n")
    elif st["inherited_rounds"]:
        text += (f"같은 scope의 부모 대화가 이미 추적한 과거 {st['inherited_rounds']}라운드는 이 대화의 "
                 "검토 대상이 아니다. 부모의 미검토 대기는 그대로 남는다.\n")
    if (st.get("coverage") or {}).get("ancestor_pending_tails"):
        text += ("fork 부모의 선언된 history 범위에 미완료 꼬리가 있다. 자식의 종료로 부모 턴을 "
                 "완료하거나 ACK하지 않는다. 부모 원본·검토 대기를 별도로 확인한다.\n")
    if st.get("repair"):
        text += ("이 snapshot은 이미 검토했지만, 그 뒤 저장 라운드가 정정되어 다시 열렸다(복구 대기). "
                 "새 대화의 완료 커서는 되감지 않았다. 정정된 라운드를 다시 읽고, 이전 증류가 정정 전 "
                 "내용에 기댔다면 현재 노드를 재검토해 별도 증류 key로 새 증거를 만든다. "
                 "같은 through로 명시적으로 재ACK하라. "
                 f"정정 사유: {st['repair']['reason']}\n")
        if st["repair"].get("corrected"):
            # A corrected cited round is not among the review's refs; name it to be read.
            text += ("정정된 라운드(`read_cited`로 정정본을 읽는다): "
                     + ", ".join(st["repair"]["corrected"]) + "\n")
        if st["repair"].get("targets"):
            text += ("그 라운드를 출처로 쓴 노드(정정본에 비추어 다시 판단한다): "
                     + ", ".join(t.get("name") or t["id"] for t in st["repair"]["targets"]) + "\n")
    jobs = []
    # 대화에 고정한 착지(capture의 space)가 검토 문맥의 scope다 — 프로젝트의 주된 scope와 다를 수
    # 있다(시행령 §3 9항). 고정이 없을 때만 결속 scope를 주된 scope부터, 없으면 결속 순서대로 본다(§9-3 1항).
    pinned = raw.scope_of_space(vault, s["space"]) if s.get("space") else None
    try:
        scopes = write.bound_scopes(vault, st["session"]) if st.get("session") and not conversation_capture.one_off(s) else []
    except (OSError, ValueError, write.WriteError):
        scopes = []
    if pinned and scopes:
        scopes = [pinned, *[x for x in scopes if x != pinned]]
    scope = scopes[0] if scopes else None       # 허브 트리의 scope — 고정한 착지, 없으면 주된 scope
    try:
        with core.mutation_lock(vault):
            jobs = (organization.pending(vault, scopes, limit=1, record=True)
                    if scopes and include_organization else [])
    except (OSError, ValueError, write.WriteError) as exc:
        text += f"참조·조직 검토는 대기 중이다: {exc}. 아래 대화 통합은 계속한다.\n"
    st["organization_jobs"] = jobs
    organization_text = organization.prompt(vault, jobs)
    if not organization_in_text:
        organization_text = ""  # the hook budgets organization_jobs as its own block (`osk.hook_text`)
    if not st["pending_refs"]:
        return {**st, "text": text + "검토할 완료 라운드가 아직 없다. 종료 꼬리는 같은 대화 재개 또는 명시 capture로 따라잡는다." + organization_text}
    command = core.cli_command(vault, "integration", "review", "--harness", harness,
                               "--conversation", conversation_id)

    def since():
        # The earliest pending turn's time bounds the receipt scan (#227); read only if needed.
        try:
            path = conversation_capture.locate_transcript(vault, harness, conversation_id,
                                      s.get("capture_path") or s.get("transcript_path"))
        except (OSError, ValueError):
            return None
        return conversation_capture.turn_at(s, path, st["pending_refs"])
    try:
        discovered = distillation.discover(vault, st["pending_refs"], since=since)
    except (ValueError, OSError, write.WriteError) as exc:
        discovered = {"proofs": [], "errors": [{"reason": str(exc)}], "truncated": False, "scanned": 0}
    st["previous_distillations"] = discovered["proofs"]
    st["proof_discovery"] = {k: discovered[k] for k in ("errors", "truncated", "scanned")}
    if discovered["proofs"]:
        text += ("기존 저장 증거가 있다. complete이면 본문을 다시 쓰지 말고 기존 key를 "
                 "preserved ACK의 targets에 재사용하라. 허브 연결만 pending이면 "
                 "update_node(name=proof.target.id, distill={resume:proof.key})를 부른 뒤 "
                 "같은 key로 ACK하라. body/summary/edges/anchors/settle/expect_hash는 보내지 않는다. "
                 "아래 증거의 실제 내용이 이번 검토 범위에 맞는지 먼저 확인하라.\n"
                 + json.dumps(discovered["proofs"], ensure_ascii=False) + "\n")
    if discovered["errors"] or discovered["truncated"]:
        text += ("기존 증류 증거 조회가 불완전하다 — 이미 인용된 턴의 영수증을 다 보지 못했다. "
                 "그 턴의 노드를 새로 만들기 전에 search로 기존 노드를 확인하고, 확인할 수 없을 때만 "
                 "deferred로 남겨라. 진단: "
                 + json.dumps(st["proof_discovery"], ensure_ascii=False) + "\n")
    text += (f"이번 검토 snapshot의 작업 키: {st['key']}\n"
             f"새 증류의 distill.key는 `{st['key']}:<대상별 고정 접미사>`로 정하라. "
             "기존 노드 ID 또는 새 노드 제목의 고정 slug를 접미사로 쓴다. key는 한 요청에 "
             "결속되므로 그 요청의 재시도와 resume에만 그대로 재사용하고, 같은 대상에 다른 "
             "내용을 더 증류하면 접미사 뒤에 -2·-3을 붙인다. 여러 대상에 같은 key를 쓰지 않는다. "
             "새 snapshot은 새 작업 키를 쓴다. ACK만 유실됐다면 위 complete 증거의 "
             "기존 key를 targets에 재사용하며, 같은 본문을 새 key로 다시 쓰지 않는다.\n")
    stored = [ref for ref in st["pending_refs"] if not raw.is_native(ref)]
    workspace = s.get("workspace") or {}
    if workspace.get("warning"):
        text += f"작업 폴더 판별: {workspace['warning']}\n"
    if workspace.get("one_off"):
        text += ("이 대화의 작업 폴더는 일회성 작업 폴더라 프로젝트 키를 결속하지 않는다(Mechanism §6-2 6항) — "
                 "쓰기 도구에 session을 주지 말고, 노드를 쓸 때마다 space를 준다.\n")
    elif st.get("session"):
        # 한 프로젝트가 여러 scope의 활동을 품는다(시행령 §3 10항) — 결속과 주된 scope를 알려 착지를 고르게 한다
        b = write.bindings(vault, st["session"])
        if b["scopes"]:
            text += (f"이 프로젝트의 결속: {', '.join(b['scopes'])}"
                     + (f" · 주된 scope {b['primary']}" if b["primary"] else
                        " · 주된 scope 없음 — 노드를 쓸 때마다 space를 준다")
                     + ". 다른 scope의 활동이면 create_node의 space에 그 scope를 준다(결속에 더해진다). "
                       "이 프로젝트 자체의 활동이 속한 scope를 확인했으면 review의 primary_scope로 지정한다.\n")
    # 시행령 §3 8항(2026-11-10 사용자 결정, #273 질문 3): 대화 검토는 결속 scope의 허브 트리만 싣는다. 다른
    # scope와 Domain의 주장은 주제 검색으로만 에이전트 앞에 놓이므로, 새 노드를 쓰기 전의 검색을 지시로 강제한다.
    text += ("노드를 새로 쓰기 전에 반드시 그 주제로 search를 먼저 불러 비슷한 기존 노드를 찾는다 — 결과에는 이 "
             "scope 밖의 다른 scope와 Domain 노드도 든다(시행령 §3 8항). 주장이 같으면 그 노드를 갱신하고, 새 노드가 "
             "찾은 노드의 주장에 실제로 기대면 derived-from으로 건다. 주제만 겹치면 걸지 않는다. search를 한 번도 "
             "부르지 않은 세션의 create_node는 거부된다(Mechanism §6-2 10항). "
             + node_checks.CLAIM_GUIDANCE + "오래 쓸 지식만 Scope 노드로 옮긴다. ")
    # 세션이 아직 결속되지 않았으면(첫 노드 쓰기 전) 이 대화가 착지한 scope로 본다
    tree_scope = scope or (raw.scope_of_space(vault, s["space"]) if s.get("space") else None)
    st["review_scope"] = tree_scope
    if not tree_scope:
        text += unlanded(vault)
    try:
        tree = organization.hub_tree(vault, organization.scope_path(vault, tree_scope)) if tree_scope else ""
    except (OSError, ValueError):
        tree = ""
    st["hub_tree"] = tree
    if tree:
        # Mechanism §9-4 3항(2026-10-06 개정): 대화 검토는 노드의 분할과 자리를 정하고, 허브의 분화는
        # 조직 검토가 맡는다. 자리의 정의는 시행령 §3 8항 그대로다.
        text += ("노드의 분할과 자리는 이 검토가 정한다. 자리는 최상위 허브에서 그 주제를 덮는 하위 허브로 내려가 "
                 "더 내려갈 곳이 없는 허브이며, 그 허브의 기존 갈래에 잇거나 없으면 새 갈래를 연다(시행령 §3 8항). "
                 "create_node의 space에는 그 군집 경로를, distill.hub에는 그 허브의 이름(경로의 마지막 마디)을 쓴다. "
                 "허브의 분화는 조직 검토가 맡으므로 하위 군집을 열지 않는다. 분화가 필요해 보이면 그 제안을 reason에 "
                 "남긴다. 이 scope의 허브 트리:\n" + tree + "\n")
    # 2026-10-05 사용자 결정(#136 M5 정보 회귀 금지, M3 `## 근거` 절). 작업자 지시(growth.prompt)와 같은 규칙.
    # 2026-11-07(#273): 본문을 고치지 않고 줄만 다는 것은 지지에 한한다 — 조건·반례는 본문도 고칠 수 있다.
    text += ("검토하는 턴이 현행 노드보다 오래됐을 수 있다. 옛 사실은 날짜를 붙인 이력으로만 더하고, "
             "현행 주장을 옛 내용으로 바꾸거나 약화·재배열하지 않는다. 선후가 불분명하면 보류한다. "
             "distill.sources에 준 라운드는 노드 끝 `# 관측` 절에 `- 날짜 · 좌표 · 지지|조건|반례 — 그 턴이 "
             "보인 것(50자 안팎)` 한 줄씩 단다 — derived-from은 노드만 받고, 엔진은 출처를 이 줄로 확인한다. "
             "노드 끝에 관측 절(`# 관측`, v5.6 전의 `## 근거`)이 이미 있으면 새로 열지 않고 거기에 잇는다. "
             "그 턴을 노드의 말로 해석했으면 해석을 같은 줄에 적는다. "
             "이미 노드에 있는 주장을 받치기만 하는 턴은 본문을 고쳐 쓰지 않고 그 줄만 단다. "
             "좌표는 링크가 아니라 코드 글자로, 회상·인용 결과의 coordinate를 쓴다(`42cf0fc0#1`, "
             "이어진 라운드는 `42cf0fc0#41–42`). ")
    turns = len(st["pending_refs"]) - len(stored)
    if turns and offline and st.get("repair"):
        # Only stored and cited rounds are corrected; the original turns in the range were not.
        text += (f"원본 턴 {turns}개는 정정되지 않았으니 원문을 다시 읽지 않는다 — 정정된 라운드만 "
                 "새로 판단한다.\n")
    elif turns and offline:
        # The conversation ended before its own review: read only these turns from this
        # device's transcript, earlier context only on demand (Mechanism §9-4 3항). MCP
        # reads them; a sandboxed worker cannot run the CLI.
        text += (f"원본 턴 {turns}개는 검토 시점 전에 끝난 대화의 미검토 턴이다. 각 턴을 "
                 "read_cited(ref=<그 원본 턴>)로 이 기기의 원본 전사에서 그 턴만 읽는다. "
                 "턴마다 선별본(≤6000자)과 바로 앞 턴의 `previous`가 온다. 앞선 맥락은 판단에 꼭 필요할 때만 "
                 "`previous`로 한 턴씩 거슬러 읽고, 대화 전체를 읽지 않는다. 노드로 옮길 지식이 나온 턴만 "
                 f"cite_round(conversation=\"{harness}/{conversation_id}\", turn=<턴 ID>)로 인용하고, 받은 "
                 "round_ref를 distill.sources로, coordinate를 관측 줄로 써 출처와 허브 Link를 완성한다.\n")
    elif turns:
        # The reviewer usually holds these turns, but not after a compaction: one call reads
        # the range (#219). Citing is the only vault write of dialogue, and only for evidence a
        # node needs (Mechanism §9 9항).
        text += (f"원본 턴 {turns}개 중 이 대화 문맥에 남은 턴은 다시 읽지 않고 현재 scope_memory와 함께 "
                 "후보를 찾는다. 압축 등으로 문맥에 없는 턴이 있으면 "
                 f"read_cited(conversation=\"{harness}/{conversation_id}\", through=아래 through)로 "
                 "이 범위만 한 번에 읽는다(턴마다 발화·응답 각 ≤1500자). 노드로 옮길 지식이 나온 범위 안 "
                 f"턴만 cite_round(conversation=\"{harness}/{conversation_id}\", quote=그 턴 사용자 발화의 "
                 "짧은 일부 또는 turn=read_cited가 준 short)로 인용하고, 받은 round_ref를 distill.sources로, "
                 "coordinate를 관측 줄로 써 출처와 허브 Link를 완성한다. 범위 안에 남길 지식이 없으면 no_value로 닫는다.\n")
    if stored:
        text += ("저장된 기록은 현재 scope_memory와 함께 read_cited(view=review) 선별본으로 본다. "
                 "보존할 주장이나 모순을 확인할 때만 query로 필요한 원료 근거를 좁혀 읽고, 원본 hash를 "
                 "출처에 쓴다. 선별본 생략은 무가치의 증거가 아니다. 원래 세션도 선의로 일했다 — "
                 "대화가 진술한 도구 결과는 그 세션의 보고로 받아 진술된 만큼 쓰고, 도구 원문이 "
                 "안 보인다는 이유로 보류하지 않는다. 진술되지 않은 것은 지어내지 않는다. "
                 "max_chars 증가·기록 전량 이어읽기·전사 전체 shell 출력으로 우회하지 않는다. "
                 "선택한 기록 출처와 허브 Link를 distill로 완성한다.\n" + "\n".join(stored) + "\n")
    text += "남길 지식이 없는 라운드까지 노드에 억지로 넣지 않는다.\n"
    text += (f"실행 명령({ 'PowerShell' if os.name == 'nt' else 'shell' }):\n{command}\n위 명령에 UTF-8 JSON을 stdin으로 전달하라: "
             + json.dumps({"through": st["through"], "outcome": "preserved|summary|no_value|deferred",
                           "reason": "검토 범위와 선택/생략 이유", "targets": [{"key": "완료한 distill key"}]}, ensure_ascii=False)
             + "\npreserved는 실제 노드·출처·허브 완료 영수증을 확인한다. summary는 현재 공유 기억의 "
             "정확한 발췌를 targets=[{text:...}]로 제출하며 노드 보존 성공으로 세지 않는다. "
             "no_value도 사유를 남기고, deferred는 대기를 유지한다. 기억 hash 변화만으로 완료되지 않는다. "
             "기계 검사는 저장·배선만 확인하며 의미 타당성은 원문과 따로 대조한다.")
    return {**st, "text": text + organization_text}
