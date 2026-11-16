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

## Mechanism §9-4 — amended 2026-11-16

The user approved this wording on 2026-11-16 ("이대로 올려"). The reasons and the counts are in
[v6.1 계획](v6.1-milestones.md) under "비용". Two changes go together:

- **Who carries the organization review (paragraph 3).** Added: "조직 검토는 정기 성장 실행이 맡는다.
  정기 실행이 없는 설치에서만 Stop의 별도 구독 세션이 원 대화의 소속 scope의 조직 검토를 함께
  맡는다(판정은 §4-1 4항과 같다) — 같은 실행의 대화 검토가 그 scope에 쓰면 조직 검토는 계획한
  snapshot을 벗어나 판정 없이 미뤄지기 때문이다." The cross-reference in parentheses was added by the
  agent after the approval.
- **What it looks at (paragraphs 3, 4 and 5).** The review now opens a scope only for a signal: hub
  wiring problems, unresolved references, unfinished moves, or unreviewed ranges of long nodes.

| Paragraph | Before | After |
|---|---|---|
| 3, first sentence | 대화 통합과 성장 실행은 변경된 scope와 기존 domain의 본문·현재 배치·하위 허브·지역 배선·미결 이동을 검토한다. | 대화 통합과 성장 실행은 변경된 scope와 기존 domain의 현재 배치·하위 허브·지역 배선·미결 이동·미해결 참조를 검토한다. 저장된 본문을 다시 읽는 것은 읽는 값이 큰 긴 노드의 구간에 한한다(4항) — 그 밖의 본문이 맞는지는 보존할 때의 판단과, 근거가 바뀔 때의 재검토(§4-1)가 맡는다. |
| 3, binding review | (none) | 조직 검토 작업이 없는 프로젝트의 결속 판정은 `routing` 명령으로 적는다. |
| 4, after the first sentence | (none) | 배선 문제·미해결 참조·미결 이동·긴 노드의 미검토 구간이 하나도 없는 scope는 검토할 것이 없는 것이며, 이와 무관한 쓰기는 그 scope를 다시 열지 않는다. |
| 4, body reading | 본문 판독은 선택된 유한 구간의 id·내용 해시와 구간별 판단 이유에 결속한다. | 본문 판독은 긴 노드에서 고른 유한 구간의 id·내용 해시와 구간별 판단 이유에 결속한다. 긴 노드의 기준은 쓰기 응답이 본문의 길이를 알리는 기준과 같다. |
| 5 | 변경이나 미완료는 다음 실행이 이어받고 | 남은 미결은 다음 실행이 이어받고 |

Bylaws §3 is unchanged. Its paragraph 7 already names the signal: a hub that has grown long has not
been differentiated. A short hub with many direct nodes is not a signal.

The sections above describe the review as it was approved on 2026-09-23 and amended on 2026-10-06;
where they say the review reads the bodies of a changed scope, this section replaces them.

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
  their own Scope only where no daily run took rechecks in the last three days (the same rule
  as rechecks); they reuse the same worker and retain the 600-second deadline. Otherwise the
  daily run takes organization: a fork that writes in the Scope must defer it, and even when it
  does not, the conversation review crowds it out.
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
