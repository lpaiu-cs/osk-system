# Installing osk-system — instructions for an agent

The user asked you to install osk-system on this device. Follow the steps in
order. Where a step says to ask, ask and wait; do not guess the user's answer.

- Speak to the user in the user's language.
- Never push the vault to the public repository.
- Do not edit harness configuration by hand. `setup.py` does it, backs up each
  file first, and touches only this vault's osk entries.
- `setup.py --apply` stops once with `"approval_required": true` and exit code 2.
  Explain the plan and wait for the user's explicit yes before you run the same
  command again. Never re-run it on your own.

## 1. Check what the device has

- Python 3.11 or newer: `python3 --version`, or `py -3.11 --version` on Windows.
- Git, with `user.name` and `user.email` set.
- At least one host: Claude Code or Codex.

If something is missing, tell the user what to install and stop.

## 2. Ask the user

- **Where** to create the vault, for example `~/osk-vault`. The folder must not
  exist yet.
- **Which private Git repository** will hold it. It should be empty and private,
  because the vault stores conversation records and personal notes. The user may
  have none yet; the vault then stays local.
- **Which optional features** to turn on. Each is off unless the user says yes,
  and each becomes one flag of `setup.py` in steps 4 and 6:
  - Background fork reviews (`--fork`): every ninth answer, the host reviews the
    conversation in a hidden one-off fork on the user's subscription.
  - A daily review run (`--schedule claude`, optionally `--at 07:30`): once a day,
    on the user's Claude subscription, the run reviews what the sessions left and
    compares the Scope notes for Domain knowledge. Register it on one device only.
    `setup.py` writes the command for Claude only; a Codex user who already has
    `.osk/growth-command.json` can use `--schedule` with it.
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
  - `sync`: the `origin` the daemon will push to, and the OS service (`task`).
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

## 7. Hand over the human steps

Tell the user each line of `human`, for example trusting the three osk hooks in
Codex with `/hooks`, and starting a new session in each host. Commit the
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
has not run yet needs a new session (Claude Code) or trust (Codex).

## Optional features later

The user can turn a feature on later with the same two steps, for example
`setup.py --apply --sync`. The hosts that are already connected come out as
`keep`.

## Removing

`python _governance/_engine/scripts/setup.py --uninstall --apply`, confirmed the
same way, removes only this vault's osk entries and keeps backups. That includes
the fork settings, the daily task and the sync daemon. It does not delete the
vault. To remove one feature only, add its flag, for example
`--uninstall --sync --apply`.
