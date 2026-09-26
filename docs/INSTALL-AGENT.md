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

## 2. Ask the user two things

- **Where** to create the vault, for example `~/osk-vault`. The folder must not
  exist yet.
- **Which private Git repository** will hold it. It should be empty and private,
  because the vault stores conversation records and personal notes. The user may
  have none yet; the vault then stays local.

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

From the vault root, with the Python from step 1:

```bash
python _governance/_engine/scripts/setup.py --apply
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
- The `human` steps the user will have to do.

Ask the user to confirm. If `errors` is present, show it and stop.

## 6. Apply

After the user says yes, run the same command within an hour:

```bash
python _governance/_engine/scripts/setup.py --apply
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

## Optional features

`setup.py` does not register these yet. Ask the user before you set any of them
up, and follow the sections of [GETTING-STARTED](GETTING-STARTED.md): syncing
the vault with Git, the scheduled review from Scope to Domain, and background
fork reviews.

## Removing

`python _governance/_engine/scripts/setup.py --uninstall --apply`, confirmed the
same way, removes only this vault's osk entries and keeps backups. It does not
delete the vault.
