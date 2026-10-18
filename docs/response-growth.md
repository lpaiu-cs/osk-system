# Response-triggered growth: implementation and experiment status

The runner and approved Mechanism §9-3 amendment shipped in v3.17.0.
Automatic fork configuration remains an explicit per-instance choice.

The current change adds a same-harness fork runner to the existing growth supervisor.
It preserves the source model, Codex reasoning effort, source working directory, tool
definitions and approval policy or permission mode, and narrows what the tools may do
(see [Fork tool restriction](#fork-tool-restriction-85)). It requires a matching native
CLI version and confirmed subscription authentication before inference. Codex uses `exec fork --ephemeral`; Claude uses
`--resume --fork-session --no-session-persistence`. Maintenance processes retain the
existing `OSK_GROWTH_WORKER=1` exclusion from conversation tracking.

Codex Desktop sources (native `source=vscode` plus `originator=Codex Desktop`) do not fork:
see [Codex Desktop sources no longer fork](#codex-desktop-sources-no-longer-fork-2026-10).
The source model is never replaced.

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

Start/input hooks never wait on the CLI: a detached worker they start runs the local
authentication/version queries (at most every 10 minutes, 30 s per query), with no
inference, and the hooks read its last verdict — the fork route until it first answers.
Under load `auth status` alone took over 8 s and the input hook hit its 10 s limit (#192).
No hook reads the transcript: the start, input and in-session Stop hooks hand its capture
to the same worker when its size or time changed (a forking Stop's supervisor captures it).
SessionStart only pins the conversation's landing and shows the waits as of the last capture. Missing
configuration, a missing CLI, logout, uncertain subscription auth, version mismatch or
invalid settings route to in-session UserPromptSubmit 9/15 review with a warning. A host
turned off (`false` in `.osk/response-growth.json`) and a Codex Desktop source take the
`off` route instead: no fork and no in-session review. Codex
also requires a Git worktree or a project trusted in Codex `config.toml` under Codex's
normalized key, no active profile that sets a key the fork pins (provider, base URL,
effort, approval or sandbox), no `model_providers.openai` or `chatgpt_base_url` other than
the first-party one in any config layer (system, user, or project `.codex/` from the
project root down to cwd), a source model outside the `chatgpt-web/` bridge route, and a
preservable native approval policy once a final answer recorded it. The fork pins
`openai_base_url` to `https://chatgpt.com/backend-api/codex` and drops `OPENAI_BASE_URL`,
so a top-level local bridge or proxy is bypassed; each run folder keeps its `argv.json`.
Granular approval policies use TOML inline tables, not JSON objects. An unknown approval
policy refuses the fork at routing and again before inference; the source's sandbox policy
never does (the fork is read-only, see below). A Claude fork runs
with the source conversation's permission mode (`permissionMode` in force at its last
final answer) passed verbatim as `--permission-mode`, with no extra bypass flag; a missing
or unknown mode, or one that changes before launch, refuses the fork. A switch after the
prompt writes no transcript row, so a Stop whose `permission_mode` differs also refuses. A switch
after nine unreviewed inputs surfaces the pending review immediately. Login recovery
restores Stop scheduling without resetting review state. Authentication is checked again
just before inference; failure there preserves the due Stop attempt for a later retry.
`response_growth_route` exposes the selected mode and reason in integration status.

The existing supervisor receives one Scope job including recovery instructions. It does
not select another conversation or a Domain batch. Busy execution defers work; native
IDs let a later Stop catch up. The daily run takes original turns only after the conversation has ended, reading just its unreviewed turns from the transcript. A new turn
or changed source metadata before launch refuses the fork without spending the Stop
attempt, preserving the pending review; native trailer rows written after Stop do not. Source movement
during execution also makes cumulative cache accounting unconfirmed. A harness upgrade
can require updating the configured native CLI path.

### Fork tool restriction (#85)

A fork reads the source conversation's untrusted text with the source's credentials. In
2026-10 measurements (63 forks) no fork used a tool outside the osk MCP, so the restriction
costs no function. It never changes the tool definitions or the Claude permission mode the
source used: either would turn the cached parent prefix (median about 450k tokens) into a
cache write on every fork.

- **Claude.** The `--settings` the fork already passes adds a PreToolUse hook that denies
  every tool call except `mcp__osk-system__*` and `ToolSearch` (which loads the deferred
  osk tools). The deny holds in every permission mode, `bypassPermissions` included; the
  model reads its reason and continues with the osk tools. `--tools`/`--strict-mcp-config`
  and a switch to `--permission-mode default` with `--allowedTools` were rejected: the first
  changes the tool definitions, the second may change the system prompt.
- **Codex.** The same deny rides in as a `-c hooks=…` PreToolUse hook. Codex runs it before
  every shell command, `apply_patch`, MCP tool and app connector (`mcp__codex_apps__*`), and
  spares only `mcp__osk_system__*` (Codex turns every character outside `[A-Za-z0-9_]` in a
  server name into `_`). Its `tool_search` never reaches the hook, so it needs no exception.
  Three details keep the hook from being skipped silently:
  - The matcher is a Rust regex, which has no lookahead, so it spells out every name that
    leaves the osk prefix.
  - The override carries the hook's own trust, `hooks.state.<session-flags key>.trusted_hash`.
    Codex hashes the normalized hook. `--dangerously-bypass-hook-trust` would also run untrusted
    project hooks in the source's folder.
  - `features.hooks=true` overrides a user who turned hooks off. An active profile that sets
    `features` still wins, and so does a managed `allow_managed_hooks_only`.
  The fork also runs with `sandbox_mode="read-only"` whatever the source's sandbox: if the hook
  were ever skipped, shell commands still could not write or reach the network. Tool
  definitions and the approval policy stay the source's. No source sandbox refuses the fork,
  since none is narrower.
  Per-server `approval_mode` was rejected: it denies only the servers it names. Any plugin
  server or app connector it misses stays open.

The daily growth workers start fresh, with no parent prefix to keep, and restrict their own
tools (Claude `--tools ""` with `--strict-mcp-config`; Codex `--sandbox read-only` in the
command [SETUP.md](SETUP.md) gives).

### 로컬 브리지·프록시

Tools such as codex-chatgpt-web write a top-level `openai_base_url` (for example
`http://127.0.0.1:17841/v1`) into Codex `config.toml`. The bridge's safety cannot be
verified from osk. The fork therefore bypasses it instead of trusting it: `-c openai_base_url`
pins the Codex backend and `OPENAI_BASE_URL` is dropped. Account, model and subscription
limits stay the source's. Settings that win over `-c` remain refusals, in every config
layer: any key the fork pins that an active profile sets, `model_providers.openai.base_url`
unless it names the backend, and a `chatgpt_base_url` other than `https://chatgpt.com/backend-api/`.
A `chatgpt-web/*` source model runs through the bridge's ChatGPT Web route, not the Codex
backend. It is refused by name at routing, before any native query. `osk fork doctor` shows these
facts and the routing verdict without writing state or settings. It does not install or remove a bridge.

### Codex paginated history

Desktop can resume the same conversation in a new rollout file whose `history_base`
names an ancestor and an exclusive byte offset. Capture and Stop counting follow
only that declared chain, checking the conversation identity on every page. They do
not combine nearby files or other tasks. Missing/ambiguous ancestors, cycles, foreign
identities and offsets inside a JSONL record remain errors; existing raw is retained.

Previously tracked turns, stored raw coordinates, hashes and review receipts keep their
order. A historical turn recognized later follows them for review and is not marked done.
Recovering a prefix before an existing Stop baseline adds no retroactive Stops. A
legacy empty baseline excludes ancestor completions once; a baseline that already
observed the full history continues counting subsequent completions normally. A
completed native compaction with no user input or assistant trace is maintenance,
not a missing dialogue round. User turns without a response still remain pending.
Native lookup errors remain visible, but rounds an older engine stored in raw remain in
both the review inventory and the scheduled catch-up queue.

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

Superseded for Codex 0.162 and later: see
[Codex Desktop sources no longer fork](#codex-desktop-sources-no-longer-fork-2026-10).

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

## Codex Desktop sources no longer fork (2026-10)

From 2026-10-07 to 10-17 all 12 Codex Desktop forks recomputed the parent context on their first
request. Across them, uncached input ≈ 1414·requests + 1.20·parent context − 7986 (least
squares, n=12). A fork that continued the parent's cache would have a parent coefficient near 0.
Misses were 2.52M of the 2.93M uncached input tokens.

Codex 0.162 sends each request's tool definitions as the first input item (`additional_tools`),
before the base instructions and the history. A fork's first request was captured at a local
endpoint and rejected before inference. Both `exec fork` and an `app-server` fork with the
Desktop's flags were captured with Codex 0.163.0-alpha.10.2:

- The Desktop serves its `codex_app` tools to its app-server over a pipe
  (`CODEX_APP_TOOLS_PIPE_PATH`). They are never deferred, so they sit in that first item. Outside
  the Desktop the server fails to start: "Codex did not provide CODEX_APP_TOOLS_PIPE_PATH". The
  parent's code-mode calls use `mcp__codex_app__*`; no fork request defines them.
- The Desktop also sets features at runtime. Codex's local log recorded 173 feature values for the
  parent's last turn; 18 differ from the CLI's. `tool_suggest` is on in the CLI and off in the
  Desktop. It alone adds `request_plugin_install` to the fork's first item.
- Neither the model catalog (`multi_agent_version` v1 through the local bridge, v2 direct) nor
  `features.prefer_mxc` changed the fork's first item. `prompt_cache_key` is the parent's thread ID.

Deferred tools (`multi_agent_v1`, MCP servers) are listed in the history's world state and replay
unchanged. Copying all 173 features removed `request_plugin_install`, but the app tools stay with
the running Desktop. The September control above predates this request layout.

A Desktop source therefore takes the `off` route: no fork and no in-session review. The scheduled
run integrates the conversation after it ends (Mechanism §9-4 3항). This holds without a
configured CLI too. Only the hooks' worker reads the transcript, which appears after the first turn.
It asks again as soon as the transcript exists, and its Desktop verdict outranks the missing CLI. An
instance can also turn a host off with `false` in `.osk/response-growth.json`. Codex CLI sources
keep forking.

A Codex CLI source was measured on 2026-10-18 with `gpt-6-astra` and Codex 0.163.0-alpha.10.2.
Its ephemeral fork read 23,424 of 23,602 input tokens from cache on its only request (0.99).
Codex gives only an ephemeral root fork the parent's cache key: `prompt_cache_key` and the
`session-id` header (openai/codex#44862). A persistent fork sends its own thread ID. Two persistent
forks of the same parent read 0 and 12,416 of about 23,600 tokens (0.00 and 0.53). Resuming the
parent read 0.99. The fork's first two items carry IDs derived from its own thread ID
(openai/codex#40962), yet the ephemeral fork hit, so those IDs do not split the cache. Forks
therefore stay ephemeral and leave no rollout.

An ephemeral fork reports only whole-turn usage. Its first request holds at least the parent's last
request input (`context`). It misses at most the whole turn's uncached input. Each run's
`cache.first_request_floor` (1 − uncached ÷ context) is therefore a lower bound on the first
request's hit rate. Below 0.9 it cannot confirm a hit.

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

- 2026-10-18 (Desktop `off` route, `false` switch, first-request floor): formal runner 2,254 passed,
  0 failed, 7 Windows permission/fsync skips. `test_response_growth` passed 45. The Desktop checks
  now cover the `off` route instead of tool-prefix selection. The real CLI fork measurement is in
  [Codex Desktop sources no longer fork](#codex-desktop-sources-no-longer-fork-2026-10).
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
- At validation time (2026-09-21, before the v3.17.0 release), no live engine update,
  automatic fork configuration, shared daemon restart, or release had been made. The
  governance wording was explicitly approved before amendment.

References: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[Claude authentication](https://code.claude.com/docs/en/authentication).
