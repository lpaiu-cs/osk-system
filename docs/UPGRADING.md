# Upgrading osk-system

**English** · [한국어](UPGRADING.ko.md)

Read this before you apply a release with a new major version. Routine updates
are covered in [Getting started, Keeping up to date](GETTING-STARTED.md#keeping-up-to-date)
and in [SETUP](SETUP.md) (Korean).

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
