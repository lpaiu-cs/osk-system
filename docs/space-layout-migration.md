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
