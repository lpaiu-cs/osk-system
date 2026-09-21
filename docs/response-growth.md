# Response-triggered growth: implementation and experiment status

The runner and approved Mechanism §9-3 amendment shipped in v3.17.0.
Automatic fork configuration remains an explicit per-instance choice.

The current change adds a same-harness fork runner to the existing growth supervisor.
It preserves the source model, Codex reasoning effort, source working directory and
permission policy. It requires a matching native CLI version and confirmed subscription
authentication before inference. Codex uses `exec fork --ephemeral`; Claude uses
`--resume --fork-session --no-session-persistence`. Maintenance processes retain the
existing `OSK_GROWTH_WORKER=1` exclusion from raw capture.

Codex Desktop sources also retain the app's code-mode host and bundled app-tool
definitions. Native `source=vscode` plus `originator=Codex Desktop` selects this path;
CLI sources retain their existing configuration. The source model is never replaced.

Each fork is limited to its original conversation's frozen review snapshot. The daily
Domain queue remains separate. The existing 600-second process-tree deadline, final
packet parser and stored body/source/hub receipt checks are reused. A failed attempt
does not advance the integration review cursor. The response counter and the review
cursor are deliberately separate.

## Execution boundary

Successful final answers count once per native turn/message ID. On the ninth Stop,
the hook detaches a supervisor and returns. The supervisor waits up to five seconds
for the final native record, captures completed dialogue, and selects at most nine
unreviewed rounds from that conversation. Tool requests, intermediate questions,
failed/aborted turns, duplicate notifications and Claude sidechains do not count.
SessionStart/UserPromptSubmit establish the initial baseline; input adds zero to the
Stop counter. Its separate input counter always advances, including in background mode,
so losing CLI access cannot restart the fallback's overdue work at zero. Resume retains
both counters. Enabling the feature does not replay all old answers.

Start/input hooks use local authentication/version queries, with no inference. Missing
configuration, a missing CLI, logout, uncertain subscription auth, version mismatch or
invalid settings route to in-session UserPromptSubmit 9/15 review with a warning. Codex
also requires a Git worktree and a representable native permission policy. Workspace
write roots, network access and both temporary-directory exclusion flags are explicitly
preserved. Granular approval policies use TOML inline tables, not JSON objects. Unknown
restrictions refuse the fork at routing and again before inference. A switch
after nine unreviewed inputs surfaces the pending review immediately. Login recovery
restores Stop scheduling without resetting review state. Authentication is checked again
just before inference; failure there preserves the due Stop attempt for a later retry.
`response_growth_route` exposes the selected mode and reason in integration status.

The existing supervisor receives one Scope job including recovery instructions. It does
not select another conversation or a Domain batch. Busy execution defers work; native
IDs let a later Stop catch up, and the daily run still sees unreviewed raw. A source
change before launch refuses the fork, preserving the pending review. Source movement
during execution also makes cumulative cache accounting unconfirmed. A harness upgrade
can require updating the configured native CLI path.

### Codex paginated history

Desktop can resume the same conversation in a new rollout file whose `history_base`
names an ancestor and an exclusive byte offset. Capture and Stop counting follow
only that declared chain, checking the conversation identity on every page. They do
not combine nearby files or other tasks. Missing/ambiguous ancestors, cycles, foreign
identities and offsets inside a JSONL record remain errors; existing raw is retained.

Previously captured raw coordinates, hashes and review receipts keep their append
order. Newly recovered historical rounds are appended for review, not marked done.
Recovering a prefix before an existing Stop baseline adds no retroactive Stops. A
completed native compaction with no user input or assistant trace is maintenance,
not a missing dialogue round. User turns without a response still remain pending.

## Cache experiments (2026-09-21)

Successful probes used verified ChatGPT or Claude Max subscription CLI authentication. Probe
prompts requested one fixed answer and prohibited tools and writes. These are transport
experiments, not evidence of autonomous distillation or downstream reuse.

| Source and child | Child input | Cached child input | Fraction |
| --- | ---: | ---: | ---: |
| Sol CLI → same-model CLI fork | 25,261 | 24,960 | 98.81% |
| Astra CLI → same-model CLI fork | 26,119 | 25,856 | 98.99% |
| Existing Codex app → CLI fork | 185,881 | 0 | 0% |
| Existing app → local app-server fork, inherited permissions | 222,087 | 0 | 0% |
| Existing app → app-server fork, observed app host/tool flags matched | 125,162 | 0 | 0% |
| Claude Max Opus 5 CLI → same-model CLI fork | 38,685 | 38,558 | 99.67% |

The last two probes used `thread/tokenUsage/updated.tokenUsage.last`, so inherited
parent totals were not counted as worker usage. CLI fork `turn.completed.usage` is
cumulative: the frozen parent baseline was subtracted for the first two rows.
The live app probes occurred during an active turn; they do not isolate every cause of
the cache miss. Matching the model/version and the observed host flags did not establish
app-to-worker cache reuse. Further large, unconditioned retries are not justified.
These active-turn results are superseded for the tested Desktop Sol path by the
post-Stop control below. CLI→CLI evidence alone does not prove Desktop→CLI reuse.
Codex used 0.155.0-alpha.9.2; Claude used 2.1.251.

### Actual Desktop post-Stop control

One temporary task was forked through the Codex app and asked to return a fixed marker
without tools or writes. Its actual model was `gpt-5.6-sol`. After its native final Stop,
three ephemeral CLI workers used that same unchanged source and actual model:

The [measurement record](response-growth-cache-evidence.json) contains the counters
and protocol without private dialogue or native conversation identifiers.

| Worker configuration | Child input | Cached child input | Fraction |
| --- | ---: | ---: | ---: |
| App host/tool definitions preserved | 210,515 | 207,872 | 98.74% |
| Default CLI configuration, same stopped parent | 202,376 | 0 | 0% |
| Final implementation, automatic Desktop detection | 210,515 | 210,304 | 99.90% |

The first matched fork establishes reuse after an actual Desktop Stop. The higher
repeat result can also reuse the earlier worker's identical fixed prompt; it is not
an independent parent-reuse measurement. Each worker emitted one completed answer,
used no tools, and retained the frozen parent usage baseline. Parent cumulative usage
was subtracted, and the source remained unchanged throughout each worker.

A local request-rendering check, rejected before provider inference, confirmed that
default CLI and app-host forks send different tool definitions at the start of the
request. The runner now preserves `features.code_mode_host` and the bundled
`codex_app` MCP tool definitions for Desktop sources. Waiting for Stop alone does not
repair that prefix difference: the default control still missed while the source was
warm. The historical active-turn misses additionally contained incomplete-history
warnings; these experiments do not attribute all earlier misses to one cause.

The temporary Desktop task was archived. Worker forks were ephemeral. This validates
the tested Desktop Sol transport, not all models/versions, Claude Desktop, autonomous
growth, or a cache lifetime guarantee. No automatic instance activation was performed.

Ephemeral child metadata reported no persistent path, and checks found no corresponding
rollout files. The sampled app task listing contained no experiment children. The listing
was bounded; that is not an exhaustive UI audit. Claude's fork produced a new child ID
but no native child transcript was found.

The initial Claude probe failed with `Credit balance is too low` and zero tokens.
No further inference ran until CLI auth reported `authMethod=claude.ai` and
`subscriptionType=max`. The successful parent wrote 38,558 tokens with
`ephemeral_1h_input_tokens`; the child read those tokens and wrote 125 new one-hour
cache tokens, plus two uncached input tokens. CLI `costUSD`/`costBasis=list` is a list-price
estimate, not evidence of API charges. Desktop browser login alone does not verify a
separate CLI's subscription. Never extract Desktop credentials or fall back to API billing.

## Cache lifetime and limits

Current [OpenAI prompt caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching)
states that GPT-5.6 and later use a default/minimum 30-minute lifetime after the most
recent write or reuse. Older models' in-memory caches typically last 5–10 idle minutes,
up to an hour. This API documentation is not a measured subscription-app TTL guarantee.

[Claude's documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
describes a default five-minute cache and optional one-hour cache. Lifetime starts at
request start, so generation time consumes it. Our Max CLI experiment actually reported
one-hour writes; we did not run a timed expiry test. Reuse refreshes lifetime.

Stop-triggering avoids waiting for the next user input but cannot guarantee a hit.
Model, rendered system/tool/message prefix, thinking settings and execution environment
must agree. A fork inherits conversation context even though extra raw reads are bounded.
A miss can therefore process the full inherited context. This is not a nine-round total
token cap. Whole-worker cache usage also includes self-reuse during its own tool loop;
it alone does not prove first-request reuse of the parent.

## Validation

- Final fixed-revision formal runner: 1,570 passed, 0 failed, 4 Windows permission-mode skips.
- New focused checks: twelve passed (native completion identity, subscription/version
  refusal, cumulative usage accounting, durable failure state, one-conversation
  supervisor/receipt integration, ninth-Stop counting, detached native-flush handling,
  9/15 fallback and login recovery, authentication loss after the input check, TOML
  permission roundtrips, real Git-boundary fallback, and Desktop tool-prefix selection).
- A real hook subprocess returns before the native final record is appended; the detached
  helper then observes that record without calling a provider. Input and duplicate Stop
  events add zero to the Stop counter. Resume retains both clocks. A busy worker or
  unavailable CLI does not consume a retry. Fallback retains raw references and never ACKs
  a review merely because routing changed.
- The focused checks are included in `tests/test_regression.py` for subsequent runs.
- Real Claude CLI auth checks passed with the existing Max login, refused a separate empty
  config directory, then passed again with the original login. This made zero model calls
  and did not log the user out. The routing/counter transition tests use isolated mini-vaults.
- The installed Codex config parser accepted the generated granular approval table and
  preserved both temporary-directory exclusions, network access and normalized write roots.
  This used `config/read` with no model call or configuration mutation.
- No live engine update, automatic fork configuration, shared daemon restart, or release
  has been made. The governance wording was explicitly approved before amendment.

References: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[Claude authentication](https://code.claude.com/docs/en/authentication).
