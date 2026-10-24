# Operating osk-system — instructions for an agent

The user asked you to do something with an osk-system that is already installed
on this device: update it, check whether it works, fix a connection, turn a
feature on or off, add a backup remote, or remove it. Find the request below
and follow its steps in order. Where a step says to ask, ask and wait.

- Speak to the user in the user's language, in plain words. The user may not
  be a developer. The [glossary](#glossary) turns osk's own words into everyday
  ones; use it when you explain, and keep the original term beside it so that
  the user can find it in the documents and messages.
- Never push the vault to the public repository, and never use an API key for
  osk: forks and scheduled runs use the user's subscription login only.
- A command that stops with `"approval_required": true` and exit code 2 is
  asking for the user's explicit yes. Explain the plan, wait for the yes, then
  run the same command again within an hour. Never re-run it on your own.
- Do not edit engine files (`_governance/_engine/`), the governing documents or
  harness configuration by hand. `setup.py` and `osk.update` do it, and back up
  what they touch. Local edits to engine files stop every later update.
- Protecting a folder, and approving or reverting its pending changes, are the
  user's own acts, done in an interactive terminal (`protect`, `approve`,
  `revert`). Do not run them, and do not work around the terminal check. If
  the user wants to, give them the command to type ([everyday
  commands](GETTING-STARTED.md#everyday-commands)).
- Do not delete the vault folder. If the user wants it gone, they delete it
  themselves, after a backup they have checked.

## Finding the vault and the commands

The vault is the folder that holds `_governance/_engine/mcp_server.py`, the
file the host's `osk-system` MCP server is registered with:

- Claude Code: `claude mcp get osk-system` prints the command.
- Codex: the `[mcp_servers.osk-system]` entry in `~/.codex/config.toml`.
- Kiro: `~/.kiro/settings/mcp.json`. Antigravity: `~/.gemini/config/mcp_config.json`.

If none of these has it, ask the user where the vault is. Run every command
below from the vault root. There are two command shapes:

- `setup.py` and `doctor` run with the system Python, the one the install used:
  `python _governance/_engine/scripts/setup.py …` (`python3`, or `py -3.12`
  on Windows). They hand over to the vault's own environment themselves.
- The engine modules run with the vault's own Python, with `PYTHONPATH` set
  to the engine:

  macOS/Linux:

  ```bash
  PYTHONPATH=_governance/_engine .venv/bin/python -m osk.update …
  PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli …
  ```

  Windows (PowerShell):

  ```powershell
  $env:PYTHONPATH = "_governance/_engine"
  .venv\Scripts\python.exe -m osk.update …
  .venv\Scripts\python.exe -m osk.cli …
  ```

There is no `osk` command. On Windows, `osk` opens the On-Screen Keyboard. The
session-start text and the new-release notice already carry a one-line form of
these commands with the vault's paths filled in; that line works as it is.

## Update ("osk 업데이트해 줘")

Update only when the user asks. Several devices sharing one vault update on
**one** device; on the others, `git pull`, restart the host sessions, and
reinstall the requirements if `requirements.txt` changed.

1. **Check.** `osk.update` without flags prints `version`, the newest release,
   and `current`. If they are equal, say so and stop.
2. **Read the release notes** at
   `https://github.com/lpaiu-cs/osk-system/releases/tag/<version>`. A new
   major version (the first number changes) means reading
   [UPGRADING](UPGRADING.md) first and telling the user what changes for them.
3. **Back up.** With a remote, commit and push, or let the sync daemon do it.
   Without one, say that there is no copy elsewhere and offer to copy the vault
   folder first.
4. **Plan.** `osk.update --apply` (or `--to vX.Y.Z --apply` for a given
   version). The first run changes nothing: it prints the changeset and exits
   with code 2. Follow its `instruction` field, and explain the changeset in
   plain words: files to `add`, `update` and `remove`, and `conflict`, the
   documents the user edited, which are kept with the new version saved beside
   them as `<file>.upstream-<version>`. The user's notes (`00_Scope/`,
   `00_Domain/`, `00_Person/`) are never part of an update. Ask for the yes.
5. **Apply.** Run the same command again within an hour. If it asks for
   approval again, the release or the files changed in between: show the new
   plan and ask again. The apply stops and restarts this vault's sync daemon by
   itself (`daemon` in the report); if `daemon.note` says the restart failed,
   `setup.py --apply --sync` registers and starts it again.
6. **Afterwards**, tell the user to restart every host session that uses osk:
   until then, writes are refused with `적재판 … ≠ 디스크판` (the running
   server still has the old engine). If `_governance/_engine/requirements.txt`
   was in the changeset, reinstall:
   `.venv/bin/python -m pip install -r _governance/_engine/requirements.txt`
   (`.venv\Scripts\python.exe` on Windows). Then commit (`git add -A`,
   `git commit -m "Update osk-system to <version>"`) and push if there is a
   remote. **Check:** `osk.update` now prints the same `current` and `version`,
   and `osk.cli validate` ends with `"verdict": "PASS"`.

When the update refuses or breaks:

| Message or situation | What it means and what to do |
|---|---|
| `[중단] 엔진 파일에 로컬 수정이 있다 …` | Engine files differ from the recorded baseline. If the vault never recorded one (cloned from `main`, or the install skipped it), run `osk.update --apply --adopt` with the same two steps; it keeps each replaced file as `<file>.local-<version>`. If someone edited engine files, undo the edits. Never force. |
| The update was interrupted | The next `--apply` recovers first. If `osk.update` itself no longer starts, run `python3 _governance/_engine/scripts/recover.py --apply` (`py -3` on Windows); it needs only the standard library. |
| `동기화 데몬 잠금이 잡혀 있는데 … 프로세스를 찾지 못했다` | A daemon runs that was started with a relative path. Stop it, start it with the absolute path of `sync_daemon.py` (or `setup.py --apply --sync`), and run the update again. |
| `<file>.upstream-<version>` files remain | The user had edited those documents. Merge by hand with the user, then delete the extra file. |

## Check the connection ("osk 점검해 줘", "is osk working?")

```bash
python _governance/_engine/scripts/setup.py doctor
```

It is read-only. For each host it shows whether the MCP server and the three
hooks point at this vault, when each hook last ran on this device, whether the
session-start text reached the agent, the host version, and the fork setup. It
ends with a verdict line (`실패` fail, `경고` warn, `정보` info, `정상` ok) and
exits with 1 only when something cannot work. Report in plain words what works,
what does not, and the `→` fix line of each failing or warning item.

| Finding | Meaning and fix |
|---|---|
| A hook is registered but never ran | The host loads hooks at startup. Claude Code: open a new session. Codex: trust the three osk hooks with `/hooks`. Kiro: trust the workspace folder and open a new chat. Antigravity: open a new conversation. |
| No osk text at session start | Same as above. Registering the MCP server alone does not install the hooks; `setup.py --apply` registers both. |
| Fork items warn with a reason after `foreground —` | Background reviews fall back to in-session reviews at turns 9 and 15, which is normal without a fork setup. The reasons and fixes are in [Troubleshooting](GETTING-STARTED.md#troubleshooting). |
| `capture_blocked` in `osk.cli status` | Tracking this conversation's turns is stuck; nothing is lost and reviews still happen at turns 9 and 15. Show the error to the user. |

When the user asks whether the vault itself is sound, `osk.cli validate` checks
every node, link and ledger and ends with `"verdict": "PASS"`; `osk.cli status`
shows protected regions (`clean` or `pending`) and unsettled tidy work.

## Messages the user asks about

The hook text is addressed to the agent, in Korean. When the user sees one and
asks, explain it rather than quoting it. The full table is in
[Getting started](GETTING-STARTED.md#hook-messages-you-will-see); the ones
people ask about most:

| Starts with | Say |
|---|---|
| `[osk 검토 경고 — …]` | Background reviews are not set up, so the conversation is reviewed inside the session at the 9th and 15th message. Normal. |
| `[osk 케이던스 — user 턴 N]` | A review is due now; the agent does it, the user does nothing. |
| `[osk 새 릴리스 — …]` | A newer version exists. Nothing changes until the user asks for the update. |
| Anything with `진단` or `diagnostic` | A hook step failed. The work continues and nothing was marked done; run `doctor`. |
| `에 결속돼 있다` (in a refusal) | This folder's memory belongs to another project; the note goes there, or into `00_Domain/` if it is useful beyond one project. |
| `아직 없는 군집이라 …` (in a refusal) | Creating a new top-level group needs one confirmation; sending the same request again within an hour creates it. |

## Turn a feature on or off

The same two-step command as the install, with the feature's flag. Hosts that
are already connected come out as `keep`.

- On: `setup.py --apply --fork`, `--schedule claude [--at HH:MM]`, or `--sync`.
  Tell the user what each uses: forks and the daily run spend the user's
  subscription; sync pushes only with a private remote and a push that asks
  nothing, and without a remote it only commits.
- Off: `setup.py --uninstall --<flag> --apply`, for example
  `--uninstall --sync --apply`.

Each prints its plan first and applies on the confirmed second run, exactly as
in the install.

## Add a private remote later

A vault without a remote has no copy anywhere else. To add one:

1. Ask the user to create an **empty, private** repository on their Git host,
   with no README or license. On GitHub: New repository, Private, nothing
   initialized. Ask for its HTTPS URL. Never use the public osk-system URL.
2. From the vault root: `git remote add origin <url>` (or `git remote set-url
   origin <url>` if a remote exists), then commit anything uncommitted
   (`git add -A`, `git commit -m "osk vault"`) and `git push -u origin main`.
3. The first push asks for the user's sign-in; the user enters it, not you. If
   Git asks on every push, a credential helper is missing; see
   [sync](GETTING-STARTED.md#optional-sync-the-vault-with-git).
4. A daemon that was already running without a remote starts pushing on its
   next round, except to the public osk-system repository, which it never
   fetches from or pushes to. Run `setup.py --apply --sync` again anyway (two steps, as
   above): it checks the push target and the credentials first, or registers
   the daemon if there was none.

## Remove

`setup.py --uninstall --apply`, confirmed the same way, removes only this
vault's osk entries from the hosts, the fork settings, the daily task and the
sync daemon, and keeps backups of every file it changes. It does not delete the
vault. Tell the user that the folder, with all their notes, is still there and
that deleting it is their own step.

## Glossary

| In osk | Say to the user |
|---|---|
| vault | the memory folder on this device: plain Markdown files in a Git repository |
| node (노드) | one note: one claim with its sources |
| scope (`00_Scope/<name>`) | one project's or topic's memory |
| session key, binding (결속) | the folder the host was opened in, tied once and for all to a scope |
| scope memory (scope 기억) | the short summary, at most 1,500 characters, that every session of that project starts with |
| cluster, hub (군집, 허브) | a group of related notes, and the note that lists them |
| Domain, Person | memory useful across projects; memory about the user |
| protected region, approve, revert (보호영역, 승인, 반려) | a folder whose changes stay pending until the user accepts or undoes them in a terminal |
| review, fork (검토) | the step that turns a conversation into notes: inside the session at turns 9 and 15, or in the background on the user's subscription |
| cited record (`_cited/`) | the user's own words, copied from the transcript as evidence for a note |
| tidy (정돈) | lines pushed out of a full summary, waiting to be kept as notes or dropped |
| release, update (릴리스, 갱신) | a new version of the engine and its documents; the user's notes are not part of it |
| doctor | the read-only connection check |
