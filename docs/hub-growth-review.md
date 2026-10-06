# Hub growth review repair

The 2026-09-23 audit found large ordinary nodes acting as hubs, partial reads
acknowledged as whole-Scope reviews, and organization jobs postponed behind raw
review until the daily worker timed out. A retired Desktop CLI path also forced
the in-session fallback even though the updated app was authenticated.

## Mechanism §9-4 wording — approved 2026-09-23

Approved replacement for paragraph 3:

> 3. 대화 통합과 기존 성장 실행은 변경된 scope와 기존 domain의 본문·현재
>    배치·하위 허브·지역 배선·미결 이동을 검토한다. Stop의 별도 구독 세션은
>    원 대화의 scope만, 정기 성장 실행은 선택한 scope·domain만 다룬다.
>    별도 모델 실행기를 만들지 않는다. 현재 구조 유지·기존 입구 통합·분화
>    중 본문에 맞는 선택을 하며 수량만으로 분화하지 않는다. 같은 최상위
>    군집 내부의 비고정 배치는 시행령 §3 1항을 따르고, 새 최상위 domain의
>    확정과 보호영역의 승인을 대신하지 않는다.

Approved addition to paragraph 4:

>    본문 판독은 선택된 유한 구간의 id·내용 해시와 구간별 판단 이유에
>    결속한다. 일부 구간의 검토를 군집 전체의 완료로 세지 않는다. 미검토
>    구간이 남으면 부분 진척과 다음 대상을 기록하고 보류한다. 변경되지
>    않은 구간의 판독은 이어받되, 변경된 구간은 다시 검토한다.

The user approved this wording before it was applied to Mechanism §9-4.

## Mechanism §9-4 paragraph 3 — amended 2026-10-06

The user's wording replaces the sentence on the choice among keeping, merging and
differentiating. It assigns node splitting and placement to the conversation review
and hub differentiation to the organization review, and puts the scope's hub tree
in both:

| Before | After |
|---|---|
| 별도 모델 실행기를 만들지 않는다. 현재 구조 유지·기존 입구 통합·분화 중 본문에 맞는 선택을 하며 수량만으로 분화하지 않는다. | 별도 모델 실행기를 만들지 않는다. 대화 검토는 노드의 분할과 자리(시행령 §3 8항)를 정한다. 조직 검토는 허브의 분화(시행령 §3 7항)를 맡는다. 두 검토에는 그 scope의 허브 트리를 싣는다. 허브의 분화 시에는 현재 구조 유지·기존 입구 통합·분화 중 본문에 맞는 선택을 하며 수량만으로 분화하지 않는다. |

The hub tree lists every hub of the scope as `- <cluster path> · 직속 노드 <n> — <hub
summary>`, indented by depth; nodes are counted, not listed, and folders without a hub
are not clusters. The conversation review receives it in its prompt (and the daily
worker as `hub_tree` on each scope job) with the placement rule of Bylaws §3 8 and
without opening sub-clusters; the organization review receives it as `hub_tree` on its
job and owns differentiation.

## Plain writes — decided 2026-10-07

The gap is not limited to reviews. An agent writing a node during its session also needs
the hub tree to choose a cluster folder, and an agent that picks the right folder can
still forget the hub link. The user decided:

- `overview(session=…)` returns the bound scope's hub tree as `hub_tree`, and the
  session-start notice points to it with the placement rule of Bylaws §3 8.
- `create_node` hangs the new node in the same write, as distillation does: from the
  landing cluster's hub up to the top hub, each hub that does not link the level below
  gets a `- [[title]]` line at the end of its body. A bound session that omits `space`
  lands at the scope root and is linked from the top hub, where the organization review
  sees it in the hub tree's direct node count.
- If a hub write fails, the node stays. The hubs that still do not link the level below
  are reported as `hub_links` and the reason as `placement_error`. Workbench nodes are
  not hung. Arranging links within a hub and differentiation stay with the organization
  review.

## Implementation contract

- Existing-node reuse means the same independently testable claim and conditions,
  not the next phase of the same project. Preserve the destination, read it back,
  connect both navigation levels, then fold the exact source section into a
  concise conclusion and link. Preserve facts and history without permanent
  duplicate paragraphs.
- Organization selects at most three ranges, each at most 4,000 characters.
  `checked: [{unit, reason}]` binds each assessment to a selected, still-current
  excerpt. `deferred` can checkpoint those assessments. `complete` also requires
  coverage of all current excerpts and the existing identity/reference/wiring
  checks. Receipts measure coverage declarations, not the truth of an agent's
  semantic judgment. Legacy blanket completions without coverage reopen.
- Unchanged excerpt receipts survive an append elsewhere; changed excerpts reopen.
  Review records are local state, not durable knowledge or raw conversation data.
- `work_order` is the actual execution order. The first turn rotates even when
  every queue was selected in the previous attempt. Stop forks add one organization job from
  their own Scope, reuse the same worker and retain the 600-second deadline.
- Existing Domains use `organization plan --scope '00_Domain/<name>'` (a vault
  that kept a legacy Domain root uses that root's name, such as `Domain/<name>`);
  this does not create a Domain, authorize Person analysis or override pinned
  placement.
- Only a configured `OpenAI/Codex/bin/<16-hex>/codex.exe` installation is eligible
  for sibling discovery. Forks require the exact native source version and the
  existing ChatGPT authentication/permission gates. Daily runs select the newest
  locally installed sibling even while the older binary still exists. Other CLI paths remain pinned;
  `bin/codex.exe`, PATH and another installation are never fallback candidates.

## Rollout

Use the normal review, release and instance-update path for the engine. A local
runner path can be corrected separately with a backed-up atomic JSON replacement.
Neither unit tests nor a directed cleanup prove autonomous long-term growth;
observe subsequent native Stop workers and retained node structure after
deployment. Existing blanket completion records reopen for bounded review, so
the pending count can initially increase without a capture regression.
