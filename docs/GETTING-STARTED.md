# Getting started with osk-system

**English** · [한국어](GETTING-STARTED.ko.md)

This tutorial is for people who use Claude Code or Codex but have never set up
osk-system. It starts from an empty folder and ends with a verified setup. At the
end, your agent writes its first memory, then finds it and reads it back. Written
for release v3.22.2.

For more detail on any step, follow the links to [SETUP.md](SETUP.md), the
operator reference (in Korean).

## Before you start

- **`<vault>`** is the absolute path of your vault folder, such as
  `/Users/you/my-osk-vault` or `C:/osk/my-osk-vault`. On Windows, write it
  with forward slashes, which work in PowerShell, JSON and TOML without escaping.
  Pick a path **without spaces**: hook commands are plain command lines, and a
  path without spaces needs no quoting in any shell. On Windows, a folder such
  as `C:/osk` also avoids a user name that contains a space.
- **`my-app`** stands for one of your project repositories.
- **Shells.** Windows commands are for PowerShell. macOS/Linux commands are for
  bash or zsh. Commands that start with `.venv` run from the vault root.
- **There is no `osk` command.** You run the engine with the vault's own Python:
  `python -m osk.cli …` and `python -m osk.update …`. On Windows, typing `osk`
  opens the On-Screen Keyboard. Where SETUP.md writes `osk validate`, read it as
  `python -m osk.cli validate`.
- **Korean messages.** Most engine and hook messages are in Korean; fork-check
  reasons and some diagnostics are in English. This guide quotes the parts you
  will see and explains what they mean.

## What you get

- **A vault you own.** It is a Git repository of plain Markdown files. You can
  read it in any editor or explore it as a graph in Obsidian.
- **An MCP server with 12 tools.** Your agents use them to search, read and write
  knowledge *nodes*. The engine validates every write.
- **Hooks for Claude Code and Codex.** They do three things:
  - give each new session its project's memory;
  - capture each finished conversation round;
  - schedule reviews that turn conversations into knowledge.
- **Human authority.** For any folder you protect, the engine keeps the last
  snapshot you approved. Agent edits wait as a changeset that only you can
  approve or revert.
- **Optional extras.** Git sync to your private remote, background reviews on
  your Claude or ChatGPT subscription, and a knowledge-only Obsidian graph.

## Concepts in five minutes

**Vault and releases.** Your vault is an *instance* of the canonical repository,
<https://github.com/lpaiu-cs/osk-system>. The framework files (`_governance/`,
`docs/`) change only through releases, which you apply with `osk.update`. Do not
edit engine files, and do not `git pull` from the public repository.

**Spaces.** Knowledge lives in three top-level folders:

| Folder | Holds |
|---|---|
| `00_Scope/` | Knowledge tied to one project or activity, one folder per *scope* |
| `00_Domain/` | Knowledge that applies across projects, organized by topic |
| `00_Person/` | Your own records, and knowledge about you: preferences, goals, notes |

Vaults created before v3.21 may still use the old names (`= Scope` and so on).
That is supported; see [space-layout-migration.md](space-layout-migration.md).

**Scopes and session keys.** Each project gets a scope, such as
`00_Scope/my-app/`.

- The hooks name every session after the repository it runs in. In
  `~/code/my-app`, the *session key* is `my-app`.
- A Git worktree gets the name of its main repository. A folder outside Git gets
  its own folder name.
- If another, unrelated repository already owns that name, the hook gives you
  `my-app-<first 8 hex of your root commit>` instead. The owner is the
  repository that first used the binding; its root commits are recorded with it.
- Your first successful write binds the key to a scope, permanently. From then
  on, every session in that repository lands in that scope, on any device.

**Nodes, clusters and hubs.** A *node* is one Markdown file with a small header
that the engine validates. The header holds:

- an id;
- timestamps;
- the author;
- the drafter, which is the model that wrote the node;
- a one-line summary of at most 80 characters.

A node's title is its file name, and no two nodes in the vault share a title.
Nodes link to each other with `[[Title]]`, just as in Obsidian. A folder of nodes
is a *cluster*. Each cluster has a *hub*: a node with the same name as the
folder, written before any other node in it. Writes through MCP are validated.
Files you edit by hand are not checked until you run `validate`.

**Raw records.** The hooks capture each finished conversation round into the
scope's `_raw/` folder. A round is your message plus the agent's visible answer.
Raw records are append-only, and search never returns them. Nodes cite a round
as evidence by its coordinate, such as `<record path>#12`.

**Scope memory.** Each scope has one short note of at most 1,500 characters. It
holds the lessons that matter right now, and the hooks inject it at the start of
every session. When the note is full, lasting knowledge moves into nodes. Lines
removed to make room are logged and settled later, which the hooks call *tidy*.
Settling means turning the line into a node, merging it into one, or discarding
it.

**Protected regions.** You can protect a folder with the CLI's `protect` command.
From then on, any change to it shows as `pending` in `status`, whether an agent
or you made it. It stays pending until you `approve` or `revert` it in an
interactive terminal. Agents cannot approve anything through MCP. This protects
against honest mistakes. It is not security against someone who can write to
your disk.

**Reviews.** A review reads new raw rounds and saves what matters as nodes or
scope memory.

- By default, the hooks ask the agent to review in the session, at your 9th and
  15th message.
- With *fork reviews* turned on, a background fork runs the review after every
  9 successful final answers. It uses the same harness and model, on your
  subscription.

**Ledgers.** The operational scope, `00_Scope/Workbench/`, holds the engine's
append-only ledgers in `_ledger/*.jsonl`. They record scope bindings, approvals,
updates and evictions. It also holds the scope memories in `_scope_memory/`.
Never edit these files by hand.

## Prerequisites

- **Python 3.11 or newer.** README and SETUP use 3.12, and so do the commands
  below. Any 3.11+ interpreter works if you change the version in the commands.
- **Git**, with `user.name` and `user.email` set so that commits work, and
  credentials that can push to your private remote.
- **Claude Code and/or Codex.** You can connect one or both.
- **An empty private Git repository** for the vault, created without a README or
  license. It is optional but recommended, because the vault will hold your
  conversation records and personal notes.

## Step 1: Create your vault

Clone a release tag, not the `main` branch, which moves between releases. The
updater compares your files with a release. Starting exactly on one lets Step 2
record a clean baseline. `v3.22.2` works as written. You can use the newest tag
from the [releases page](https://github.com/lpaiu-cs/osk-system/releases) instead,
as long as you use the same tag again in Step 2.

Run these in the folder that will contain the vault, such as `C:/osk` on
Windows (create it first). The commands are the same on every OS:

```bash
git clone --branch v3.22.2 https://github.com/lpaiu-cs/osk-system.git my-osk-vault
cd my-osk-vault
git switch -c main
```

Point the vault at your private remote and push it:

```bash
git remote set-url origin <your-private-remote-url>
git push -u origin main
```

Never push your vault to the public repository. If you have no private remote
yet, run `git remote remove origin` instead. Otherwise the sync daemon would
later pull unreleased upstream changes into your vault.

**Check:** `git status` says `On branch main`. `git remote -v` shows your private
URL, or nothing if you removed the remote.

## Step 2: Install the engine and record the release baseline

macOS/Linux:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r _governance/_engine/requirements.txt
export PYTHONPATH=_governance/_engine
.venv/bin/python -m osk.cli validate
```

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r _governance\_engine\requirements.txt
$env:PYTHONPATH = "_governance\_engine"
.venv\Scripts\python.exe -m osk.cli validate
```

`PYTHONPATH` lasts until you close the terminal. Set it again in each new
terminal before you run `osk.cli` or `osk.update`, and run them from the vault
root. The MCP server and the hooks do not need it.

**Check:** the JSON printed by `validate` ends with `"verdict": "PASS"`.

Now record the release baseline. A fresh clone has no record of which release
its files came from. Recording it lets future updates tell release files apart
from local edits. The updater downloads the release from GitHub to compare.

macOS/Linux:

```bash
.venv/bin/python -m osk.update --to v3.22.2 --apply
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe -m osk.update --to v3.22.2 --apply
```

The first run changes none of your files. It prints the plan, then exits with
code 2 and `"approval_required": true`. It also shows `"ok": false` and a Korean
`instruction` that tells an agent to stop and ask you. Both are expected. On a
fresh clone, the plan lists every framework file under `rebaseline`. Their
content already matches, so only the baseline is recorded. Run **the same
command again**, within an hour, to apply it. Every `--apply` works this way
(see [Keeping up to date](#keeping-up-to-date)).

The baseline is written to `00_Scope/Workbench/_ledger/update.jsonl`. Commit it,
or let the sync daemon do so later. Skip `git push` if you removed the remote in
Step 1:

```bash
git add -A
git commit -m "Record osk release baseline"
git push
```

**Check:** `.venv/bin/python -m osk.update` prints `"current": "v3.22.2"`. On
Windows, use `.venv\Scripts\python.exe -m osk.update`. `git status` is clean.

## Step 3: Connect Claude Code

Skip this step if you only use Codex.

**3a. Register the MCP server** at user scope, so every project can use it.

macOS/Linux:

```bash
claude mcp add --scope user osk-system -- <vault>/.venv/bin/python <vault>/_governance/_engine/mcp_server.py
```

Windows (PowerShell):

```powershell
claude mcp add --scope user osk-system -- <vault>/.venv/Scripts/python.exe <vault>/_governance/_engine/mcp_server.py
```

**Check:** `claude mcp list` shows `osk-system: … - ✔ Connected`.

**3b. Register the hooks.** The MCP server gives the agent tools, but it does not
install the hooks. Add the three events below to the `"hooks"` object in
`~/.claude/settings.json` (Windows: `%USERPROFILE%\.claude\settings.json`).
Create the file if it does not exist. If it already has hooks, add these entries
beside them instead of replacing them. On Windows, change `.venv/bin/python` to
`.venv/Scripts/python.exe` in all three commands.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_session_start.py",
            "timeout": 30
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_prompt_submit.py",
            "timeout": 30
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/capture_stop.py",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

The same scripts serve Claude Code and Codex. The `claude_` prefix in two of the
file names is historical.

**Check:** first, run the session-start hook by hand from one of your project
repositories. It prints what a new session there would receive. On Windows, run
it in Git Bash with `.venv/Scripts/python.exe`. PowerShell can add a byte-order
mark to piped text, which the hook rejects.

```bash
cd ~/code/my-app
echo '{"session_id":"hook-test"}' | <vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_session_start.py
```

The output is one JSON line containing `[osk 세션 시작 — session=\"my-app\"]`:
session start, with the key `my-app`. For this made-up session ID it also ends
with a `native harness/transcript unavailable` diagnostic, which is expected.
Then start a **new** Claude Code session, because hooks load at startup. `/hooks`
lists the three osk entries.

## Step 4: Connect Codex

Skip this step if you only use Claude Code.

**4a. Register the MCP server.**

macOS/Linux:

```bash
codex mcp add osk-system -- <vault>/.venv/bin/python <vault>/_governance/_engine/mcp_server.py
```

Windows (PowerShell):

```powershell
codex mcp add osk-system -- <vault>/.venv/Scripts/python.exe <vault>/_governance/_engine/mcp_server.py
```

This adds an `[mcp_servers.osk-system]` table with `command` and `args` to
`~/.codex/config.toml`. Codex reads TOML, so do not paste `.mcp.json.example`
into it.

**Check:** `codex mcp list` shows `osk-system` with status `enabled`.

**4b. Register the hooks** in `~/.codex/hooks.json` (Windows:
`%USERPROFILE%\.codex\hooks.json`). If the file exists, add these entries to its
event lists instead of overwriting it. On Windows, use `.venv/Scripts/python.exe`
again.

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear|compact",
      "hooks": [{
        "type": "command",
        "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_session_start.py",
        "timeout": 30,
        "statusMessage": "osk: loading scope memory"
      }]
    }],
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_prompt_submit.py",
        "timeout": 30,
        "statusMessage": "osk: checking review cadence"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/capture_stop.py",
        "timeout": 30,
        "statusMessage": "osk: capturing the finished round"
      }]
    }]
  }
}
```

Codex skips new or changed hook definitions until you trust them. Start Codex,
open `/hooks`, review each osk entry and trust it, then start a new session.

**Check:** the hand-run test from Step 3b prints your repository's session key;
the test is the same for Codex. `codex features list` shows `hooks` as `true`,
which is the default on Codex 0.154. In a new session, ask the agent: *"What
session key did the osk hook give you?"* It should answer with the repository
name.

## Step 5: Your first session

Open Claude Code or Codex **inside a project repository**, such as
`~/code/my-app`, not inside the vault. The folder name becomes the session key,
so the examples use `my-app`; use your own. The harness may ask you to allow
each osk tool the first time; allow it.

**What the hook tells the agent.** In a repository with no binding yet, the
session-start text begins like this:

```text
[osk 세션 시작 — session="my-app"]
이 세션에서 `overview(session="my-app")`를 한 번 불러 … 아직 scope 결속이 없다. 착지를 추측하지 말고 …
```

In English: *"Session start, key `my-app`. Call `overview` once. There is no
scope binding yet, so don't guess where to write; confirm the project first."*

Without fork reviews, it also says `[osk 검토 경고 — subscription fork CLI is not
configured. …]`. That means reviews happen in this session at your 9th and 15th
message, which is normal.

Until the scope is bound, this conversation's rounds cannot be captured. From
your second message on, the hook text therefore carries a capture diagnostic,
`포착 진단: WriteError: 착지 미정 …` ("landing undecided"), and a
`[osk 케이던스 — user 턴 N]` line on every message, although no review is due.
Both stop after step 2 below. The review warning stays until you set up fork
reviews.

1. **Look around.** Prompt:

   ```text
   Call the osk overview tool with this session's key and show me the result.
   ```

   **Check:** on a new vault the result shows `"clusters": []` and
   `"session_scope": null`.

2. **Create the project's scope.** Prompt:

   ```text
   Create this project's scope in osk: a hub node titled "my-app" in space "00_Scope/my-app",
   with a one-line summary and a short description of the project. Use this session's key.
   ```

   The first attempt is refused on purpose, with this message:

   > `00_Scope/my-app`는 아직 없는 군집이라 … 새 군집을 만든다

   It means: *"This creates a new cluster. Confirm with the user, then send the
   same request again within one hour."* The agent should ask you. Reply:

   ```text
   Yes, create it. Send the same request again.
   ```

   **Check:** the result shows `"ok": true`, `"path": "00_Scope/my-app/my-app.md"`
   and `"bound_scope": "my-app"`.

3. **Record something.** Prompt:

   ```text
   Record in osk, as its own node in this project's scope, that the test suite runs with `make test`.
   Link it from the my-app hub.
   ```

   The agent can now leave out `space`, because the binding decides where the
   node lands. Then it edits the hub to add the link.

   **Check:** the result shows `"ok": true`. This time `bound_scope` is `null`,
   because it only reports new bindings.

4. **Find it and read it.** Prompt:

   ```text
   Search osk for "test suite", then read the node you found and quote its body.
   ```

   **Check:** `search` returns the node's title and summary. `read_node` returns
   its `body` and a `hash`. The agent needs that hash only to replace a whole
   body; small edits use `old_text` and `new_text`.

5. **Save a lesson in scope memory** (optional). Prompt:

   ```text
   Add one line to this project's osk scope memory: "Tests run with make test."
   ```

   **Check:** the result shows `"ok": true` and `"limit": 1500`.

6. **Confirm from the terminal.** Run these from the vault root with
   `PYTHONPATH` set.

   macOS/Linux:

   ```bash
   ls 00_Scope/my-app
   .venv/bin/python -m osk.cli search "test suite"
   .venv/bin/python -m osk.cli sm show --session my-app
   ```

   Windows (PowerShell):

   ```powershell
   Get-ChildItem 00_Scope\my-app
   .venv\Scripts\python.exe -m osk.cli search "test suite"
   .venv\Scripts\python.exe -m osk.cli sm show --session my-app
   ```

   **Check:** the folder lists the hub and the new node, plus `_raw/` once a
   round has been captured. `search` returns the new node. `sm show` prints
   your scope-memory line. For a key that is not bound yet, or a scope memory
   that is still empty, `sm show` prints nothing and exits 0; that is not an
   error.

Commit the new files with `git add -A` and `git commit`, or let the sync daemon
do it. If you saved a line in item 5, the next session in `my-app` starts with
that scope memory. The injected block begins with
`[osk scope 기억 — 00_Scope/my-app · N/1500자 · 여유 M자]`: N characters used
out of 1,500, and M characters still free (`여유`).

### What "bound scope" means

- The binding is a row in `00_Scope/Workbench/_ledger/routing.jsonl` that maps
  the session key `my-app` to the scope `my-app`.
- Later writes with that key land in that scope without a `space` argument.
  That includes nodes, scope memory and captured conversations, from any
  conversation, device or worktree of the repository.
- A session belongs to exactly one scope. A write from `my-app` into another
  scope is refused, and the refusal says the session `…에 결속돼 있다` ("is
  bound to …"). Knowledge useful beyond one project belongs in `00_Domain/`,
  and knowledge about you in `00_Person/`. Every session can write to both.
- A binding is permanent, and no command undoes it, so choose the scope name
  with care. Several repositories can share one scope: give the first write
  from each one the same `space`.
- Never use a conversation ID as the key. A UUID-shaped key is refused with
  `1회용 대화 id` ("one-time conversation ID"), because the next conversation
  could never find that memory again.

### Hook messages you will see

| Starts with | Meaning |
|---|---|
| `[osk 세션 시작 — session="…"]` | Session start. Shows the session key and asks the agent to call `overview`. `아직 scope 결속이 없다` means "not bound yet". |
| `[osk scope 기억 — 00_Scope/… · N/1500자 · 여유 M자]` | The scope memory: characters used out of 1,500, characters free, then its hash and full text. |
| `[osk 검토 경고 — <reason>. …]` | Background fork reviews are not running, for the reason given. Reviews happen in this session at user turns 9 and 15. Normal if you have not set up fork reviews. |
| `[osk 대화 검토 — …]` | Fork reviews are on: one runs after every 9 successful final answers. |
| `[osk 케이던스 — user 턴 N]` | A review is due. At turn 9 the agent reviews along with its next tool call. At turn 15 it may spend a whole turn on the review. Before the scope is bound, it appears on every message because capture fails (Step 5). |
| `[osk 대화별 통합 대기 — …]` | This conversation's review queue, with instructions for the agent. |
| `[osk 참조·조직 검토]` | Work to tidy links and hubs among this scope's nodes. |
| `[osk 정돈 — …]`, `[osk 정돈이 밀렸다 — …]` | Evicted scope-memory lines waiting to be settled. `밀렸다` means overdue: older than 14 days. |
| `[osk scope 복구 대기 — …]` | The scope memory hit its limit. The agent should prune entries or move them into nodes. |
| Anything containing `진단` or `diagnostic` | A hook step failed. Your work continues, and nothing was marked done. See [Troubleshooting](#troubleshooting). |

## Everyday commands

Run these from the vault root with `PYTHONPATH` set. Prefix each one with
`.venv/bin/python -m osk.cli`, or on Windows with
`.venv\Scripts\python.exe -m osk.cli`.

| Command | Use it to |
|---|---|
| `validate` | Check the whole vault: node contracts, links and ledgers. Expect `"verdict": "PASS"`. |
| `status` | See protected regions (`clean` or `pending`), unsettled evictions, and scope-memory recovery. |
| `search "<words>"` | Search the nodes. Raw records are not searched. |
| `sm show --session <key>` | Print a scope's memory. |
| `tidy list` | List evicted scope-memory lines that are not settled yet. |
| `integration list` | List conversations whose captured rounds wait for review. |
| `protect <folder>`, `approve <folder>`, `revert <folder>` | Protect a folder, or accept or undo its pending changeset. These ask `[y/N]` and refuse to run without an interactive terminal. |
| `fork doctor` | Check whether fork reviews can run. Read-only. |

## Optional: browse the vault in Obsidian

The graph can show only knowledge nodes, without raw records or governance
files. To set that up, preview the change first and then apply it. If Obsidian
has this vault open, close it first: Obsidian writes the same settings file
itself when you use the graph.

macOS/Linux:

```bash
.venv/bin/python _governance/_engine/scripts/configure_obsidian_graph.py --vault-root .
.venv/bin/python _governance/_engine/scripts/configure_obsidian_graph.py --vault-root . --apply
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe _governance\_engine\scripts\configure_obsidian_graph.py --vault-root .
.venv\Scripts\python.exe _governance\_engine\scripts\configure_obsidian_graph.py --vault-root . --apply
```

**Check:** the second command prints `"applied": true`. It changes
`.obsidian/graph.json`, which stays on this device (Git ignores it). If that
file already existed, the old version is saved beside it as
`.obsidian/graph.before-osk-<hash>.json`. Git does not ignore this backup, so
`git add -A` and the sync daemon commit it.

Then, in Obsidian, choose *Open folder as vault* and pick `<vault>`.

## Optional: sync the vault with Git

The sync daemon runs every 15 minutes by default. Each time, it:

1. commits everything in the vault (`git add -A`) on `main`;
2. rebases onto `origin main`;
3. pushes.

It only ever syncs `main`, and it does nothing unless `SYNC_ENABLED=1` is set.
Before you start it:

- `origin` must be your private remote (Step 1).
- Git must be able to push without prompting, through a credential helper,
  token or ssh-agent.
- Start the daemon with the **absolute path** of `sync_daemon.py`. `osk.update`
  finds and restarts the daemon by that path. It cannot find a daemon that was
  started with a relative path.

Try one round first.

macOS/Linux:

```bash
SYNC_ENABLED=1 .venv/bin/python <vault>/_governance/_engine/sync_daemon.py --once
```

Windows (PowerShell):

```powershell
$env:SYNC_ENABLED = "1"
.venv\Scripts\python.exe <vault>/_governance/_engine/sync_daemon.py --once
```

**Check:** it prints `ok`. If the vault had uncommitted changes, your remote now
has a commit named `sync: <date> <time> (daemon)`. Without the variable, the
daemon exits with `sync 비활성 — SYNC_ENABLED=1 …`.

To keep the daemon running in the background:

- **macOS (launchd):**

  ```bash
  cp _governance/_engine/scripts/launchd/com.ltm-vault-daemon.plist.example ~/Library/LaunchAgents/com.example.ltm-vault-daemon.plist
  # Edit the copy: replace <REPO> with your vault path and <HOME> with your home directory.
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.ltm-vault-daemon.plist
  launchctl list | grep ltm-vault-daemon
  ```

  The log is `~/Library/Logs/ltm-vault-daemon.log`. The template's `PATH`
  assumes Homebrew on Apple silicon (`/opt/homebrew/bin`); on an Intel Mac, use
  `/usr/local/bin`. To stop it, run
  `launchctl bootout gui/$(id -u)/com.example.ltm-vault-daemon`. Do not just
  delete `SYNC_ENABLED` from the plist: the daemon would exit and launchd would
  restart it in a loop.

- **Linux (systemd user service):**

  ```bash
  mkdir -p ~/.config/systemd/user
  cp _governance/_engine/scripts/systemd/ltm-vault-daemon.service.example ~/.config/systemd/user/ltm-vault-daemon.service
  # Edit the copy: replace every <REPO> with your vault path.
  systemctl --user daemon-reload
  systemctl --user enable --now ltm-vault-daemon.service
  loginctl enable-linger "$USER"
  systemctl --user status ltm-vault-daemon
  ```

  Read the log with `journalctl --user -u ltm-vault-daemon -f`. To stop it, run
  `systemctl --user disable --now ltm-vault-daemon.service`. Do not just delete
  the `SYNC_ENABLED` line: the daemon would exit and systemd would restart it in
  a loop.

- **Windows:** the repository ships no Task Scheduler template. You can run the
  daemon in a terminal you keep open: use the command above without `--once`,
  and press Ctrl+C to stop it.

  For an always-on daemon, register a task that starts at logon. Task Scheduler
  has no per-task environment variables, so the task runs `cmd.exe`, which sets
  `SYNC_ENABLED=1` and starts the vault's `pythonw.exe` with the **absolute
  path** of `sync_daemon.py`. Run this in PowerShell, with your own `<vault>`:

  ```powershell
  $vault = "C:/osk/my-osk-vault"
  $run = "$vault/.venv/Scripts/pythonw.exe $vault/_governance/_engine/sync_daemon.py"
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c set SYNC_ENABLED=1&& start "" ' + $run)
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries
  Register-ScheduledTask -TaskName "osk-sync-daemon" -Action $action -Trigger $trigger -Settings $settings
  Start-ScheduledTask -TaskName "osk-sync-daemon"
  ```

  - Keep `1&&` together. A space before `&&` becomes part of the value, and
    the daemon exits with `sync 비활성`.
  - `start ""` lets `cmd.exe` exit at once, so a console window only flashes
    when the task starts. `pythonw.exe` opens no window.
  - `-AllowStartIfOnBatteries` lets the task start on a laptop that runs on
    battery.
  - `osk.update` finds this task by the path in its action, and starts it
    again after an update.

  **Check:** this command lists the running daemon. A venv Python shows up as
  two processes.

  ```powershell
  Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" | Where-Object CommandLine -like '*sync_daemon.py*' | Select-Object ProcessId, CommandLine
  ```

  To stop the daemon, run `Stop-Process -Id <ProcessId>` for the processes
  listed with your vault's path. `Stop-ScheduledTask` does not stop it,
  because the task ends as soon as the daemon starts. To remove the task, run
  `Unregister-ScheduledTask -TaskName osk-sync-daemon`.

## Optional: background fork reviews

Without fork reviews, reviews happen inside your sessions, at turns 9 and 15.
With them, the Stop hook launches a hidden, one-shot *fork* of the conversation
after every 9 successful final answers. The fork keeps the conversation's
harness, model, working folder and permission mode. It reviews up to 9 rounds
that have not been reviewed yet, and writes knowledge through MCP. It runs on
your **subscription** login, and never falls back to paid API calls. You turn
it on per device and per harness.

1. **Tell osk which CLI to use.** Create `<vault>/.osk/response-growth.json`.
   Everything under `.osk/` stays on this device: Git ignores it. The file may
   contain only the keys `claude` and `codex`, and each value is the absolute
   path of a native CLI. For the Windows desktop apps, the paths look like this:

   ```json
   {
     "claude": "C:/Users/you/AppData/Roaming/Claude/claude-code/2.1.280/claude.exe",
     "codex": "C:/Users/you/AppData/Local/OpenAI/Codex/bin/0123456789abcdef/codex.exe"
   }
   ```

   - The fork runs only when the CLI's version equals the version that
     recorded the conversation.
   - With the Windows desktop-app paths shown above, osk switches to the sibling
     folder that holds the matching version, if one is installed. Any other path
     is used exactly as written.
   - To find the desktop-app paths, run these in PowerShell. Any listed path
     works; copy one per harness into the file, with forward slashes.

     ```powershell
     (Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe").FullName
     (Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe").FullName
     ```

   - If you use a standalone `claude` or `codex` CLI (on macOS, Linux or
     Windows), register the absolute path of the binary you run. On macOS or
     Linux, `command -v claude` prints it; in PowerShell,
     `(Get-Command claude).Source` does.
   - Leave out a harness that you do not want forked.

2. **Log the CLI in with your subscription**, using the same binary.

   - **Claude:** run `<claude> auth login` and choose your claude.ai Pro, Max,
     Team or Enterprise account. On Windows, run it in a normal PowerShell
     window, outside the app:

     ```powershell
     & "C:/Users/you/AppData/Roaming/Claude/claude-code/2.1.280/claude.exe" auth login
     ```

     The desktop app's login does not carry over to the CLI. An API-key
     (Console) login is refused.
   - **Codex:** run `<codex> login` and sign in with ChatGPT. Then
     `<codex> login status` must report `Logged in using ChatGPT`. A Codex fork
     also needs the project folder to be a Git work tree, or a project marked
     trusted in Codex's `config.toml`.

3. **Check that forks can run.** The `fork doctor` command is read-only and
   starts no model. Run it from the vault root with `PYTHONPATH` set (Step 2).

   macOS/Linux:

   ```bash
   .venv/bin/python -m osk.cli fork doctor --harness claude
   .venv/bin/python -m osk.cli fork doctor --harness codex
   ```

   Windows (PowerShell):

   ```powershell
   .venv\Scripts\python.exe -m osk.cli fork doctor --harness claude
   .venv\Scripts\python.exe -m osk.cli fork doctor --harness codex
   ```

   **Check:** the first line reads `claude: background` or `codex: background`.
   A line like `foreground — <reason>` names what to fix; see
   [Troubleshooting](#troubleshooting).
   - Without `--session`, `fork doctor` checks the folder you run it from, here
     the vault, so it does not see `my-app`'s Git status, Codex trust or
     `.claude` settings.
   - Add `--session <conversation-id>` to check one real conversation, in its
     own folder.
     - For Claude, the ID is the transcript's file name without `.jsonl`,
       under `~/.claude/projects/`.
     - For Codex, it is the ID at the end of the `rollout-….jsonl` file name,
       under `~/.codex/sessions/`.
   - Add `--json` for the full report.

**Bridges, proxies and API keys.** The fork always removes `OPENAI_API_KEY`,
`CODEX_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` from its environment.

- **Codex.** The fork pins the official backend,
  `https://chatgpt.com/backend-api/codex`, so it bypasses a local bridge or
  proxy set as the top-level `openai_base_url`. It refuses to run if any config
  layer sets one of the following. The layers are the system file, your
  `~/.codex/config.toml`, and each project `.codex/config.toml`.
  - an active profile that sets a key the fork pins;
  - a `model_providers.openai` base URL other than the official backend;
  - a `chatgpt_base_url` other than the official one.

  It also refuses a conversation whose model is a `chatgpt-web/…` bridge route.
- **Claude.** The fork refuses to run in either of these cases:
  - Your environment sets a non-first-party `ANTHROPIC_BASE_URL`, or
    `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or
    `CLAUDE_CODE_USE_FOUNDRY`.
  - A `settings.json` (yours, or the project's `.claude/settings.json` or
    `.claude/settings.local.json`) sets `apiKeyHelper`, or puts an API key,
    base URL or provider switch under `env`.

`fork doctor` reports each of these. Fork results appear in `.osk/growth/runs/`.
To see one conversation's results, run
`.venv/bin/python -m osk.cli integration status --harness <claude|codex> --conversation <id>`
(Windows: `.venv\Scripts\python.exe`) the same way as `fork doctor`. For the
full design, see
[SETUP → 대화별 검토 훅](SETUP.md#대화별-검토-훅-최종-답변-stop-9회) and
[response-growth.md](response-growth.md).

## Keeping up to date

Releases always come from the canonical repository, whatever your `origin` is.
Run these from the vault root with `PYTHONPATH` set (Step 2).

macOS/Linux:

```bash
.venv/bin/python -m osk.update            # report only: "version" (newest release) vs "current"
.venv/bin/python -m osk.update --apply    # prints the changeset, changes nothing, exits 2
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe -m osk.update
.venv\Scripts\python.exe -m osk.update --apply
```

If `current` already equals `version`, you are up to date, so stop here.
`--apply` would still ask for approval, with an empty changeset.

Otherwise, the first `--apply` changes nothing; it only prints the changeset.
The changeset shows the files to `add`, `update` and `remove`, and any
documents you edited (`conflict`). The run then exits with code 2 and
`"approval_required": true`.

Read the changeset. If you agree with it, run **the same command again** within
an hour: that run applies the update and exits 0. If the release or your files
change in between, the updater asks for confirmation again.

What the apply step handles for you:

- **Sync daemon.** If this vault's daemon is running, the apply step stops it
  and starts it again afterwards. The report's `daemon` field shows `stopped`
  and `restarted`. If the restart fails, `daemon.note` tells you to start the
  daemon yourself. On Windows this happens when no scheduled task runs the
  daemon.
- **Documents you edited.** They are not overwritten. The new version is saved
  beside yours as `<file>.upstream-<version>`, for you to merge.
- **Engine files you edited.** A local edit to an engine file stops the whole
  update. Changes to the engine belong upstream.

After an update:

- **Restart** every Claude Code and Codex session that uses osk. Do this after
  every applied update, so that each MCP server loads the new engine. Until
  then, writes are refused with `적재판 … ≠ 디스크판` ("loaded engine ≠ engine
  on disk").
- **Reinstall the requirements** if `_governance/_engine/requirements.txt` is in
  the update list. Run the `pip install` line from Step 2 again.
- **Commit** the updated files, or let the daemon do it.
- **On other devices** that share this vault, do not run `osk.update`. Pull,
  restart the sessions, and reinstall the requirements if they changed.

**Check:** `osk.update` without `--apply` now reports the same value for
`current` and `version`, and `validate` still ends with `"verdict": "PASS"`.

If an update is interrupted, the next `--apply` recovers first. If `osk.update`
itself no longer starts, run the recovery script. It uses only the standard
library.

- macOS/Linux: `python3 _governance/_engine/scripts/recover.py --apply`
- Windows: `py -3 _governance\_engine\scripts\recover.py --apply`

## Troubleshooting

**Installing and connecting**

| Symptom | Fix |
|---|---|
| Typing `osk` opens the On-Screen Keyboard, or says *command not found* | There is no `osk` command. Run `.venv/bin/python -m osk.cli …` (Windows: `.venv\Scripts\python.exe -m osk.cli …`) from the vault root, with `PYTHONPATH` set. |
| `No module named 'osk'` | `PYTHONPATH` is not set in this terminal, or you are not in the vault root (Step 2). |
| `No module named 'mcp.server.fastmcp'` | You are using a different Python, or mcp 2.x is installed. Use the vault's `.venv` Python and reinstall `_governance/_engine/requirements.txt`, which pins `mcp<2`. |
| Windows: `ZoneInfoNotFoundError` for `Asia/Seoul` | The venv lacks the `tzdata` package. Reinstall the requirements into it. |
| `claude mcp list` shows no *Connected*, or Codex cannot start the server | The registered command must use the vault's `.venv` Python (`.venv/Scripts/python.exe` on Windows). Run the same command in a terminal to see the error. A healthy server prints nothing and waits for a client; press Ctrl+C to stop it. |
| No osk text at session start | Claude Code loads hooks only at startup, so open a new session and check `/hooks`. In Codex, trust the entries in `/hooks`. In both, registering MCP does not install the hooks, and each hook command must use the vault's `.venv` Python. Run the hand test from Step 3b. |
| Hook text repeats `착지 미정` or `scope 결속이 없다` | This repository's key is not bound yet. Create or choose its scope (Step 5, item 2). |

**Write refusals your agent may report**

| Text in the refusal | Meaning and fix |
|---|---|
| `아직 없는 군집이라 … 새 군집을 만든다` | A new top-level cluster needs one confirmation. Confirm, then have the agent send the same request again within an hour. |
| `착지가 정해지지 않았다 — space를 지정하라` | The session is not bound, and the write gave no `space`. Pass `space="00_Scope/<name>"` together with `session` once. |
| `에 결속돼 있다` | The session is bound to a different scope. Leave out `space`, or write knowledge that spans projects to `00_Domain/`. |
| `1회용 대화 id` | A conversation UUID was used as the session key. Use the key that the hook printed. |
| `군집이 비어 있다` | A new cluster must start with its hub, named after the folder. Create the hub first. |
| `drafter는 하네스명이 아니라 모델명` | `drafter` must be the model's own name in lowercase, not `claude` or `codex`, and without a `provider:` prefix. |
| `summary` with `한도 80` or `한 줄`, or *at most 80 characters* | A summary is one line of at most 80 characters, without `[[`. |
| `앵커 편집` or `expect_hash` | Replacing a whole body needs the `hash` from `read_node`. For a small change, use `old_text`/`new_text`. |
| `적재판 … ≠ 디스크판` | The engine changed on disk after the MCP server started, through an update or a pull. Restart the session, then send the request again. |

**Updates and sync**

| Symptom | Fix |
|---|---|
| `osk.update --apply` exits with code 2 | This is expected: that run only shows the changeset. Review it and run the same command again within an hour. |
| `[중단] 엔진 파일에 로컬 수정이 있다 …` | Engine files differ from the recorded baseline, or there is no baseline at all. See the note after this table. |
| `동기화 데몬 잠금이 잡혀 있는데 … 프로세스를 찾지 못했다` | A daemon is running, but it was started with a relative path, so the updater cannot find it. Stop it, start it again with the absolute path, and rerun the update. |
| Daemon exits with `sync 비활성 — SYNC_ENABLED=1 …` | Set `SYNC_ENABLED=1` in the daemon's environment. For a Windows task, use the `cmd.exe` action from [the sync section](#optional-sync-the-vault-with-git). |
| Daemon refuses to sync, reporting another branch or no `main` | The daemon syncs only `main`, and it will not move edits you have not committed on another branch. Commit your work, then `git switch main`. |
| `<file>.upstream-<version>` files appear after an update | You had edited those documents, so the new version was saved beside yours. Merge the changes by hand, then delete the extra file. |

About the `엔진 파일에 로컬 수정이 있다` error ("engine files have local edits"):

- **The clone never recorded a baseline** because Step 2 was skipped or the
  clone came from `main`. Run `.venv/bin/python -m osk.update --apply --adopt`
  (Windows: `.venv\Scripts\python.exe -m osk.update --apply --adopt`), review
  the changeset, then run the same command again. Each file it replaces is kept
  as `<file>.local-<version>`; delete those copies if you never edited the
  files.
- **You edited engine files yourself.** Undo the edits. Changes to the engine
  belong upstream.

**Fork reviews** (the reason printed by `fork doctor` after `foreground —`)

| Reason | Fix |
|---|---|
| `subscription fork CLI is not configured` | There is no `.osk/response-growth.json`, or it has no entry for this harness. |
| `configure an existing absolute native CLI path`, or `[WinError 3] The system cannot find the path specified` | The path in `.osk/response-growth.json` does not exist. Find the real path (item 1 above) and correct the file. |
| `matching Claude Desktop CLI is unavailable` or `matching Codex Desktop CLI is unavailable` | The desktop app no longer has the CLI version that recorded this conversation. Its reviews stay in the session. Conversations recorded by an installed version are not affected. |
| `configured CLI version differs from the source harness` | A standalone CLI's version differs from the one that recorded the conversation. Register a binary of that version. |
| `… subscription login is required; no API fallback` | Log that CLI in with claude.ai (`auth login`) or ChatGPT (`login`). API keys are never used. |
| `not a Git worktree or trusted Codex project` | Run Codex in a Git repository, or trust the folder in Codex. |
| `non-first-party endpoint`, `unverified API/provider path` or `unverified provider` | A proxy, profile, provider or API setting would redirect the fork. See [Bridges, proxies and API keys](#optional-background-fork-reviews). |

## Where to go next

- [SETUP.md](SETUP.md) (Korean) is the operator reference. Useful sections:
  - [MCP server](SETUP.md#mcp-서버)
  - [CLI command table](SETUP.md#cli)
  - [Scope-memory hook](SETUP.md#scope-기억-주입-훅-sm-show)
  - [Per-conversation reviews](SETUP.md#대화별-검토-훅-최종-답변-stop-9회)
  - [Codex hooks](SETUP.md#codex에도-훅을-등록한다)
  - [Daily Scope → Domain review](SETUP.md#scope에서-domain으로-정기-재검토)
  - [Sync daemon](SETUP.md#동기화-데몬)
  - [Releases and updates](SETUP.md#정본-릴리스와-갱신)
- [response-growth.md](response-growth.md) explains how fork reviews work, with
  cache measurements and limits.
- [space-layout-migration.md](space-layout-migration.md) covers vaults with
  `= Scope` roots, and recovery from v3.20.1.
- [WINDOWS-SHELL.md](WINDOWS-SHELL.md) (Korean) explains how Git Bash rewrites
  path-like arguments to native tools.
- The governance documents (Korean) define the rules. Read them in this order:
  [Constitution](../_governance/Constitution.md),
  [Bylaws](../_governance/Bylaws.md), then
  [Mechanism](../_governance/Mechanism.md). The
  [Workbench contract](../_governance/Workbench-Contract.md) covers the
  operational scope.
- The [engine README](../_governance/_engine/README.md) includes its
  [known limits](../_governance/_engine/README.md#알려진-한계).
- The [README](../README.md) gives the project overview and design rationale.
