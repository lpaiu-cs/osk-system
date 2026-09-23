# Space paths in v3.20.1

New distributions use `Scope/`, `Domain/`, and `Person/` without the `= ` prefix.
Engine paths, publishing skeletons, setup instructions, and tests use these names.

Existing vaults are not automatically migrated. Do not apply this release to a legacy vault until a reviewed migration is ready: directory renames alone are insufficient because ledger records, approval snapshots, stored paths, and links can contain the old names and content hashes. Back up the vault and retain v3.20.0 until then. The new engine refuses to start when legacy Space roots exist, rather than silently reading an empty new ledger. The updater protects both old and new roots.

The repository owner explicitly authorized this hotfix and a one-time noninteractive release in the current conversation. This exception does not change the normal interactive release requirement.
