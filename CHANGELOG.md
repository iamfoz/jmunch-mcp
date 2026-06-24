# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Gateway verb loop: preserve `reasoning_content` for thinking-mode
  upstreams.** DeepSeek V4 Pro, Kimi `/coding` / Moonshot thinking mode,
  and Xiaomi MiMo thinking all require a non-empty `reasoning_content`
  on every assistant turn replayed back. The synthesized assistant
  message that the verb loop builds for each drill-in round dropped the
  field, so the next round hit the upstream with HTTP 400
  *"The reasoning_content in the thinking mode must be passed back to
  the API."* The OpenAI route's `_verb_loop` now copies
  `reasoning_content` from the upstream's response onto the synthesized
  message, or pads with `" "` when absent (tolerated by validators that
  require the field, harmless on those that ignore it; `""` is *not*
  tolerated by DeepSeek V4 Pro). Anthropic's route already preserved
  thinking blocks naturally — they ride along as content blocks (with
  their `signature`) on the assistant message and are deep-copied.
- **Gateway verb loop no longer destroys conversational context or
  hides the app's tools.** The OpenAI route's `_verb_loop` rebuilt a
  synthetic context from scratch (`_compact_base_messages` /
  `_extract_user_and_handle`) — it discarded all conversation history
  and kept only the *first* user message; deep in a conversation the
  model was handed the opening turn as the entire request, lost the
  thread, and greeted the user instead of answering. It also stripped
  the request's `tools` array down to `jmunch_*` verbs only, so the
  model wrongly concluded its real tools were gone mid-drill-in (cron
  jobs reporting "I do not have access to a terminal/bash tool"). The
  verb loop now continues the real (already handle-ified) conversation —
  every user/assistant/tool turn, in order — with the drill-in guidance
  merged into the system message, and forwards the FULL `tools` array
  (app tools + jmunch verbs) to every iteration. The Anthropic route
  already continued the real conversation; its verb loop now also merges
  the drill-in guidance into the top-level `system` field. Supersedes
  the earlier `fix/verb-loop-400-502`, `fix/preserve-user-role-in-verb-loop`,
  and `fix/preserve-app-tools-in-verb-loop` band-aids.
### Added
- **Gateway: `/v1/models` is now a real upstream passthrough.** Previously a
  Phase 1 stub that always returned `{"object":"list","data":[]}`. The handler
  now resolves an upstream (via `X-Jmunch-Upstream` header or config default),
  GETs `/v1/models` against it, and returns the response verbatim — so clients
  that depend on `context_length` and other catalog metadata (e.g. Hermes'
  endpoint detection and adaptive context window) work correctly. Cached per
  upstream for `gateway.models_cache_ttl_seconds` (default 60s; set to 0 to
  disable). Anthropic-kind upstreams continue to return the empty stub since
  Anthropic's catalog isn't OpenAI-shaped.
- **Gateway: streaming `usage` is now preserved end-to-end.** The
  buffer-then-replay path collapsed every upstream stream into a single
  chunk with `usage: null`, so OpenAI-SDK clients (Hermes, LangChain,
  any caller that opts into `stream_options.include_usage=True`) saw
  zero token totals for every call. `assemble_response_from_chunks` now
  captures `usage` from whichever chunk carries it (spec-compliant
  trailing chunk with `choices: []`, or inline on the final
  content-bearing chunk for non-compliant upstreams) and `encode_as_sse`
  emits a separate trailing usage frame so the wire shape matches the
  OpenAI streaming spec. Clients that didn't request usage see an extra
  empty-choices chunk and ignore it gracefully.
### Added
- **Gateway: `X-Jmunch-Gateway` response header** on every response —
  streaming and non-streaming, both routes, plus `/health`, `/v1/models`
  and error responses. Lets a downstream consumer passively detect that
  jmunch is in the LLM path; the value carries the gateway version.
- **Gateway: `X-Jmunch-Handleify` request header.** `X-Jmunch-Handleify:
  false` (or `0`/`no`) disables request-side handle-ification for that
  one call, so a memory-extraction call sees full-fidelity tool content.
  A per-request config override, parallel to `X-Jmunch-Inject`.

### Changed
- **Gateway: `inject_tools = "auto"` now keys off the handle envelope, not
  the request's `tools` array.** `auto` injects the jmunch verbs (and the
  handle-envelope system instruction) exactly when the forwarded request
  carries a handle envelope — i.e. precisely when the model needs the
  verbs to drill in. Previously `auto` guessed from whether the app
  declared `tools`, which missed a large tool result on a request that
  omitted the `tools` array, and needlessly injected the verbs on
  tool-using turns that had nothing to drill into.

### Fixed
- **Gateway: model no longer mistakes a handle envelope for a
  user-attached file.** A static system instruction explaining handle
  envelopes is now injected into every forwarded request whenever verb
  injection is active — not just during the internal verb loop. Without
  it, a leftover handle envelope in conversation history made the model
  narrate "a file or data payload has been attached" instead of treating
  it as compressed tool output.
### Added
- **Gateway: `JMUNCH_DEBUG_DUMP` upstream-request dump.** With the env
  var set truthy (`1`/`true`/`yes`/`on`), every upstream call writes the
  exact outbound request body — model, full messages array, system
  field, tools, all untruncated (handle envelopes included) — as
  pretty-printed JSON to a timestamped file under `~/.jmunch/debug/`.
  Covers both the OpenAI and Anthropic routes, including verb-loop
  iterations. Default off; no behaviour change when the var is unset.
### Added
- **Context-aware handle-ification.** The gateway now gates request-side
  compression on request size *relative to the model's context window*.
  New `[interception]` keys:
  - `context_fraction` — only handle-ify once the request reaches this
    fraction of the window. `0.0` (default) keeps the previous behaviour
    (compress every request). On a big-context model (Qwen3 262k,
    GPT-4.1 1M) this stops the gateway compressing context that would
    have fit fine — the root cause of agents "forgetting" mid-task.
  - `default_context_window` and `[interception.context_windows]` — the
    window size assumed per model. A built-in prefix table covers common
    GPT / Claude / Qwen / Llama / Gemini / Mistral / DeepSeek models;
    the table teaches the gateway about custom or newly released ones.
- **Recency window.** `[interception] recency_window` leaves the last N
  tool_results in a request verbatim — they are the agent's live working
  set, and compressing them mid-task is the most direct cause of dropped
  context. Older history is still compressed. `0` (default) disables it.
- `tests/gateway/test_context_aware.py` covering the context-window
  table, the fraction gate, the recency window, and config
  loading/validation of the new keys.

### Changed
- Gateway config `Interception` gained `context_fraction`,
  `recency_window`, `default_context_window`, and `context_windows`.
  All default to the previous behaviour, so existing `gateway.toml`
  files are unaffected; `configs/gateway.example.toml` ships the
  recommended values.

### Fixed
- **Savings tracker over-count.** Handle-ification called `envelope()`
  with `response_bytes=0`, so the persistent `SavingsTracker` (and the
  dashboard total it feeds) was credited with `raw_bytes/4` of savings
  on every handle — far more than the handle envelope actually saved.
  `envelope()` now self-measures its serialized size when `response_bytes`
  is omitted and records the true savings exactly once. Affected both the
  MCP proxy and the gateway.
- **Dispatcher crash on malformed verb arguments.** A `tools/call` with a
  bad argument type (e.g. `n: null` → `int(None)`) raised an unhandled
  exception that killed the MCP proxy's pump loop or returned HTTP 500
  from the gateway. `Dispatcher.dispatch` now converts any handler
  exception into a structured `INVALID_ARGS` error.
- Dashboard `/api/calls?limit=` no longer crashes the request handler on
  a non-numeric value; it falls back to 100 and clamps the range.
- **`SavingsTracker` cross-process race.** Multiple proxies sharing one
  `~/.jmunch/_savings.json` could lose increments — each held only an
  in-process lock and wrote its own stale in-memory total. `record()` now
  re-reads the on-disk totals under a cross-process file lock, so
  concurrent processes accumulate correctly.
- **Setup wizard: per-upstream API-key env vars.** The wizard now writes
  each upstream's `api_key_env` to its config as `<NAME>_API_KEY` (e.g.
  `AIROUTER_API_KEY`, `DEEPSEEK_API_KEY`) instead of relying on the
  kind-default `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. Previously two
  upstreams of the same kind silently overwrote one another's key in
  `~/.jmunch/env`. Editing an upstream and changing its name carries
  the existing key forward to the new env var if no new key is entered,
  so renames don't break a working upstream. The CLI `gateway
  add-upstream` does the same. Hand-rolled configs without
  `api_key_env` continue to fall back to the kind default (back-compat).
- **Gateway logging is now split by severity** instead of being lumped
  into stderr by Python's default `basicConfig(stream=sys.stderr)`.
  DEBUG/INFO go to stdout (→ `~/.jmunch/logs/gateway.out.log`), WARNING+
  to stderr (→ `~/.jmunch/logs/gateway.err.log`), so a non-empty
  `gateway.err.log` always signals a real problem and `gateway.out.log`
  carries the routine request-by-request activity it was always meant
  to hold.

### Added
- **`jmunch-mcp gateway setup` — interactive setup wizard (Textual).**
  Walks the user through listen address, one-or-more upstreams (name /
  kind / base URL / masked API key), default upstream, inject-tools mode,
  and threshold; writes `~/.jmunch/gateway.toml` plus `~/.jmunch/env`
  (mode `0600`); optionally calls `install` to register the service.
  The upstream dialog has a **Test** button that probes the upstream's
  `/v1/models` endpoint with the supplied API key — uses OpenAI's
  `Authorization: Bearer` or Anthropic's `x-api-key` header per the
  configured kind, works with local servers (Ollama / LM Studio / vLLM)
  without a key. Add / Edit / Remove buttons manage upstreams in the
  main wizard. Textual ships as a new optional extra `[setup]` (`pipx
  install 'jmunch-mcp[gateway,setup]'` or `pipx inject jmunch-mcp
  textual`). When the extra is absent, the wizard prints a friendly
  install hint and exits — the non-interactive `init` / `install` path
  still works.
- **`gateway add-upstream` (interactive Textual modal) and
  `gateway remove-upstream --name <n>` (non-interactive)** — manage
  upstreams without re-running `setup`. `remove-upstream` auto-updates
  `default_upstream` if you remove the current one, and refuses to leave
  the gateway with zero upstreams.
- **`~/.jmunch/env` — KEY=VALUE secrets file (mode `0600`).** Read by
  `gateway install` and embedded into the rendered unit so the
  launchd / systemd service actually sees the API keys. Hand-editable;
  `setup` / `add-upstream` write to it for you. `install --env-file
  <path>` overrides the location; the file is optional.
- **`jmunch-mcp gateway init` / `install` — scaffold and run the gateway
  as a background service.** `init` writes a commented starter
  `~/.jmunch/gateway.toml`; `install` / `start` / `stop` / `restart` /
  `status` / `uninstall` generate and manage a user-level service —
  launchd on macOS, systemd user unit on Linux. `install` defaults
  `--config` to `~/.jmunch/gateway.toml` and `--env-file` to
  `~/.jmunch/env`. Runs the same interpreter that ran `install`; restarts
  on failure; logs to `~/.jmunch/logs/`; `--label` allows multiple
  instances. Unsupported platforms fall back to the foreground `gateway`
  run with a clear message. The generated unit declares
  `JMUNCH_DEBUG_DUMP=0` in the service environment; `install --debug-dump`
  sets it to `1`.
- `contrib/` — optional, clearly-fenced integration helpers that are not
  part of the core package and not shipped in the wheel. `contrib/hermes-agent/`
  ships a safe gateway update/restart script for Hermes-agent users.
### Changed
- README restructured around the two run modes — **MCP proxy** (for MCP
  clients) vs **Gateway** (for any OpenAI/Anthropic-API app) — so users
  wiring up an HTTP-API app don't set up the MCP proxy by mistake. Adds an
  **Integrations (`contrib/`)** section, including notes for Hermes-agent
  users.

## [0.2.1] — 2026-04-30

### Fixed
- **MCP tool names now use underscores (`jmunch_peek` etc.) instead of dots.**
  The Anthropic API enforces `^[a-zA-Z0-9_-]{1,64}$` on tool names server-side,
  so dotted names produced 400s on every Claude Desktop chat that had
  jmunch-mcp loaded (`FrontendRemoteMcpToolDefinition.name` regex error).
  Claude Code happened to mask this because it namespaces remote MCP tools
  as `mcp__<server>__<tool>` before forwarding; Desktop forwards the raw
  `tools/list` names verbatim. Reported by @denovich (#2).
- The seven dotted names (`jmunch.peek`, `jmunch.slice`, `jmunch.search`,
  `jmunch.aggregate`, `jmunch.summarize`, `jmunch.describe`,
  `jmunch.list_handles`) remain accepted as deprecated aliases in the
  dispatcher for one release so in-flight `tools/call` requests from
  older clients still resolve. They are no longer advertised in
  `tools/list`.

## [0.2.0] — 2026-04-24

### Changed
- **Gateway verb loop: compact-iteration architecture.** Drill-in rounds no
  longer re-transmit the accumulating conversation on every upstream call.
  Each verb iteration now forwards a consolidated system message, the
  handle envelope, a terse prior-verbs trail, and the latest verb call +
  result — jmunch-only tool schemas, non-jmunch tools dropped from
  follow-ups. Eliminates the O(K²) context-growth regression that made
  real-world savings a coin flip.
- Dedicated upstream byte counters (`bytes_sent_upstream`,
  `bytes_received_upstream`, `upstream_calls`) on `OpenAIUpstream`.
  Metrics now records actual POSTed bytes, not app-side request size.
- `scrub_params` option on `[[upstream]]` (drops named params before
  forwarding — e.g., Opus 4.7 rejects `temperature` via OpenAI-compat).
- `stream_options` stripped when the gateway forces `stream=false`
  (Anthropic 400 otherwise).

### Added
- Lock-in test `tests/gateway/test_verb_loop_savings.py`. A 100 KB fat
  tool_result across 4 drill-in verbs must land under 35% of raw request
  bytes and stay per-call flat. Guards against re-regression.
- `bench/nanobot/demo/`: two-terminal side-by-side demo (left.sh /
  right.sh) with its own tiny MCP stdio server, per-side metrics DBs,
  and fresh workspaces so the two sides never interfere.

### Fixed
- Metrics schema split (`_SCHEMA_TABLE` + `_SCHEMA_INDEXES`) so
  `ALTER TABLE` migrations run before `CREATE INDEX` — fixes
  "no such column: surface" on databases created pre-0.1.0.
- Dashboard `totals()` accepts `include_zero_savings=True` — needed to
  surface baseline request counts on the OFF side of the demo.

### Performance
- Synthetic benchmark (100 KB tool_result, 4-verb drill-in, measured by
  actual upstream bytes): pre-refactor ~125% of raw request → post-base-
  refactor ~27% → post-optimizations ~24.5%.

## [0.1.0] — 2026-04-23

### Added
- HTTP gateway frontend (`gateway/`) with `/v1/chat/completions` (streaming +
  non-streaming) and `/v1/messages` routes. Token-savings now apply to any
  OpenAI- or Anthropic-compatible app, not just MCP clients.
- Request-side handle-ification of fat `tool_result` blocks; jmunch verb
  injection into `tools` arrays; verb short-circuit with synthesized
  follow-up turns.
- `PersistentHandleRegistry`: SQLite-backed handle store with TTL sweeper,
  survives restarts. In-memory LRU retained as hot cache.
- `TokenCounter`: tiktoken when available, bytes/4 fallback.
- Metrics: `surface` and `tokens_saved_exact` columns (auto-migrated); read
  helpers accept `?surface=mcp|gateway|all` filter.
- `bench/nanobot/`: automated before/after demo wired to Anthropic's
  OpenAI-compat endpoint.
- `[gateway]` and `[exact-tokens]` optional extras keep the base install
  dep-free.
- 35 new tests in `tests/gateway/`.

### Changed
- README expanded with broader MCP server support description.

### Unchanged
- MCP proxy behavior preserved; jMRI-compliant core (sniffer, registry,
  verbs, envelope) shared between MCP and gateway surfaces.

## [0.0.3] — pre-0.1.0

### Changed
- Dashboard hides zero-savings rows uniformly. Any row with `saved_bytes=0`
  no longer surfaces — covers `jmunch.*` handle ops, below-threshold
  passthroughs, and pure errors. Previous tool-prefix filter replaced with
  a single SQL predicate applied to every read query (totals, per_upstream,
  recent_calls, series).

## [0.0.2] — pre-0.1.0

### Added
- Dashboard documentation.

### Fixed
- Python 3.10 compatibility via `tomli` fallback.

## [0.0.1] — initial release

### Added
- Handle-ifying MCP proxy with content-aware backends (JSON, tabular, text).
- Local verbs: `peek`, `slice`, `search`, `summarize`, `aggregate`, `describe`.
- CLI `init` with server discovery and client-config rewrite.
- SQLite metrics store.
- Browser dashboard.

[0.2.1]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.1.0
[0.0.3]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.3
[0.0.2]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.2
[0.0.1]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.1
