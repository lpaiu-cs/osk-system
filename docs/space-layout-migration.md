# Space paths in v3.21.0

New distributions use `00_Scope/`, `00_Domain/`, and `00_Person/`: a `00_` prefix that
sorts them first and needs no shell quoting. Engine paths, publishing skeletons,
documentation and tests use these names. Shell commands should still quote paths;
node and repository names may contain spaces independently of these roots.

v3.20.1 could report a successful update from v3.20.0 and then refuse to start
because the existing vault still had `= Scope`. v3.21.0 removes that import-time
rejection. Each Space selects its existing physical root from `00_Scope`, `= Scope`
or `Scope` (and the corresponding Domain/Person names). An empty skeleton does
not displace a populated root. Multiple populated roots for the same Space are
an ambiguity that requires manual review; the engine never merges them silently.

Existing roots are **not renamed during an engine update**. Their ledgers,
approval manifests, content hashes, saved raw coordinates and session state keep
their original identities. Public path resolution accepts the three root
spellings and resolves to that Space's selected physical root. The updater's
instance-data floor covers all three spellings. Recovery and synchronization
also inspect the existing ledger/raw location.

Consequently an existing vault may still display `= Scope` after updating; this
is compatibility, not a completed data migration. Physically renaming an existing
vault requires a separate reviewed changeset that preserves historical evidence.
Do not globally replace paths in old raw records or approval objects.

Release declaration no longer requires a separate interactive approval. Instance
updates use the content-bound confirmation checkpoint described in
[SETUP](SETUP.md#정본-릴리스와-갱신). General protected-region authority is unchanged.

## Recovering a vault stopped by v3.20.1

A vault that still has `= Scope` stops working after an update to v3.20.1. The
MCP server, hooks, sync daemon, CLI and `osk.update` itself all fail with
`RuntimeError: Legacy Space layout detected`. No data was moved. Because the
vault's own updater cannot start, run the updater from a v3.21.0 checkout
against the vault:

```bash
git clone --depth 1 --branch v3.21.0 https://github.com/lpaiu-cs/osk-system osk-v3.21.0
OSK_VAULT_ROOT=/path/to/vault PYTHONPATH=osk-v3.21.0/_governance/_engine \
  /path/to/vault/.venv/bin/python -m osk.update --to v3.21.0 --apply
```

On Windows, set the two variables with `$env:` and use
`.venv\Scripts\python.exe`. The first run shows the changeset, exits with code
2 and `approval_required: true`. Review it, stop the daemon if it is running,
and run the same command once more. The vault then runs v3.21.0 on its
existing `= Scope` roots. Restart the harness, MCP server and daemon afterwards.

Do not rename the roots by hand to silence the error: ledgers, approval objects
and raw coordinates still refer to the old names.
