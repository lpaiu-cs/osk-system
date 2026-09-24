# Hidden raw storage

Raw transcripts are evidence, not knowledge nodes. New records use
`00_Scope/<scope>/_raw/.records/<record>.txt`: a non-Markdown extension inside
a dot directory. (A vault that kept a legacy `= Scope` or `Scope` root uses that
root name; see [Space paths](space-layout-migration.md).) The text codec, numeric round headings, secret filtering and
append-only prefix are unchanged. Git still preserves exact bytes through the
existing `**/_raw/** -text` attribute.

A record name at the old 252-byte UTF-8 limit cannot take `.txt` in a single
255-byte filename. Its lossless physical path is
`_raw/.records/<record>/record.txt`: every component fits, and the original
name remains reversible without truncation, hashes or an alias registry.
Case and Unicode-equivalent aliases still select one canonical record.

`read_raw`, capture hooks and distillation accept both old
`[[00_Scope/<scope>/_raw/<record>.md#N]]` and new plain
`00_Scope/<scope>/_raw/.records/<record>.txt#N` coordinates. New raw Predicate
Edges use plain YAML strings, so they do not create Obsidian wiki-link nodes.
Old source coordinates, pending snapshot keys and historical receipt hashes
remain readable; migration does not rewrite node bytes or acknowledge reviews.

Unwritten v1 distillation journals replay their original wiki-form source
serialization against the original target hash. New v2 journals use plain raw
coordinates. An upgrade never replaces a reserved hash to accept changed bytes.

Obsidian's native file explorer ignores dot directories; `.txt` also is not a
native Markdown note format. This does not promise invisibility against plugins
specifically installed to expose hidden files. Raw is opened through `read_raw`,
not by clicking a note in Obsidian. See [accepted formats](https://obsidian.md/help/Files%2Band%2Bfolders/Accepted%2Bfile%2Bformats)
and the [Show Dotfiles author's description of native behavior](https://community.obsidian.md/plugins/show-dotfiles).

## Migration

After the new engine is released, approved and installed, restart all old raw
writers before migrating. Updating disk files alone does not replace engines
already loaded in other harness processes. Old clients/machines must not write
the legacy Markdown path again. Do not run this operation by pointing a new
checkout at a live vault whose old capture processes are still active.

Run with the instance's Python and engine import path configured:

```powershell
python -m osk.cli raw migrate
python -m osk.cli raw migrate --apply
python -m osk.cli raw migrate
```

The first command previews every source, destination, size and SHA-256. The
second holds the normal vault mutation lock, renames files without changing
bytes and verifies each destination hash. The final command should report zero
remaining Markdown records. An interrupted migration can be retried; conflicting
old/new copies are refused rather than overwritten. The regular capture path
also migrates a legacy record when it next appends or verifies an exact replay.

No original transcript is deleted or rewritten. The old physical filename
disappears because the same file is moved. A physical rollback requires the
reverse move as well as an engine rollback; an old engine cannot read the new
layout. Preserve the migration JSON with the rollout evidence.

Existing node wiki links are not silently rewritten: that would alter receipt
target bytes. They continue to resolve through OSK's legacy alias. Obsidian may
show those old references as unresolved nodes if its `hideUnresolved` setting is
off. Keep the existing node-only graph view during rollout; rewrite old node
citations through MCP only as a separately verified graph change. Newly written
raw citations use the plain form automatically.

## Approved governance text

The user approved these replacements on 2026-09-17. They are reflected in
Bylaws and Mechanism in this change; formal release/deployment is separate.
Paths are quoted with the `00_Scope` root that Mechanism uses since v3.21.0.

Bylaws §1.3 raw-specific sentence:

> `_raw/`의 기록은 위키링크로 감싸지 않은 `경로#라운드번호` 문자열로
> 참조하며 라운드 번호를 반드시 지정한다. 기존 raw 위키링크는 계속 해석한다.

Mechanism §8.2 raw-specific sentence:

> `_raw/` 기록은 비노드 위키링크의 예외로, 라운드 번호를 붙인 평문 좌표를
> YAML 문자열로 쓴다: `derived-from: "00_Scope/<scope>/_raw/.records/<기록 이름>.txt#N"`.
> 기존 `.md` 경로와 위키링크 표기는 계속 해석한다.

Mechanism §8.3 coordinate sentence:

> 해당 제목의 좌표는 `경로#24`이며 `#`을 한 번만 쓴다. 기존
> `[[경로#24]]`도 계속 해석한다.

Mechanism §9.5 replacement:

> 세션 기록의 물리 자리는 `00_Scope/<scope>/_raw/.records/<기록 이름>.txt`다.
> Markdown이 아닌 파일을 숨김 디렉터리에 두어 원료가 Obsidian 네이티브의
> 노트로 표시되지 않게 한다. 기록 이름은 노드 제목과 같은 이식성 규칙을
> 받으며 확장자를 포함한 파일명 길이도 검사한다. 이식성 기준으로 같은 이름은
> 같은 정본이다. 기존 `.md` 기록의 이관은 바이트와 라운드 번호를 보존하고
> 구 좌표를 계속 해석한다. 구·신 저장본이 함께 있으면 덮거나 임의로 합치지 않는다.
> 기록 이름에 `.txt`를 붙이면 파일명 상한을 넘는 경우에는 이름을 자르지 않고
> `_raw/.records/<기록 이름>/record.txt`에 둔다. 기존 좌표도 같은 기록을 가리킨다.

Mechanism §9.8 coordinate phrase:

> `경로#index` — 곧 `derived-from`에 저장된 그 값 — 를 입력으로 받는다.
> 기존 `[[경로#index]]`도 계속 해석한다.

The storage layout and coordinate syntax must be released together with these
clauses.
