# Installing osk-system — instructions for an agent

The user asked you to install osk-system on this device. Follow the steps in
order. Where a step says to ask, ask and wait; do not guess the user's answer.

- Speak to the user in the user's language, in plain words. The user may not
  be a developer: say what each step does for them, and do not pass osk's own
  words (scope, binding, cluster, hub, protected region) to the user without
  their everyday meaning. The [glossary](OPERATE-AGENT.md#glossary) has them.
- Never push the vault to the public repository.
- Do not edit harness configuration by hand. `setup.py` does it, backs up each
  file first, and touches only this vault's osk entries.
- `setup.py --apply` stops once with `"approval_required": true` and exit code 2.
  Explain the plan and wait for the user's explicit yes before you run the same
  command again. Never re-run it on your own.
- Everything after the install — updates, connection checks, optional features,
  a backup remote, removal — is in [OPERATE-AGENT](OPERATE-AGENT.md).

## 1. Check what the device has

- Python 3.11 or newer: `python3 --version`, or `py -3.11 --version` on Windows.
- Git, with `user.name` and `user.email` set (`git config --global --get user.name`).
- At least one host: Claude Code, Codex, Kiro or Antigravity. Kiro and Antigravity have
  no subscription fork, so `--fork` and `--schedule` are for Claude Code and Codex only.

If Python or Git is missing, offer to install it, and install it only after the
user says yes:

| Missing | Windows | macOS | Debian or Ubuntu |
|---|---|---|---|
| Python | `winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements` | `brew install python@3.12`; without Homebrew, the user runs the installer from python.org | Debian 12, Ubuntu 24.04 or newer: `sudo apt install python3 python3-venv`. Ubuntu 22.04, whose `python3` is 3.10: `sudo add-apt-repository ppa:deadsnakes/ppa`, then `sudo apt install python3.12 python3.12-venv` |
| Git | `winget install --id Git.Git --accept-source-agreements --accept-package-agreements` | `xcode-select --install`, or `brew install git` | `sudo apt install git` |

A shell that was open before the install does not see the new program. Use the
interpreter you installed in every command below (`py -3.12` on Windows,
`python3.12` from Homebrew or the PPA), or open a new shell.

If the Git identity is missing, ask the user for the name and e-mail that will
sign their vault's commits, and say what that means: the e-mail becomes part of
every commit, and `--global` makes this the identity of every Git repository on
this device, which is the normal setup for a user who has none yet. A GitHub
user who would rather not show their address can give the noreply address from
GitHub's e-mail settings. A user who wants the identity for osk only gets it
with `--local` instead, run inside the vault after step 3 and inside each folder
of step 9.

```bash
git config --global user.name "<name>"
git config --global user.email "<email>"
```

A host is not something to install: the user is already talking to you in one.

## 2. Ask the user

- **Where** to create the vault. Suggest a short path without spaces or shell
  special characters (quotes, `& % ! $ ( ) ; ,`): `C:/osk/vault` on Windows,
  `~/osk-vault` elsewhere. On Windows the home folder often carries a user name
  with a space, and hook commands are plain command lines; Antigravity on
  Windows cannot run a quoted path at all. The folder must not exist yet.
- **Which private Git repository** will hold it. It should be empty and private,
  because the vault stores conversation records and personal notes. The user may
  have none yet; the vault then stays local. Say in plain words what that means:
  there is no copy anywhere else, so the user backs the folder up themselves
  until a remote is added ([how](OPERATE-AGENT.md#add-a-private-remote-later)).
- **Which optional features** to turn on. Each is off unless the user says yes,
  and each becomes one flag of `setup.py` in steps 4 and 6:
  - Background fork reviews (`--fork`): every ninth answer, the host reviews the
    conversation in a hidden one-off fork on the user's subscription.
  - A daily review run (`--schedule claude`, optionally `--at 07:30`): once a day,
    on the user's Claude subscription, the run reviews what the sessions left and
    compares the Scope notes for Domain knowledge. Register it on one device only.
    `setup.py` writes the command for Claude only; a Codex user who already has
    `.osk/growth-command.json` can use `--schedule` with it, and then gets the Claude
    worker that runs the day Codex is out of usage and credits.
  - Git sync (`--sync`): a background daemon commits the vault every 15 minutes
    and pushes it. It needs the private repository and a push that does not ask
    for a password.

## 3. Clone the newest release

Clone a release tag, not the `main` branch: the updater compares the vault with
a release. Find the newest `vX.Y.Z` tag of the canonical repository:

```bash
python -c "import re,subprocess;t=subprocess.run(['git','ls-remote','--tags','--refs','https://github.com/lpaiu-cs/osk-system.git'],capture_output=True,text=True,check=True).stdout;print(max(re.findall(r'refs/tags/(v\d+\.\d+\.\d+)$',t,re.M),key=lambda v:tuple(map(int,v[1:].split('.')))))"
```

Then, with that tag and the folder from step 2:

```bash
git clone --branch <tag> https://github.com/lpaiu-cs/osk-system.git <folder>
cd <folder>
git switch -c main
```

With a private repository: `git remote set-url origin <private-url>` and
`git push -u origin main`. Without one: `git remote remove origin`.

## 4. Plan the installation

From the vault root, with the Python from step 1, adding the flags of the
features the user chose in step 2:

```bash
python _governance/_engine/scripts/setup.py --apply [--fork] [--schedule claude] [--sync]
```

The first run creates `.venv` and installs the dependencies. This takes a
while; pip writes to standard error. Standard output is one JSON report. It
exits with code 2 and `"approval_required": true`: nothing outside the vault
has changed yet.

## 5. Show the plan and ask for confirmation

Summarize the report for the user:

- `baseline`: the release version it records and whether `_governance` gets
  protected (`governance`).
- For each entry in `hosts`: the MCP `action`, and the hook `events` with the
  file they go into. Mention every `notes` line.
- That each changed file is copied next to itself as
  `<name>.osk-backup-<time>` first.
- For the chosen features:
  - `fork`: the CLI each host's fork will run (`entries`).
  - `schedule`: the daily command (`command.argv`), the time (`at`), and what
    happens to the OS task (`task`).
  - `sync`: the `origin`, every `push` target the daemon will push to (all must
    be private), and the OS service (`task`).
  - An OS registration that `task.remove` lists is this vault's older one; it is
    replaced. Its definition is kept first in `~/.osk-system/backups/`.
- The `human` steps the user will have to do.

Ask the user to confirm. If `errors` is present, show it and stop.

## 6. Apply

After the user says yes, run the same command, with the same flags, within an
hour:

```bash
python _governance/_engine/scripts/setup.py --apply [--fork] [--schedule claude] [--sync]
```

It applies and reports `steps`, `backups` and `human`. If it asks for
approval again, the plan changed in between: show the new plan and ask again.

Then check the one-off workspaces. Some harnesses open each conversation or
task in a new folder that they create, such as Codex desktop's dated chat
folders. osk does not bind the session key of such a folder. The engine has
rules for the harnesses in [SETUP](SETUP.md#일회성-작업-공간) (Korean), checked
on Windows. For a harness the user works in that has no rule on this OS, find
the folder it creates under its root, and add a rule from the vault root:

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -c "from osk import workspaces; print(workspaces.add('<harness>', '<root folder>', r'<regex of the path under the root>', '<harness version>', '<note>'))"
```

On Windows, use `.venv\Scripts\python.exe`. The root may use `~` and
environment variables. The regex matches the start of the path under the root,
joined with forward slashes.

## 7. Hand over the human steps

Tell the user each line of `human`, for example trusting the three osk hooks in
Codex with `/hooks`, trusting the workspace folder in Kiro, and starting a new
session in each host. Commit the
recorded baseline:

```bash
git add -A
git commit -m "Record osk release baseline"
git push
```

Skip `git push` if the vault has no remote.

## 8. Check the connection

After the user has opened a new session:

```bash
python _governance/_engine/scripts/setup.py doctor
```

For each host it shows whether the MCP server and the three hooks point at this
vault, when each hook last ran on this device, and whether the agent called
`overview` after the session started. Report failures to the user. A hook that
has not run yet needs a new session (Claude Code), trust (Codex), a trusted
workspace folder and a new chat (Kiro) or a new conversation (Antigravity).

## 9. Help with the first session

osk keys memory by the folder the host is opened in: a Git repository's folder
name becomes the session key, and the first write from that key binds it to a
scope, one project's or topic's memory. The binding is permanent. So the first
session should not run in a catch-all folder such as the home folder or
`Documents`: folders outside Git share memory by name, and that binding would
collect everything the user ever does.

Ask what the user will use osk for first.

- A developer opens the host in a project repository, as usual.
- A user without project repositories gets one folder per topic. Create it
  with the user, with a first commit so that the folder has a Git identity of
  its own and no other folder of the same name shares its memory:

  macOS/Linux:

  ```bash
  mkdir -p ~/work/<topic>
  cd ~/work/<topic>
  git init -b main
  git commit --allow-empty -m "Start <topic>"
  ```

  Windows (PowerShell; it has no `&&`, so one command per line):

  ```powershell
  mkdir C:/work/<topic>
  cd C:/work/<topic>
  git init -b main
  git commit --allow-empty -m "Start <topic>"
  ```

  Check that each command succeeded before the next; `git log` must show the
  one commit.

Then ask the user to open a new session of the host inside that folder and to
tell the agent there what the topic is. That agent creates the scope by writing
its hub node; the first write is refused once (`아직 없는 군집이라 … 새 군집을
만든다`) and succeeds when the same request is sent again within an hour.
[Getting started, step 5](GETTING-STARTED.md#step-5-your-first-session) walks
through it.

## Later

When the user asks to update, check, fix, change or remove osk-system, read
[OPERATE-AGENT](OPERATE-AGENT.md).
