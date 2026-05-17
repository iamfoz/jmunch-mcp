# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
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

[0.2.0]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.1.0
[0.0.3]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.3
[0.0.2]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.2
[0.0.1]: https://github.com/jgravelle/jmunch-mcp/releases/tag/v0.0.1
