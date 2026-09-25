# osk-system on-disk format

> **Non-normative commentary.** This document explains the
> [`_governance/Mechanism.md`](../_governance/Mechanism.md) of the release it
> ships with, as a commentary under [Bylaws](../_governance/Bylaws.md) §10 5.
> The Mechanism, written in Korean, governs. Where this text and the Mechanism
> conflict, the Mechanism wins.

Citations such as "Mechanism §3 1" name a section and its numbered item. Other
governing documents are cited by name: [Constitution](../_governance/Constitution.md),
[Bylaws](../_governance/Bylaws.md) and the
[Workbench contract](../_governance/Workbench-Contract.md). A statement without
a citation describes what the engine in `_governance/_engine/osk/` does where
the Mechanism leaves the detail open. Paths are relative to the vault root and
use `/`.

| Term | Meaning |
|---|---|
| Space | One of three top-level knowledge areas: Scope, Domain, Person |
| scope, domain, facet | A top-level cluster in the Scope, Domain or Person Space |
| cluster | A directory of nodes; clusters nest |
| hub | The node named like its cluster directory |
| node | A Markdown file with the frontmatter of section 2 |
| Predicate Edge | A typed reference in frontmatter: `derived-from` or `conflicts` |
| ledger | An append-only JSON Lines file under `_ledger/` |
| region | A directory under approval control (a protected region) |
| raw record | The append-only transcript of one session, in rounds |
| scope memory | One short shared note per scope |

Contents: [1. Vault layout](#1-vault-layout) ·
[2. Node files](#2-node-files) · [3. Identity](#3-identity) ·
[4. Ledgers](#4-ledgers) · [5. Approval store](#5-approval-store) ·
[6. Raw records and scope memory](#6-raw-records-and-scope-memory) ·
[7. Release attestation and updates](#7-release-attestation-and-updates) ·
[8. Compatibility promise](#8-compatibility-promise)

## 1. Vault layout

A vault is one self-contained directory tree, normally a Git repository
(Mechanism §1 1).

| Path | Contents | Nodes |
|---|---|---|
| `00_Scope/<scope>/` | a scope: nodes, sub-clusters, and `_raw/` | yes |
| `00_Scope/Workbench/` | the operational scope (table below) | in `transit/` only |
| `00_Domain/<domain>/` | a domain | yes |
| `00_Person/<facet>/` | a facet | yes |
| `_governance/` | the governing documents and `records/` | yes, special |
| `_governance/_engine/` | the engine, sync tools and test suite | no |
| `_sources/` | shared source material such as images and PDFs | no |
| `.osk/` | device-owned configuration, update transactions and run outputs (section 7) | no |
| any other top-level directory (`docs/`, …) | support files | no |

(Mechanism §1 2 and §1 4.)

**Space roots.** The roots are `00_Scope`, `00_Domain` and `00_Person`
(Mechanism §1 4). The engine also reads a Space root whose prefix differs
(`= Scope`, `Scope`, and likewise for Domain and Person) and uses, per Space,
the root that contains data. A root that contains only `.gitkeep` or
file-manager metadata (`.DS_Store`, `Thumbs.db`, `desktop.ini`, `._*` files)
counts as empty. If two roots of one Space contain data, the engine stops and
merges nothing. A Space root is a local directory, not a symbolic link or
junction. Engine updates never rename a root, and paths stored in ledgers,
approval objects and raw coordinates are never rewritten; a stored path that
begins with any of the three spellings resolves to the root in use. Renaming a
root, or moving data between roots, is a separate reviewed change.

**Clusters and hubs.** A cluster is a directory. A directory directly under a
Space root is a top-level cluster; a directory inside a cluster is a
sub-cluster, to any depth. A node belongs to its top-level cluster however deep
it sits (Mechanism §1 2). The hub of a cluster is the node whose file name
equals the directory name, as in `00_Domain/Chess/Chess.md`; nothing inside
the file marks it (Bylaws §3 6–7). Nodes live only inside clusters: a file
directly in a Space root is not a node. Outside Workbench, the first node the
engine creates in an empty cluster is that cluster's hub. It opens a sub-cluster
only inside a cluster that has a hub (Mechanism §6-2 3).

**Workbench.** `00_Scope/Workbench/` is a scope with a fixed inner layout
(Mechanism §1 3; Workbench contract §2):

| Path | Contents |
|---|---|
| files directly in `Workbench/` | work state, not nodes |
| `transit/` | transit nodes, the only nodes in Workbench |
| `_ledger/` | the ledgers (section 4) and the approval store (section 5) |
| `_raw/` | raw records of Workbench sessions |
| `_scope_memory/` | one scope-memory file per scope (section 6.2) |

**Where nodes live.** Nodes live in the node compartments: the clusters of the
three Spaces, `Workbench/transit/`, and `_governance/` (Mechanism §1 4). Inside
them, these are not node locations and not clusters:

- a directory whose name begins with `_` or `.`, and everything below it;
- a file whose name begins with `.`.

A file whose name begins with `_` is a node. The `.md` extension matches in any
letter case, on every operating system: `Note.MD` is a node. Governance nodes
follow the node contract, are never written through the MCP surface, and do
not count in search or centrality (Mechanism §1 4).

Outside the node compartments, a Markdown file whose first line is `---` is a
layout violation: `_sources/`, `_ledger/`, the Workbench root, `docs/` and the
engine directory never contain node-shaped files. Directories at the vault
root whose names begin with `.` (`.git`, `.obsidian`, …) are not scanned.

**Device-owned state.** Locks, confirmation markers, recovery markers and work
queues are not part of the vault. They live in the repository's Git directory
(the common directory for worktrees), or in the system temporary directory when
the vault has no Git directory, and they are never synchronized. `.osk/`
inside the vault is untracked.

**Git attributes.** The vault's `.gitattributes` checks every text file out
with LF line endings (`* text=auto eol=lf`), because approval trees and update
checks hash raw bytes. It exempts `_ledger/approved/objects/`, `_raw/` and
`_scope_memory/` from conversion (`-text`), because their bytes are content.
It merges `_ledger/**/*.jsonl` with `merge=union`, so that lines appended on
two devices both survive (Mechanism §3 1).

## 2. Node files

A node is a UTF-8 file without a byte-order mark, named `<title>.md`. It
begins with YAML frontmatter between two `---` lines; the body follows.

```text
---
id: "260802-1720-k7f2m9x3"
created: "2026-08-02 15:30 (KST)"
updated: "2026-08-02 15:30 (KST)"
author: "agent"
drafter: "opus-5.5"
summary: "One line, at most 80 characters, without links"
derived-from: ["[[Source node]]", "00_Scope/my-app/_raw/.records/session-1.txt#12"]
---

Body in Markdown, with links such as [[Another node]].
```

### 2.1 Frontmatter

Reading rules:

- CR LF and a lone CR read as LF.
- The first line is exactly `---`. The frontmatter ends at the next line that
  is exactly `---` and is followed by a newline.
- The frontmatter is a YAML mapping. A duplicate key is an error. Flow
  collections nest at most 32 levels of `[` and `{`, counting brackets inside
  quoted strings.
- A file that breaks these rules is *broken*. The validator and the
  `overview` tool list it; it is not a node for search, reads or writes.

Fields (Mechanism §2; Bylaws §1):

| Field | Required | Value |
|---|---|---|
| `id` | yes | `YYMMDD-ssss-rrrrrrrr`, see below |
| `created` | yes | `YYYY-MM-DD hh:mm (KST)`, a real calendar time |
| `updated` | yes | same format as `created` |
| `author` | yes | `user` or `agent` |
| `drafter` | yes | `user`, `agent`, or a model name |
| `summary` | yes | one non-empty line, at most 80 characters, no `[[` |
| `derived-from` | no | one target or a list of targets (section 2.3) |
| `conflicts` | no | one target or a list of targets (section 2.3) |

The six required fields come first, in this order; the Predicate Edges follow
in any order (Mechanism §2 5). No other key is allowed.

- **`id`** (Mechanism §2 1): the creation date as six digits, a hyphen, the
  seconds since midnight in base 36 (`0-9a-z`) padded to four characters, a
  hyphen, and eight random lowercase base-36 characters. A random part of four
  characters is also valid. The date part equals the date of `created`: both
  come from the same KST clock. The engine assigns ids.
- **`created`, `updated`** (Mechanism §2 2): minute precision, with the fixed
  suffix `(KST)`. `updated` changes when the node's content changes. Moving a
  node does not change it, and neither does attaching or removing a `conflicts`
  case marker (Bylaws §1 4).
- **`author`** (Mechanism §2 4): `user` for what the user wrote, `agent`
  otherwise. Nodes created through the MCP surface have `agent`.
- **`drafter`** (Mechanism §2 4): a single value. `user` when the user drafted
  it, otherwise the model name alone (`fable-5`, `gpt-5.6-sol`), or `agent` when
  the model is unknown. It matches `^[a-z][a-z0-9.\-]{0,39}$`. Harness names
  (`claude`, `codex`, `claude-code`, `gemini-cli`), prefixes such as `agent:`,
  and any colon are refused.
- **`summary`** (Bylaws §1 2): at most 80 characters counted as Unicode code
  points, spaces included; one physical line; no Link or Predicate Edge.

When the engine writes a node, it writes each required value as a double-quoted
JSON string. The Predicate Edges follow: one target as a scalar, several as a
flow list, a `derived-from` target stored as an id unquoted and every other
target double-quoted. Then come the closing `---`, one blank line, and the body
with line endings converted to LF, leading newlines and trailing whitespace
removed, the tag guard of section 2.2 applied, and one final newline.
Hand-written nodes may use any YAML the reader accepts, such as unquoted values
and block lists.

### 2.2 Body

The body is Markdown, as Obsidian reads it.

- **Links** (Mechanism §8 1): `[[Target]]`; `[[Target#Heading]]` points into a
  heading. `[[Target|shown text]]` is a link to `Target`.
- **Embeds** (Mechanism §8 5): `![[file]]` displays a file, such as an image in
  `_sources/`, and counts as a Link.
- **Code regions** are fenced code blocks (three or more backticks or tildes),
  indented code blocks, and inline code spans. Inside a code region, `[[…]]`
  is not a Link and `#…` is not a tag. A fence that is never closed runs to the
  end of the body, or to the end of the list item or block quote that
  contains it.
- **Tag guard** (Mechanism §8 7): when the engine writes a node body or a scope
  memory, it inserts one space after `#<digits>` when a character that would
  extend an Obsidian tag follows directly: a letter, `_`, `-`, `/` or `·`. So
  `#1227은` is written `#1227 은`. Code regions and raw records are left
  unchanged.
- **Delegation clause** (Mechanism §7): a standing delegation node, kept in
  `00_Person/Delegation/`, has a `## 위임` section with four list items:
  `- 대상:` (the delegated act), `- 범위:` (its boundary), `- 조건:` (conditions)
  and `- 종료:` (end condition). `조건` and `종료` read `없음` when there are
  none, and a full-width colon also reads. A clause with a missing section or
  item grants nothing.

### 2.3 Predicate Edges

`derived-from` names what the node comes from (Mechanism §8 2):

| Target | Stored form |
|---|---|
| a node | its title as a quoted wikilink: `"[[Title]]"` |
| a raw round | the plain coordinate, quoted: `"00_Scope/<scope>/_raw/.records/<record>.txt#N"` |
| another non-node file | a quoted path wikilink: `"[[_sources/plan.pdf]]"` or `"[[path#Heading]]"` |

- A node target never uses a path: paths break when nodes move, and titles are
  unique.
- A raw target names its round (`#N`) (Bylaws §1 3); the engine reports one
  that does not.
- A node cannot cite itself.
- The engine also reads a node target written as the bare id
  (`derived-from: 260802-1720-k7f2m9x3`). A target it receives as an id is
  stored as the title wikilink; an id that resolves to no node, or to several,
  is stored as given. It also reads raw coordinates written as wikilinks or with a
  `.md` record path (section 6.1), and stores every raw target it receives as
  the plain `.txt` coordinate.

`conflicts` is a case marker (Mechanism §8 2; Bylaws §9 4). Its target is
either `"[[CASE-<year>-<n>]]"`, an open (`docketed`) case that lists the node as
a party, or, after a `존치` (both stand) verdict, the other party's title. In
the second form both nodes name each other, and an `adjudicated` case with
verdict `존치` lists both as parties. The MCP surface writes only the open-case
form.

**Resolving a name.** For Links and Predicate Edges alike:

1. An `http://` or `https://` URL is external.
2. A name containing `/_raw/` is a raw record.
3. A name matching the id pattern is a node id.
4. A name containing `/` is a vault-relative path, tried as given and with
   `.md` appended, confined to the vault.
5. Any other name is a node title. When no readable node has that title, it
   is matched against the names of files under `_sources/`, the `_raw/`
   directories and `_ledger/`, with or without the extension; this is how
   `[[CASE-<year>-<n>]]` finds its case file.

An embed therefore resolves when written with its file name, as Obsidian writes
it (`![[diagram.png]]`), or with its vault path (`![[_sources/diagram.png]]`).

Two readable nodes with the same title or id make the name ambiguous, and it
resolves to neither. A name that resolves to nothing is *dangling*: a warning,
not an error. Which references are permitted between Spaces is a governance
rule (Constitution Article 8), checked on writes and by the validator; this
document covers only the syntax.

## 3. Identity

- **The id is the identity.** The file name is the title; renaming or moving a
  file changes the title or the location, not the node (Mechanism §2 3). A move
  keeps the file's bytes.
- **Ids are unique.** Uniqueness rests on the width of the random part and on
  the validator's full comparison (Mechanism §2 1). When two readable node files
  carry the same id, the engine chooses neither: reads and writes by id or by
  title are refused until a person resolves it. This applies to copies
  (duplicated files, restored backups, merge results) and equally to two paths
  that are the same file, such as a hard link or a symbolic link to another
  node file: one node in two places is a duplicate.
- **Titles are unique** across the vault (Mechanism §8 2). Two readable nodes
  with the same file name make that name ambiguous, and reads and writes by it
  are refused. The engine also refuses to create a title that matches an
  existing one after NFC normalization when letter case is ignored (Python
  `str.casefold`), because such names are the same path on NTFS and APFS.
- **Title rules.** A title is a file name on every device that syncs the vault.
  The engine refuses a title that:
  - is empty, or has leading or trailing whitespace;
  - contains any of `< > : " | ? * \ /`, `#` or `]` (they break `[[…]]`), a
    C0 control character (below U+0020), or U+0085, U+2028, U+2029;
  - begins with `.`, or ends with `.` or a space;
  - is a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `CONIN$`, `CONOUT$`,
    `COM1`–`COM9`, `LPT1`–`LPT9`, and `COM`/`LPT` followed by `¹`, `²` or `³`),
    compared without letter case on the part before the first `.`, trailing
    spaces ignored;
  - makes `<title>.md` longer than 255 bytes in UTF-8.

  The MCP surface also limits a title to 120 characters.
- **Cluster names** follow the title rules and do not begin with `_` or `.`
  (Mechanism §6-2 3). A hub carries its cluster's name, so renaming a cluster
  renames its hub (Bylaws §3 6).

## 4. Ledgers

### 4.1 Common rules

Every `*.jsonl` file under `00_Scope/Workbench/_ledger/` is a ledger
(Mechanism §3 1–2).

- **Lines.** UTF-8 JSON Lines: one JSON object per line, each line ending in
  `\n`. Only `\n` separates records; blank lines are skipped. The engine writes
  JSON with `", "` and `": "` separators, non-ASCII characters unescaped, and
  U+0085, U+2028 and U+2029 escaped as `\u0085`, `\u2028` and `\u2029`, so that
  no line contains a character that other line splitters treat as a break.
- **Append only.** Records are never edited or deleted. On one device, appends
  are serialized by an exclusive lock on the ledger file. Each append writes
  one complete line and fsyncs it; when the file does not end in a newline, the
  new record starts on a new line. The append that creates a ledger also makes
  its directory entry durable (on POSIX).
- **Common fields.** The engine adds three fields to each record, after the
  record's own fields:

| Field | Value |
|---|---|
| `at` | append time in ISO 8601 with seconds and offset, in KST: `YYYY-MM-DDThh:mm:ss+09:00` (Mechanism §2 2) |
| `rid` | record id: a UUIDv7 in lowercase hex, `xxxxxxxx-xxxx-7xxx-xxxx-xxxxxxxxxxxx` |
| `parents` | the rids of every head at append time; `[]` in a ledger's first record |

- **`rid`.** The first 48 bits are Unix time in milliseconds, the 12 bits after
  the version digit are a sequence number, the variant bits are `10`, and the
  last 62 bits are random. Under the lock the engine reads the largest rid in
  the file (by time, then sequence) and issues a larger one: the current
  millisecond with sequence 0 when the clock has passed it, otherwise the same
  millisecond with the next sequence number (Mechanism §3 1). Rids therefore
  increase even when merged lines are out of order in the file.
- **Causal maxima.** A *head* is a record that no record names as a parent.
  Normally a record has one parent; the first record after a multi-device
  merge names every head and so joins the branches. State comes from causal
  maxima, not from rid order or line order. Among the records about one subject
  (the key field in the table below), a record is a maximum when no other such
  record descends from it. A single maximum is the current state. Several
  maxima leave the subject unresolved, and the engine treats it conservatively
  until a later record that descends from all of them is the single maximum
  (Mechanism §3 1).
- **Records without `parents`.** Records before the first record that carries
  `parents` take line order as causal order. After that record, a record
  without `parents` is an isolated root.
- **Invalid parents.** A parent that is the record itself, an unknown rid, or a
  record on a later line is dropped, so the graph is always acyclic. A record
  left without a valid parent is an isolated root (Mechanism §3 2).
- **Damage.** A line that is not JSON (such as a partial line), a line that is
  not a JSON object, and a missing, malformed or duplicate rid are structural
  damage (Mechanism §3 2). The engine treats the affected subjects as
  unresolved and refuses every further append to that ledger.
- **Manual repair** (Mechanism §3 8). Ledgers are readable without the engine.
  After a merge conflict, the repaired file is the union of both sides' lines,
  with one copy of each rid. A later user record whose `parents` lists every
  head then joins the branches.

| File under `_ledger/` | Record kinds | Subject key | Appended by | Mechanism |
|---|---|---|---|---|
| `approvals.jsonl` | `protect`, `approve`, `revert`, `unprotect` | `region` | CLI (user); updater | §3 |
| `routing.jsonl` | `bind`, `alias` | `session` | first writes; session hooks | §6-2 6 |
| `moves.jsonl` | `move` | none (section 4.4) | node and cluster moves | §1 3 |
| `case/candidates.jsonl` | `candidate`, `dismiss` | `basis` | MCP `record_candidate` (`candidate` only) | §4 1–2 |
| `evictions.jsonl` | `evict`, `settle` | `of` names the evict | scope-memory writes (`evict`); node writes, `osk tidy settle`, growth runner (`settle`) | §9-2 12 |
| `update.jsonl` | `begin`, `apply`, `remove`, `skip`, `done`, `rollback` | `path` | updater | §1-2 7 |
| `validators.jsonl` | `activate`, `deactivate` | `rule` | `osk validators` (user) | §6-1 |
| `pins.jsonl` | `pin`, `unpin` | `target` | not appended by the engine | §6 |
| `growth.jsonl` | `plan`, `review`, `run`, `eviction_review` | `key` (reviews) | growth runner | none |
| `rechecks.jsonl` | `complete` | (`node`, `target`) | node writes; session start | §4-1 |
| `migration/events.jsonl` | `archive`, `move`, `transform`, `hold`, `drop` | none | not appended by the engine | §5 |
| `signatures.jsonl` | preserved records | none | never appended | §3 9 |

The validator checks every ledger in the table for valid JSON and for the
presence, format and uniqueness of rids. Optional fields below are marked
"opt.".

### 4.2 Approvals

`approvals.jsonl` records protected regions (Mechanism §3 3):

| `kind` | `base` | `accepted` | `discarded` | `moves_seen` |
|---|---|---|---|---|
| `protect` | `null` | tree at protection: the first approved snapshot | absent | yes |
| `approve` | approved tree the review assumed (`null` when joining branches) | reviewed working tree: the new approved snapshot | absent | yes |
| `revert` | approved tree that remains | absent | working tree that was discarded | yes |
| `unprotect` | approved tree at release | `null` | absent | absent |

Every record also has `region`, the vault-relative POSIX path of the region
directory (for example `_governance` or `00_Person/Delegation`), and `reason`,
free text that is empty when none was given. Tree values are
`sha256:<64 hex>` tree hashes (section 5). `moves_seen` lists the heads of
`moves.jsonl` when the record was written; it bounds which moves a later revert
undoes. A record without `moves_seen` is bounded by rid time instead.

- **State** (Mechanism §3 5): the single causal maximum for a `region` decides
  it. After `protect` or `approve` the approved snapshot is `accepted`; after
  `revert` it is `base`; after `unprotect` the region is unprotected. Several
  maxima make the region *stale*, and approval and revert wait for a user
  record that joins the branches: an `approve` with `base: null` whose
  `parents` include every branch. Structural damage anywhere in the ledger makes
  every region named in it stale.
- **Order** (Mechanism §3 6): `approve` is appended only when `base` still
  equals the approved snapshot, checked again under the ledger lock, and
  `accepted`, the working tree as stored, equals the tree the user reviewed.
  `revert` is appended only after the working copy has been restored and its
  entries made durable.
- **Writers** (Mechanism §3 7): the interactive CLI, and the updater for
  `_governance` inside a confirmed update (section 7.2). No MCP tool appends
  to this ledger.

### 4.3 Routing

`routing.jsonl` binds session keys to scopes (Mechanism §6-2 6). A session key
names a working context that stays the same across sessions, such as a
repository name.

| `kind` | Fields |
|---|---|
| `bind` | `session` (the key), `scope` (a scope name), `reason`; opt. `repo`: the sorted root-commit SHAs of the repository |
| `alias` | `session` (another name), `canonical` (the key it maps to), `reason` |

- The single causal maximum among records with a given `session` decides that
  key. A `bind` binds it to `scope`. An `alias` maps it to `canonical`; the
  engine follows a chain of aliases for at most 8 steps and ignores a chain that
  loops. Several maxima leave the key unbound, and writes must then name their
  landing scope.
- A write in a bound session lands in its scope, and a request that names a
  different scope is refused. The first successful write of an unbound session
  into a scope appends its `bind`. Rebinding is a new record. No MCP tool or CLI
  command appends an `alias`.
- A key shaped like a one-off conversation id (a UUID with or without hyphens,
  or 32 hex digits) is refused before anything is written.
- **Keys from hooks.** Inside a Git repository, the key is the main
  repository's directory name: a worktree takes its main repository's name, and
  a submodule or bare repository uses its own name. Outside Git, the key is the
  directory name.
- **Repository identity (`repo`).** The sorted union of the root commits
  (`git rev-list --max-parents=0`) reachable from local branches and
  remote-tracking branches. Directories outside Git, repositories without
  commits and shallow clones have no identity.
- **Ownership.** Among the `bind` records of a key that carry `repo`, the one
  with the smallest rid is the key's owner. The first repository with an
  identity to meet an unowned binding appends an owner record: a `bind` that
  repeats the current scope and adds `repo`. A repository whose roots share
  none with the owner's uses the derived key `<name>-<first 8 hex of a root>`:
  a derived key whose owner shares one of its roots; failing that, a derived
  key made from one of its roots that is bound but has no owner; failing that,
  one made from its smallest root. A derived key stays the same when roots are
  added. When the owner's key has branched into several maxima that all bind
  the same scope, the owner appends one record that joins them. Hook records
  never choose a scope, and ownership never moves. Identity helps resolve keys;
  it is not a security boundary.

### 4.4 Moves

`moves.jsonl` records node moves that a revert must be able to undo
(Bylaws §6 4):

| Field | Value |
|---|---|
| `kind` | `move` |
| `node` | the node's id |
| `from`, `to` | vault-relative POSIX paths of the file before and after |

A record is appended before the rename, for every move whose source or
destination lies in a protected region, and for every later move of a node
that already appears in the ledger. It is a log of physical events, not a
ledger of decisions: a record whose destination does not contain a file with
that id is ignored.

### 4.5 Case docket

`case/candidates.jsonl` lists conflict candidates (Mechanism §4 1–3):

| Field | Value |
|---|---|
| `kind` | `candidate`, or `dismiss` (appended by the user) |
| `basis` | `sha256:` of the UTF-8 string `v1\|<type>\|<id>@<file hash>,…`, one entry per party, sorted |
| `basis_version` | `1` |
| `type` | `contradiction`, `duplication`, `competition` or `delegation-overlap` |
| `nodes` | the party ids, sorted |
| `reason` | opt. text |

A record with the same `basis` and `basis_version` already in the ledger
suppresses a repeat. When a party's file changes, its hash, and so the basis,
changes too (Mechanism §4 2).

Case files are `case/CASE-<year>-<n>.md` (Mechanism §4 4). A case file starts
with a header of `key: value` YAML lines that ends at the first blank line;
prose follows. The header is not frontmatter and has no `---` fences. Its
fields are `case_no` (equal to the file name), `status` (`docketed` or
`adjudicated`), `parties` (a list of ids), `docketed_at`, `verdict` (`기각`,
`수정`, `존치`, or null), `verdict_at`, `applied` and `schema_version`. A case
file that contains `pre_sign` keeps it. Links inside a case file are for
display only (Mechanism §4 5).

### 4.6 Evictions

`evictions.jsonl` keeps what callers removed from a scope memory under
pressure of its size limit (Mechanism §9-2 12):

| `kind` | Fields |
|---|---|
| `evict` | `scope`; `session` (the canonical key); `text`, the removed lines |
| `settle` | `of` (the rid of an `evict`); `outcome`: `node`, `merged` or `discarded`; `target`, a node title, absent for `discarded`; opt. `reason` |

- After an over-limit refusal, the first successful scope-memory write that
  changes the memory, for the same session key on the same device, records the
  lines it removed, if any, as an `evict` before the memory file changes. Its
  `text` lists those lines of the stored memory in order, once each, after
  secret filtering.
- An `evict` with at least one `settle` is settled. Two settles from two
  devices are both valid (Mechanism §9-3 4).

### 4.7 Validators and pins

- **`validators.jsonl`** (Mechanism §6-1): `kind` (`activate` or `deactivate`),
  `rule`, `reason`. The rule's single causal maximum decides; a rule without
  records, or with several maxima, is inactive. The only rule is
  `cluster-overview`.
- **`pins.jsonl`** (Mechanism §6): `kind` (`pin` or `unpin`), `target` (a
  vault-relative cluster path or a node id), opt. `reason`. Before moving nodes
  or clusters, the engine treats a target that has records as pinned unless its
  single causal maximum is `unpin`; an unreadable or damaged ledger counts as
  pinned. The engine does not append to this ledger.

### 4.8 Update journal

`update.jsonl` records what the updater applied (Mechanism §1-2 7):

| `kind` | Fields |
|---|---|
| `begin` | `txn`, `version`, `adopt` (boolean) |
| `apply` | `txn`, `version`, `path`, `hash` (the file's hash after applying) |
| `remove` | `txn`, `version`, `path` |
| `skip` | `txn`, `version`, `skipped_path`, `why` |
| `done` | `txn`, `version`, `attest` (hash of the release's `release.json`), `applied`, `removed`, `conflicts` (counts) |
| `rollback` | `why`; opt. `txn` and `version` |

- `txn` is 16 lowercase hex digits and ties one transaction's records together.
  An `apply` or `remove` counts only when a `done` with the same `txn` exists;
  one without `txn` counts as committed.
- A path's state is its committed causal maximum: `apply` means the path is
  managed and `hash` is its baseline; `remove` means it is not managed. Several
  maxima that agree on kind and hash count as one.
- `skip` names its path in `skipped_path`, so that it never changes a path's
  state.
- The installed version is the `version` of the single causal maximum among
  `done` records; several maxima count as one version when their `version` and
  `attest` agree.

### 4.9 Growth ledger

`growth.jsonl` is the work record of the Scope-to-Domain growth runner. It is
not listed in Mechanism §1 3; `osk/growth.py` defines the payloads.

| `kind` | Fields |
|---|---|
| `plan` | the run manifest: selected `candidates`, `scope_jobs`, `organization_jobs`, `eviction_jobs` and bookkeeping; its `rid` names the run |
| `review` | `key`, `manifest` (a plan rid), `candidate`, `outcome` (`preserved`, `no_value` or `deferred`), `target`, `reason`, `distillation` (a receipt or null); opt. `omitted_sources` |
| `run` | `manifest`, `ok`, `state`, counts, per-queue outcomes, `output` (the run directory `.osk/growth/runs/<rid>/`) |
| `eviction_review` | `manifest`, `of`, `outcome` (`node`, `merged`, `discarded` or `deferred`); opt. `target`, `reason`, `settlement` (a settle rid) |

A candidate `key` is `sha256:` of the compact JSON list of `[id, file hash]`
pairs of its source nodes, sorted. A candidate's decision is the single causal
maximum among `review` records with that key.

### 4.10 Rechecks

`rechecks.jsonl` records completed checks of `derived-from` pairs
(Mechanism §4-1; Bylaws §7 2–3):

| Field | Value |
|---|---|
| `kind` | `complete` |
| `node` | the citing node's id |
| `node_state` | `sha256:` of the citing node file as written |
| `target` | a node id or a vault-relative file path, with `#<heading>` appended for a heading range |
| `target_state` | `sha256:` of the target file's bytes, or of the heading range |
| `result` | `bound`, `updated` or `unchanged` |
| `reason` | opt. text |

- **Tracked targets.** Nodes, non-node files, and heading ranges in either. A
  heading range runs from the first byte of its heading line to the byte before
  the next heading of the same or a higher level, or to the end of the file.
  Lines in code regions are not headings, and a heading text that occurs twice
  does not resolve (Mechanism §8 4). Raw rounds only grow and external URLs have
  no state; neither is tracked. A target that does not resolve gets no record
  and is reported as dangling.
- **State.** A pair (`node`, `target`) is complete when its single causal
  maximum matches both current states; several maxima that agree on both states
  count as one. Any other pair makes the citing node a recheck candidate, listed
  by `overview`, the validator's warnings and `osk rechecks`.
- **Writers.** A node write appends `bound` for each pair it wires. An
  `update_node` whose `add_edges` names an existing target again appends
  `updated` when the same call changes the node and `unchanged` when it does
  not; this closes a candidate. Every other pair that was complete before an
  engine write is appended again with the new `node_state`, as `unchanged` with
  reason `이어받음`. A ledger without records receives one `bound` record with
  reason `기준선` for every tracked pair, at the first session start or node
  write.

### 4.11 Migration and signatures

The engine reads these for the integrity checks of section 4.1 and appends to
neither of them.

- **`migration/events.jsonl`** (Mechanism §5 2): `kind`, `source`, `dest`
  (path or null), `before`, `after` (hash or null), `rule`, opt. `note`. A
  migration also keeps a manifest in `migration/` and closes the ledger when it
  ends (Mechanism §5 3).
- **`signatures.jsonl`** (Mechanism §3 9): a preserved record set. Nothing is
  appended to it and no decision reads it.

## 5. Approval store

A protected region's approved snapshot is kept as content-addressed objects
(Mechanism §3 4).

- **Tree.** The list of `[path, hash]` pairs for every regular file in the
  region: `path` is the vault-relative POSIX path, `hash` is `sha256:` of the
  file's raw bytes, sorted by path in code-point order.
- **Manifest.** The tree serialized as compact UTF-8 JSON: no spaces,
  non-ASCII characters unescaped, as in `[["a/b.md","sha256:…"],…]`. The
  tree hash is `sha256:` of the manifest bytes. A region that is empty or
  deleted has the tree `[]`.
- **Exclusions.** Below the region root, the tree leaves out directories named
  `.git`, `.venv`, `__pycache__`, `_ledger`, `_raw` or `_scope_memory`; every
  file or directory whose name begins with `.`; symbolic links and special
  files; and directories whose real path differs from their path (links and
  junctions). The name of the region root itself is not tested.
- **Region roots.** A region cannot be the vault root or a path with `.git`,
  `.venv`, `__pycache__`, `_ledger`, `_raw` or `_scope_memory` as a component.
  A region root whose name begins with `.` is allowed.
- **Objects.** Every file's bytes and every manifest are stored at
  `00_Scope/Workbench/_ledger/approved/objects/<first 2 hex>/<remaining 62 hex>`
  of their SHA-256. The same content is stored once, and merging two devices'
  stores is a plain union. A reader checks an object's hash before using it and
  treats a mismatch as missing; storing an object whose path contains other
  bytes replaces them.
- **Durability order.** An object is written to a temporary file, fsynced and
  renamed into place. On POSIX, each directory created for it and the directory
  that owns its entry are fsynced too. Only then is the ledger record that names
  the object appended. On Windows the engine does not fsync directories: NTFS
  journals directory entries, so after a power loss an entry is either the
  earlier or the later one, and the last change can be lost (Mechanism §1-2 7).
- **Reading with the exclusions.** An approved manifest that lists paths under
  the excluded names is read without them. A revert of such a region restores
  only the remaining files and records the re-derived tree as its `base`.
- **States.** `clean` when the working tree hash equals the approved one,
  `pending` when it differs, `stale` when the approvals ledger has several
  maxima for the region, `unprotected` otherwise. The changeset lists the
  added, removed and modified paths, and moved nodes.

## 6. Raw records and scope memory

### 6.1 Raw records

Raw records are the append-only transcripts of sessions (Mechanism §8 3, §9).

- **Path.** `00_Scope/<scope>/_raw/.records/<record>.txt`. When
  `<record>.txt` would be longer than 255 bytes, the record is
  `00_Scope/<scope>/_raw/.records/<record>/record.txt`. The engine also reads
  a record stored as `00_Scope/<scope>/_raw/<record>.md` as the same record. It
  renames that file, bytes unchanged, to the `.txt` path when it next appends to
  it, and `osk raw migrate --apply` renames all of them. When both forms exist,
  the engine refuses to choose. The validator reports `.md` raw records.
- **Names.** Record names follow the title rules of section 3; two names that
  match after NFC normalization with letter case ignored are the same record
  (Mechanism §9 5). The MCP surface limits them to 120 characters.
- **Encoding.** UTF-8, with bytes kept exactly: no line-ending conversion on
  reading, writing or checkout.
- **Layout.** A round is one user message and the agent reply that belongs to
  it (Bylaws §2 7). Rounds are separated by one blank line:

```text
## 1

### user

<user message>

### agent

<agent reply>

## 2

…
```

- **Round headings.** A round begins with the line `## <n>`, where `<n>` is a
  positive decimal index; the reader allows trailing spaces, tabs or a CR. The
  engine assigns indexes: they start at 1 and strictly increase. A record whose
  indexes do not start at 1 or do not strictly increase takes no further appends
  (Mechanism §9 6).
- **Escaping.** A message line that looks like a round heading (`## <digits>`,
  possibly after backslashes) is stored with one more `\` in front, and reading
  a round removes it again (Mechanism §8 3).
- **Capture stamps.** Capture adapters put the line
  `<!-- osk-capture: dialogue-v1 "<id>" -->` and a blank line after a round
  heading. A record may begin with a one-line header
  `<!-- osk-capture: claude-inherited-v1 {…} -->` and a blank line. The
  validator reports rounds stamped `codex-user-items-v2` or `codex-terminal-v3`.
- **Coordinates.** `<record path>#<n>`, with one `#` (Mechanism §8 3).
  `[[<record path>#<n>]]` and the `.md` record path also resolve. A round runs
  from the first byte of its heading line to the byte before the next round
  heading, or to the end of the file.
- **Round hash.** The hash that `read_raw` reports and that distillation
  receipts store is `sha256:` of the round's UTF-8 bytes without trailing
  newlines, so that appending a round leaves the hash of the round before it
  unchanged.
- **Append only** (Mechanism §9 4). A write succeeds only when the file's
  current bytes are a prefix of the new bytes, compared after secret filtering.
  A batch of rounds is written whole or not at all, and a batch whose round
  bodies equal the record's last rounds is refused as a retry (Mechanism §9 7).
- **Secret filter** (Mechanism §9 1–3). Before writing, matches of seven
  patterns are replaced with `[FILTERED:<name>]`: `pem-private-key`,
  `aws-access-key`, `github-token`, `openai-style-key`, `slack-token`,
  `google-api-key` and `bearer-header`. Mechanism §9 1 lists the Python `re`
  patterns; token boundaries are ASCII word characters only. The validator
  reports any match found in `_raw/` or `_scope_memory/`.

### 6.2 Scope memory

- **Path.** `00_Scope/Workbench/_scope_memory/<scope>.md`, one per scope,
  shared by every session and device (Mechanism §9-2 1).
- **Not a node.** No frontmatter; the text cannot begin with `---`. It is not
  searched and not counted in centrality (Mechanism §9-2 1, §9-2 10).
- **Canonical form.** NFC-normalized, with leading and trailing whitespace
  removed. The limit is 1500 characters (code points) of the canonical form,
  measured after secret filtering and the tag guard (Mechanism §9-2 2, §9-2 9,
  §9-2 11).
- **Stored bytes.** The canonical form and one `\n`; an empty memory is an
  empty file. The bytes are exempt from Git line-ending conversion.
- **Hash.** `sha256:` of the canonical form in UTF-8, without the final
  newline. Replacing a non-empty memory as a whole requires the hash the caller
  read (Mechanism §9-2 4).

## 7. Release attestation and updates

### 7.1 `release.json`

The canonical repository declares a release with an attestation at its root
(Mechanism §1-2 2):

```json
{
 "version": "vX.Y.Z",
 "at": "<ISO 8601 time, KST>",
 "files": {
  "<path>": "sha256:<64 hex>"
 }
}
```

- `version` is exactly `v<major>.<minor>.<patch>` in decimal digits. A version
  is declared once and never reused.
- `files` maps every file of the released commit except `release.json` itself
  to the SHA-256 of its committed content, in Git's path order. Only regular
  files without the executable bit (mode `100644`) can be released; symbolic
  links, submodules and executable files are refused.
- The file is JSON with one-space indentation, non-ASCII characters
  unescaped, and a final newline.
- The release is a commit that adds `release.json` on top of the verified
  commit, tagged `vX.Y.Z`.

### 7.2 Updating an instance

- **Source** (Mechanism §1-2 3). By default, the newest `vX.Y.Z` tag of the
  canonical repository. A pinned version must be an existing tag, and the
  fetched `version` must equal it. A local directory or tar bundle is verified
  the same way.
- **Verification.** Every file listed in `files` exists in the fetched tree
  with the listed hash, or nothing is applied. The SHA-256 of `release.json`
  itself is the release's identity (`attest` in the journal); a release whose
  version was applied before with another identity is refused.
- **Scope of an update** (Mechanism §1-2 4). The publish manifest inside the
  release (`_governance/_engine/scripts/publish-manifest.txt`, itself attested)
  maps release paths to instance paths (`MAP`), skips canonical-only files
  (`KEEP`), excludes path fragments (`DENY`), and creates empty Space roots
  (`SKEL`). A file managed by the previous release and missing from this one is
  deleted, unless it was changed locally.
- **Instance-owned floor** (Mechanism §1-2 5). The updater never writes or
  deletes below any Space root in any spelling (apart from creating an empty
  top-level root), nor in `_ledger/`, `_raw/`, `_sources/`, `.osk/` or
  `.git/`.
- **Local changes** (Mechanism §1-2 6). A managed file equal to its baseline is
  replaced. A locally changed document stays, and the release's copy is placed
  beside it as `<path>.upstream-<version>`. A locally changed engine file stops
  the whole update. `--adopt`, for a first adoption only, takes the release as
  the baseline and keeps each overwritten local file as `<path>.local-<version>`.
- **Transaction** (Mechanism §1-2 7). Before touching any target, the updater
  writes pre-images to `.osk/txn/backup/<six-digit index>` and then
  `.osk/txn/manifest.json`, with `txn`, `version`, `entries` (each with `rel`,
  `backup`, `existed`, `hash` and `mode`) and `dirs` (directories the
  transaction may create). The journal's `done` for that `txn` is the commit
  point. The next update run, or
  `_governance/_engine/scripts/recover.py --apply`, which does not import the
  engine, finishes an interrupted transaction: with `done` it clears the marker
  (roll forward), without `done` it restores the pre-images and removes the
  empty directories it created (roll back).
- **Governance region** (Mechanism §1-2 6, §3 7). When `_governance` is
  protected and the update changes it, the confirmed update appends an
  `approve` inside the transaction, with the reason
  `osk.update <version>; user confirmation: <review id>`. When
  `_governance` has no approval records and the updated region equals the
  attested files exactly, the update appends `protect` instead.
- **`.osk/config.json`.**
  `{"upstream": {"source": "git" | "bundle", "url": "<repository URL>", "pin": "<version>" | null}}`.
  Without it, the source is the canonical Git repository, with no pin.

## 8. Compatibility promise

For the v4 line:

1. Within v4.x, the engine does not change the format in this document
   incompatibly. A change is incompatible when a vault valid under v4.0.0 fails
   validation, a stored value takes on another meaning, a stored path moves, or
   a field, record kind or file that readers depend on disappears.
2. An incompatible change requires a new major version and a documented
   migration.
3. Every form this document lists as read continues to be read: the
   `= Scope` and `Scope` Space-root spellings (likewise for Domain and
   Person), four-character id random parts, bare-id
   `derived-from` targets, `.md` raw records and wikilink raw coordinates,
   ledger records without `parents`, update records without `txn`, approval
   records without `moves_seen`, case files with `pre_sign`, and
   `signatures.jsonl`.

New ledgers and new optional fields in ledger records are compatible, since
v4 readers ignore fields they do not use. A new record kind in an existing
ledger is not, because the approvals, evictions and growth readers reject
unknown kinds. Neither is a new frontmatter field, because the node contract
admits no other keys.
