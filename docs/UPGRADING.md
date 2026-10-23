# Upgrading osk-system

**English** · [한국어](UPGRADING.ko.md)

Read this before you apply a release with a new major version, or a release
listed below. Routine updates are covered in
[Getting started, Keeping up to date](GETTING-STARTED.md#keeping-up-to-date)
and in [SETUP](SETUP.md) (Korean).

## Vaults that committed `.osk/`

`.osk/` holds device-local settings. In an older vault whose ignore rules lack
`.osk/`, the sync daemon committed what lives there (run records, and
`response-growth.json` with this device's CLI paths) and sent it to every
device. The daemon now puts an ignore rule inside `.osk/` that keeps only
itself, and stops tracking what was already committed. The files stay on the
device.

- **Update every device together.** An updated device stops tracking its own
  `.osk/` before it takes another device's untracking, so its files stay. A
  device that has not been updated loses the `.osk/` files it tracked when it
  takes that change, or its sync stops on a conflict. If files were removed,
  run `osk setup` on that device to recreate its settings. Run records remain
  in Git history.
- **Pull before the daemon on a device that has been off for a while.** An
  older daemon commits before it pulls, so it can commit that device's `.osk/`
  files again. Before you start the daemon, run `git pull` to take the ignore
  rule (`.osk/.gitignore`) first, or update the engine first.

## From v5.2 to v5.3

v5.3 adds recency, centrality and locality to search ranking (Bylaws §8 3, Mechanism §8 8). It
also fixes conversation review where the live instance showed it falling short.

- It moves or rewrites no notes or records, so there is nothing to migrate and nothing to do.
  After the update, restart the harness so that its MCP server loads the new engine.
- Updating from v5.2.0 also brings the v5.2.1 fixes: subscription fork review is restored (#221),
  and a backlogged review's range can be read at once (#219·#220).

- **Search ranking changes (#170).** Relevance (character-bigram BM25) is multiplied by
  `1 + recency + centrality + locality`.
  - A node whose name equals the query still comes first.
  - `search` takes `session` (the project key). Nodes in its bound scopes are pulled forward and
    never filtered out. The session-start text points to `search(session=…)`.
  - The `score` in results is now this exposure score.
- **A Claude turn that ends in a tool call closes at the next prompt (#218).**
  - A turn ended by the desktop app's next-step box or by `ScheduleWakeup` has no `end_turn`, so the
    next human prompt joined it.
  - Now a prompt with another `promptId` closes the open turn before it.
  - The rule applies to turns tracked after this release first reads a conversation. Turns tracked
    before that keep their boundaries.
- **Conversation review is blocked less often (#226·#227·#228).**
  - A Stop fork reads the pending turns that compaction dropped from its context with one range
    read (`read_cited(conversation=…, through=…)`).
  - Proof lookup scans receipts only when a pending turn was cited, and skips receipts older than
    the earliest pending turn. A long receipt history alone no longer reads as "incomplete".
  - The notice "[osk 백그라운드 검토 대기 — …]" (background review pending) appears only while that
    conversation's own review is unfinished.
- **Rebinding records from before v5.2 read as replacements (#216).**
  - Back then a rebinding replaced the binding, so a scope's old name no longer returns to the
    binding set.
  - A binding to a scope that no longer exists goes to the organization review to be released.

## From v5.1 to v5.2

v5.2 keeps cited records in one place and makes bindings belong to the project
rather than to the session (Constitution art. 4 §3 and art. 8 §3, Bylaws §2 and
§3 9–10, Mechanism §6-2 6 and §9 5). It also runs the scheduled growth run as
daily batches sized each week, and narrows conversation review: no fork for
conversations started in the Codex app, and only the osk tools inside a fork.
It moves or rewrites no notes or records; there is nothing to migrate. On the device that hosts the scheduled run, register it again
(below).

- **Cited records are written to the root `_cited/`.** New records are created
  as `_cited/.records/<record>.txt`, not under a scope or under Person. Records
  under a scope's `_cited/` or `_raw/` and under `00_Person/_cited/` are read
  where they are as records of the same name, and new rounds of the same name
  are appended there. Old coordinates (`00_Scope/<scope>/_cited/…#N` and the
  like) still resolve. Moving legacy records to the root is a separate change
  set.
- **`cite_round` lost its `space` argument.** Citing needs no landing; a
  conversation without a scope cites right away, and scope is decided when a
  node is written. `read_cited` with no arguments lists the root records.
- **Bindings are a set per project.** The key the hook derives from the working
  folder (the former "session key") is the project key. A node or scope-memory
  write with an explicit `space` is no longer refused for a scope outside the
  binding; it adds that scope to the binding. A write without `space` goes to
  the primary scope. The primary scope is designated, not written into
  existence: a conversation review passes `primary_scope` in its
  `integration review` JSON, or run
  `osk routing primary --project <key> --scope <scope>`. **Binding records from
  before v5.2 read as the primary scope**, so existing projects keep their
  default landing.
- **The SessionStart hook injects the memory of every bound scope** (primary
  first). A project without a primary scope is told to pass `space` on every
  write.
- **The organization review audits bindings.** Projects with more than three
  bindings, with bindings that grew, or without a primary scope are listed;
  the review records keep / unbind after moving nodes / propose a split /
  designate the primary as `bindings` in `organization review`.
  `osk routing show` lists the candidates.
- **A Scope node may cite any cited record** (Constitution art. 8 §3). Moving a
  node born in another scope no longer requires rewiring its evidence. Domain's
  ban on citing records directly stands.
- **Codex compressed rollouts are read (#203).** Codex compresses a
  conversation log left untouched for 7 days to `.jsonl.zst` in place. Engines
  up to v5.1 took that for a lost source and blocked its capture and review.
  This release finds and decompresses them; byte bounds and fingerprints count
  the plain bytes, so compression alone is no new turn. Decompressing needs
  `zstandard`: **reinstall `requirements.txt` on every device.** The next
  scheduled run or `integration catchup` captures the blocked conversations
  again (procedure in [growth-status.md](growth-status.md#codex-압축본-203),
  Korean).
- **Codex reverted turns are accepted (#208).** Editing and resending a sent
  turn in Codex (`thread/revert`) takes that turn out of the conversation.
  Engines up to v5.1 then refused capture and the response growth count for
  good if the turn was already tracked. This release drops an unreviewed
  reverted turn from the cursor; nothing to do, the next capture continues
  ([growth-status.md](growth-status.md#codex-되돌리기-208), Korean).
- **The scheduled run works in daily batches (#175).** It is now
  `growth run --daily`: capture once, then batches (a Scope job alone for 900
  s, other queues three at a time for 600 s) until the day's limit or a
  1.5-hour budget. The limit is set again every 7 days from the week's inflow,
  backlog and usage. Before each batch the run reads the Codex limits without
  inference; on a day Codex is exhausted, credits included, a Claude worker
  runs the batch instead. Limit rejections are waits, not failures. **On the
  device that hosts the scheduled run, run
  `python _governance/_engine/scripts/setup.py --interactive --schedule` once,**
  with `--at HH:MM` if the run was not at 09:00 (re-registering sets the time
  again). It replaces the older task that calls this vault (an
  `osk-domain-growth` task included) with one that runs Python directly with
  `--daily`, limited to two hours under Task Scheduler and systemd. When the
  command is Codex and a Claude CLI is found, it also creates
  `.osk/growth-command-claude.json` for that worker; the Claude adapter now
  also finds the desktop app's CLI under its build-hash folder (#206). Existing command files are kept: in a Codex
  command, replace `approvals_reviewer="auto_review"` with
  `mcp_servers.osk-system.default_tools_approval_mode="approve"`. Automatic
  review approved every osk call and spent 27–29% of the scheduled run's tokens
  ([SETUP](SETUP.md), Korean, "Scope에서 Domain으로 정기 재검토").
- **Conversations started in the Codex app are not reviewed during the
  conversation (#204, Mechanism §9-3 1).** A fork could not reuse their cache, so they get neither
  a fork nor the in-session 9/15 review; the scheduled run integrates them after
  they end. `false` in place of a path in `.osk/response-growth.json` turns a
  harness off the same way.
- **Forks use only the osk tools (#85).** A Claude fork denies every call except
  the osk MCP tools and `ToolSearch`. A Codex fork runs read-only and denies
  calls outside the osk MCP with a hook, and starts only when Codex reports that
  hook enabled and trusted; otherwise the conversation falls back to the
  in-session review. Tool definitions stay as the source had them, so the cache
  holds ([response-growth.md](response-growth.md#fork-tool-restriction-85)).
- **Hooks no longer read transcripts (#192).** The start, input and Stop hooks
  hand capture and the CLI checks to a detached worker and return. `doctor`
  warns about a hook registered with a limit under 30 seconds; the
  `setup.py --interactive` run above replaces it.
- **Update every device together.** A v5.1 engine does not read the root
  `_cited/` as cited records, so nodes citing them show unresolved references
  there, and it refuses writes outside the binding.
- **Ratification.** This release ratifies the amendments to Constitution art. 3
  §6, 4 §3, 5 §2, 8 §3 and 11 §3, Bylaws §1, §2, §3 9–10 and §7 2, Mechanism
  §1, §4-1, §6-1 4, §6-2 6, §8 2, §9 5, §9-2, §9-3 and §9-4 3, and Workbench
  Contract 2–3. The reasons are in the record
  `2026-10-13-cited-root-and-project-binding`, in #189 and #190, and for
  Mechanism §9-3 1 in #175 and #211.

## From v5.0 to v5.1

v5.1 also tracks conversations that have no scope yet, and their review decides
where they belong (Bylaws §3 9). v5.1 does not move or rewrite your notes and
records, and there is nothing to migrate.

- **Conversations without a landing are reviewed.** Earlier releases did not
  track a conversation whose session key was not bound. Now its turns are
  tracked, and the review first decides whether anything is worth keeping. If
  nothing is, the conversation is closed without a scope. If something is, the
  conversation's first citation fixes its landing. After the update, the first
  hook or scheduled run captures the conversations left without a landing once
  more.
- **`cite_round` takes `space`.** A conversation without a scope lands where its
  first citation names `00_Scope/<name>`. A conversation that keeps only what
  belongs to Person cites with `00_Person`, into `00_Person/_cited/`, and writes
  Person nodes only under the facet's active statute.
- **One-off workspaces do not bind their session key.** A session opened in a
  Codex desktop chat folder, a Claude desktop scratch workspace or an
  Antigravity playground does not follow its key's binding, even an existing
  one. Give `space` on each write in such a conversation, and no `session`. The
  engine's rules apply only on Windows; for another OS or harness, add rules as
  described in [SETUP](SETUP.md) (Korean), "일회성 작업 폴더".
- **Recheck marks recorded before v5.0 (v4.x) hold.** The carry at session start and in
  scheduled runs now also rewrites, in the current definition, that a version was
  made by a recheck (`cascade`) and that a correction waits for your review.
  Editing only a `## 근거` section no longer releases either mark.
- **Update every device together.** A v5.0 engine reads what v5.1 writes, except
  the records in `00_Person/_cited/`, which it does not read as cited records (Person
  nodes that cite them show dangling references there). It also binds the keys of
  one-off workspaces and does not track conversations without a scope.
- **Ratification.** This release ratifies the amendments to Constitution
  Article 6 (8), Bylaws §2 8, §3 1 and §3 9, and Mechanism §1, §4-1 2, §6-2 3,
  §6-2 6, §9 5 and §9-4 3. Their reasons are in #179 and #177.

## From v4.1 to v5.0

v5.0 stops copying conversations into the vault. The hooks track each finished
turn on the device, and the conversation stays in the harness's transcript. A
node cites only the turns it needs: the new MCP tool `cite_round` copies your
words from the transcript into `00_Scope/<scope>/_cited/`. v5.0 does not move or
rewrite your notes and records, and there is nothing to migrate.

- **Rename the tools in client settings.** `cite_round` and `read_cited`
  replace `append_raw` and `read_raw`; `read_cited` takes the same arguments as
  `read_raw`. If an MCP client's allow or deny list names the old tools, such
  as `autoApprove` or `disabledTools`, a Claude Code permission rule like
  `mcp__osk-system__read_raw`, or a Codex approval setting, change it to the
  new names. The CLI commands `raw append` and `raw status` are gone;
  `raw migrate` stays.
- **`_raw/` stays.** Records that earlier releases wrote under `_raw/` stay
  where they are and stay readable, and the coordinates that nodes cite keep
  resolving. A conversation that already has a `_raw/` record adds its new
  citations there. Rounds stored before the update and not yet reviewed stay in
  the review queue.
- **New turns are reviewed in their conversation.** The conversation reviews
  them at turns 9 and 15, or its Stop fork does. A conversation that ends
  before its review is picked up by the daily or explicit `growth run`, which
  reads only its unreviewed turns from this device's transcript through
  `read_cited`. The run also reviews rounds stored in `_raw/`.
- **Stalled Claude turns are reviewed too.** A Claude turn left without a final
  answer for 12 hours closes as an interrupted round and goes to review, and a
  message you send while a turn runs stays among that turn's words. Turns tracked
  before the update keep their boundaries and wording.
- **Closed reviews stay closed.** After a review is acknowledged, osk checks
  only that its node still exists, stays in its top-level cluster and still
  cites its source; a break is reported in `growth status`, not reopened.
  Editing the node, moving its hub link or pruning shared memory no longer
  sends the conversation back for review. Repair entries that the earlier
  recheck left are closed the first time osk sees that conversation. If you
  correct a stored or cited round, register it with `integration reopen`.
  Scripted `codex exec` runs, osk's own scheduled runs included, are neither
  tracked nor reviewed.
- **Full-body edits show what they dropped.** A full-body `update_node` also
  returns `dropped`: the paragraphs, list items and headings of the old body that
  the new body no longer contains word for word. Anything dropped by mistake can
  be restored with an anchor edit.
- **Update every device together.** A recheck that the agent could not judge
  is recorded in the growth ledger as a deferral (`outcome: deferred`). A v4.1
  engine reads that record as ledger damage and stops its growth runs, so bring
  every device that syncs the vault to this release before you use it.
- **Ratification.** The amendment to the governing documents behind this
  change takes effect only when a release ratifies it. Its reasons are recorded
  in `_governance/records/2026-10-04-cited-capture.md`.

## From v3.x to v4

v4 keeps reading every layout and ledger that v3 wrote
([FORMAT §8](FORMAT.md#8-compatibility-promise)). It stops accepting a few
placements and values that v3 let through, and it records a few things that v3
did not. It does not move or rewrite your notes and records.

Run the commands from the vault root with `PYTHONPATH` set, as in
[Getting started, Step 2](GETTING-STARTED.md#step-2-install-the-engine-and-record-the-release-baseline).
The examples use the macOS/Linux form (`.venv/bin/python`). On Windows, use
`.venv\Scripts\python.exe`. Where a path starts with `00_Scope`, use your own
Space root (`00_Scope`, `Scope` or `= Scope`). Do not rename legacy roots by
hand ([space-layout migration](space-layout-migration.md)).

### Update

1. Push the vault to your private remote, or back it up.
2. Update one device as usual: `.venv/bin/python -m osk.update --to v4.0.0 --apply`,
   read the plan, then run the same command again to apply it. Coming from
   v3.20.x, follow the first-transition note in [SETUP](SETUP.md) instead.
3. Restart every Claude Code and Codex session, and reinstall the requirements
   if `requirements.txt` was updated.
4. **Record the protection of `_governance`** if `.venv/bin/python -m osk.cli status`
   does not list it under `protected_regions`. The v3 updater that applied v4
   cannot designate it. Run the same-tag update once more, plan and confirm:
   `.venv/bin/python -m osk.update --to v4.0.0 --apply`, twice. It changes no
   files and reports `"governance_protected": "established"`. Do this on one
   device only. If the plan says `"protect": "withheld"`, the files listed under
   `unattested` differ from the release. Often they are `<file>.local-<version>`
   copies left by an earlier `--adopt`: keep what you need elsewhere, delete
   them, and run the same-tag update again. If you changed a governance document
   on purpose, review it and protect the folder yourself with
   `.venv/bin/python -m osk.cli protect _governance`.
5. Run `.venv/bin/python -m osk.cli validate` and fix what it reports (below).
6. Commit. On the other devices: pull, restart the sessions, and reinstall the
   requirements if they changed. Update every device that writes to the vault.
   A v3 device can still create what v4 rejects.

### What v4 records on its first run

All of it is appended, never rewritten. Commit it, or let the sync daemon.

- `00_Scope/Workbench/_ledger/routing.jsonl`: one ownership row for a session
  key, written by the first session start under v4 in a Git repository
  (reason `저장소 동일성 기록`), and not again.
- `approvals.jsonl`: one `protect` row for `_governance` (step 4), with its
  content-addressed objects in `_ledger/approved/objects/`.
- `rechecks.jsonl` (new): one `bound` row with reason `기준선` for each
  `derived-from` pair, written once by the first session start, node write or
  growth run. From then on, when what a node cites changes, the citing node
  becomes a recheck candidate: the body of a cited node, the content of a cited
  file, or the section of a cited heading. A change to a node's summary or links
  alone does not count. See the recheck section of [SETUP](SETUP.md).
- `osk-repo-identity` in the Git directory of each project repository: a cache
  outside the vault that Git does not track.

### What v4 no longer accepts

`validate` names every case. The files stay as they are until you act.

| v3 let it through | v4 | What to do |
|---|---|---|
| Nodes in a folder whose name begins with `_` or `.` (`_inbox`, `W1/_drafts`), or a node file whose name begins with `.` | Not a node place: not indexed, read, searched or written. `validate` fails with `비노드 구획에 노드형 파일: <path>` ("node-shaped file in a non-node area") | [Move them](#moving-nodes-out-of-underscore-and-dot-folders) |
| A node file directly under a Space root (`00_Scope/note.md`) | Not a node place; `validate` reports it | Move it into a cluster |
| `author` other than `user` or `agent`; a `drafter` that is not a lowercase model name; a `created` or `updated` time that does not exist on the calendar | Contract violation: `validate` fails and the node cannot be updated | Fix the value in the frontmatter |
| Two files with the same `id`, or one node file reachable at two places through a file-system link | Reading, writing and moving it are refused by name as well as by id; the refusal says how to resolve it | Keep one copy; remove the other copy or the link |
| A move that breaks a reference rule (a Scope node linking another scope, a Domain node citing `_raw`) | `move_nodes` refuses it before writing | Fix the references, then move |

### Session keys

- Repositories that share a folder name but not a root commit no longer share a
  session key. The first of them to start a session under v4 keeps the name; the
  others get `<name>-<first 8 characters of the root commit>` and start unbound.
- Git submodules and worktrees of bare repositories now use their own name
  instead of `modules`, and also start unbound.
- To keep writing into the scope such a repository used before, give that scope
  as `space` on its first write; several keys may share one scope. Folders
  outside Git still share a key by name.

### Moving nodes out of underscore and dot folders

v4 does not move them for you: a rename changes paths that ledgers, approvals
and `_raw` coordinates record. For each folder that `validate` reports:

1. If the folder is a Scope, settle its pending evictions first. List them with
   `.venv/bin/python -m osk.cli tidy list` and record each disposition with
   `tidy settle`.
2. Check protected regions before you move anything. `protected_regions` in
   `.venv/bin/python -m osk.cli status` lists each region and its state.
   - If the folder itself, or a folder under it, is a protected region, first
     settle each such region's pending changeset with `approve` or `revert`.
     Then release it with `.venv/bin/python -m osk.cli unprotect <old path>`.
     A protection is tied to its path and does not follow a move. If you move
     the folder without releasing the protection, three things are blocked. The
     old path can no longer be approved, because the directory is gone. It
     cannot be released either, because its changes are pending. The new path
     stays unprotected.
   - If only a folder above it is a protected region, there is nothing to do
     here.
3. Rename the folder and its hub to a name without the prefix. The hub is the
   node named like its folder:

   ```bash
   git mv "00_Scope/W1/_drafts" "00_Scope/W1/drafts"
   git mv "00_Scope/W1/drafts/_drafts.md" "00_Scope/W1/drafts/drafts.md"
   ```

   Right away, protect each region you released in step 2 again, at its new
   path: `.venv/bin/python -m osk.cli protect <new path>`. The working copy at
   that moment becomes the initial approved state, so do this before you change
   anything else. Later edits then become that region's changeset, which you
   review in step 5.

   The hub's title changes with its file name, so update the links to it, such
   as `[[_drafts]]` in the parent hub.
4. If you renamed a top-level Scope, such as `00_Scope/_inbox` to
   `00_Scope/inbox`:
   - Rename its scope memory, `00_Scope/Workbench/_scope_memory/_inbox.md`, to
     `inbox.md`.
   - Its `_raw/` moved with it, so `derived-from` coordinates that spell the old
     path (`00_Scope/_inbox/_raw/…#N`) no longer resolve. `validate` lists them
     under `dangling_refs`; replace the old path in each.
   - Rebind each session key that was bound to it. The key is the `session="…"`
     shown at session start. There is no command for this, so call the engine
     function that appends the new binding (Mechanism §6-2 6: a binding changes
     by a new row):

     ```bash
     .venv/bin/python -c "from osk import write; print(write.bind_session('<key>', 'inbox', 'v4 upgrade'))"
     ```

5. Review the changesets of protected regions. In a region that contains the
   folder, the move is the changeset. In a region you protected again in step 3,
   the links and coordinates you fixed afterwards are. Review each, then approve
   it with `.venv/bin/python -m osk.cli approve <region>`.
6. Commit, restart the sessions, and run `validate` again.

If you already moved a protected folder without releasing it, rename the folder
and its hub back, then start again from step 2.

For a node file whose name begins with `.`, rename the file without the dot.

### Other changes you may notice

- In v3, `update_node` treated every heading of one node as the same basis:
  adding `[[X#B]]` next to `[[X#A]]` changed nothing, and removing one removed
  both. v4 keeps each heading. If a node relies on several sections of another
  node, check that all of them are still cited.
- A node id given as `derived-from` is stored as the node's title link.
- New warnings, not failures: `governance_unprotected`, `duplicate_edges` and
  `rechecks`.
- The MCP server does not start if the installed `mcp` package cannot refuse
  unknown arguments. `_governance/_engine/constraints.txt` pins the versions that
  CI tested.
