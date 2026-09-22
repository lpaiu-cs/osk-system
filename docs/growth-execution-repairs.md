# Growth execution repairs — 2026-09-22

The September 19–22 instance audit found storage ingress, repair budgeting,
completion recording, oversized knowledge nodes and aged evictions still open.
These changes do not update the running instance.

## Raw storage at the Git boundary

The current dialogue capture filter cannot control an older process on another
machine. Sync now checks changed raw records before local commit and remote
rebase, and checks outgoing commits before push. Visible Markdown raw and legacy
Codex capture codecs stop sync with filenames and a repair instruction. Rejected
content is neither printed nor silently rewritten. Validators also report
existing storage violations, including records unchanged by the current sync.

Update the actual writer on every participating machine and explicitly repair
its records before resuming sync. This guard cannot stop an old, unupdated daemon
or arbitrary manual Git pushes; receiving instances must also run the new guard.
Historical raw coordinates remain readable. Existing legacy bytes are not
automatically sanitized because their hashes may bind completed reviews.

Validation uses disposable local, peer and bare Git repositories: inbound,
uncommitted outbound and already-committed outbound legacy raw are rejected;
hidden dialogue storage syncs; rejected bytes remain intact.
Incoming checks compare the frozen fork point with the fetched commit. A local
migration, including one committed while offline, can therefore propagate even
while the remote still has the old raw path.

## Bounded repair and immediate decisions

Repair reviews use the same `max_rounds` bound as initial review. Oversized
repair obligations split into independently acknowledged snapshots. The parent
is complete only while every part's current receipt validates; newer conversation
cursors are not rewound. A failed part can be repaired again without reading all
the other parts.

`growth checkpoint --file <UTF-8 JSON>` accepts the existing `osk_reviews` packet
for a recorded manifest, including a single job and empty other queues. Workers
must checkpoint after each decision before starting the next job. The existing
receipt checks run at that point. A later timeout leaves the whole run incomplete
but keeps already verified job decisions. Final provider JSON remains the fallback
when shell access is unavailable; it is never extracted from tool output.

## Knowledge size and organization

Writes and organization inventories flag bodies over 12,000 characters, or hub
bodies over 6,000. These are inspection hints, not rejection limits or evidence
that content should be discarded. Review starts with outlines and selected
sections, covers at most three node claims per job, and records the next concrete
target when deferred. Hubs carry navigation; durable claims keep their conditions,
corrections and sources; repository execution diaries belong in repository docs.
The next organization job carries the last deferred reason and inspected
snapshot, including whether the files have changed since that decision. This
resume context is also preserved in the growth manifest and worker prompt.

## Aged evictions

Evictions older than the existing 14-day threshold join the daily growth queues,
even when their original scope has no new sessions. They share the existing total
job budget and fair queue rotation. Oldest unattempted records go first; attempted
records rotate so one blocked item cannot starve every other scope. Young entries
retain the existing SessionStart path.

Each selected eviction needs a content-based decision. Preservation uses MCP
node writes and an existing readable target. The checkpoint records the reason;
deferred items stay pending. A settlement is structural evidence and does not
prove that the semantic judgment was correct.
For items selected by a growth run, a settlement alone is not review completion:
an explicit decision must name that receipt and the selected source snapshot.
Interrupted writes and later deferrals return to the growth queue even when the
eviction ledger already marks the item settled. Historical settlements never
selected for growth are not reopened merely for lacking this new review record.
