# jmunch-mcp

Transparent token-saving proxy for LLM tool calls. It handle-ifies fat tool responses into compact, queryable handles — the model sees a small summary and drills in with a few universal verbs (`peek`, `slice`, `search`, `aggregate`, `describe`) instead of paying for the whole payload. It runs in **two modes**: an **MCP proxy** for MCP clients, and a **gateway** for any OpenAI-/Anthropic-API app.

## Two ways to run jmunch-mcp

jmunch-mcp has **two independent modes**. They share the same token-saving core but are wired up completely differently — pick the one that matches how your tool reaches models, and ignore the other. You do not need both.

### Mode 1 — MCP proxy

For **MCP clients**: Claude Desktop, Claude Code, Cursor, Windsurf, Continue. It wraps a single upstream **MCP server** over stdio, forwards every call, and handle-ifies fat responses on the way back. Set up with `jmunch-mcp init` — see [MCP proxy setup](#mcp-proxy-setup).

### Mode 2 — Gateway (universal proxy)

For **any app that speaks the OpenAI or Anthropic HTTP API**: LangChain, LlamaIndex, CrewAI, AutoGen, Aider, Cline, agent runtimes (the Hermes agent included), or a raw SDK. It runs as a local HTTP service — point the app's `base_url` at it, with no changes to the app's code.

What the gateway gives you:

- **Handle-ification + drill-in verbs** — fat `tool_result` payloads become compact handles; the `jmunch_*` verbs are injected and resolved locally, so drilling in costs zero upstream tokens.
- **Context-aware compression** — compresses only when a request is large relative to the model's context window, and never the most recent tool_results (the agent's live working set).
- **Background service** — `jmunch-mcp gateway install` runs it under launchd (macOS) or systemd (Linux), restarting on failure.
- **Per-upstream default-model fallback**, a real **`/v1/models`** passthrough, a self-identifying **`X-Jmunch-Gateway`** response header, per-request **`X-Jmunch-Handleify`** / **`X-Jmunch-Inject`** controls, and an opt-in **`JMUNCH_DEBUG_DUMP`** that records exact upstream requests for debugging.

Set up with `jmunch-mcp gateway` — see [Gateway (universal proxy)](#gateway-universal-proxy).

> **Picking the wrong mode is the most common setup mistake.** If your app calls an LLM over HTTP, you want the **Gateway** — the MCP proxy will not help it. Rule of thumb: *MCP client → Mode 1; everything else → Mode 2.*

Both modes feed the same [dashboard](#dashboard).

## Benchmarks

Measured end-to-end against two popular real-world MCP servers. Each run fires a fixed script of tool calls twice — once direct, once through jmunch-mcp — with three follow-up `jmunch.*` verb calls on the proxied side to model an agent drilling into a large result rather than slurping it whole.

| suite | upstream | direct tokens | via jmunch-mcp | saved |
|---|---|---:|---:|---:|
| GitHub (`facebook/react` issues/PRs/commits) | `@modelcontextprotocol/server-github` | 379,878 | 44,328 | **335,550 (88.3%)** |
| Firecrawl (Wikipedia scrapes + site map + search) | `firecrawl-mcp` | 259,574 | 2,928 | **256,646 (98.9%)** |

Wall-clock time was also faster with the proxy on both suites, despite the extra verb calls — the agent never has to page through the fat payload:

| suite | direct | via jmunch-mcp | delta |
|---|---:|---:|---:|
| GitHub    |  8.4s |  6.8s | **−1.6s (−19.0%)** |
| Firecrawl | 16.4s |  9.2s | **−7.2s (−43.9%)** |

Tabular content (GitHub) routes to the SQLite backend and answers `peek`/`slice`/`aggregate`; JSON content (Firecrawl scrape/map) routes to the JSON-tree backend and answers `peek`/`slice` (JSONPath)/`search`. See [bench/README.md](bench/README.md) to reproduce.

## Install

```bash
pip install jmunch-mcp
```

To install it as a standalone command-line tool — recommended on macOS, where the system Python is locked down — use [pipx](https://pipx.pypa.io). It puts `jmunch-mcp` on your PATH in its own isolated environment, built with a modern Python:

```bash
brew install pipx && pipx ensurepath
pipx install 'jmunch-mcp[gateway,setup]'   # gateway runtime + interactive setup wizard
```

The `[setup]` extra adds [Textual](https://textual.textualize.io/) for `jmunch-mcp gateway setup`. Leave it off if you'd rather edit `~/.jmunch/gateway.toml` by hand — `gateway init` still scaffolds a minimal config you can fill in.

From source:

```bash
git clone https://github.com/jgravelle/jmunch-mcp
cd jmunch-mcp
pip install -e .
```

## MCP proxy setup

```bash
jmunch-mcp init
```

`init` scans three sources — your MCP client configs (Claude Desktop, Claude Code, Cursor, Windsurf, Continue), running processes, and a small catalog of popular upstreams (GitHub, Firecrawl, filesystem, fetch, Brave Search, Slack) — and renders a checklist. Tick the upstreams you want wrapped, and it writes one `<name>.toml` per selection into `./configs/`. Non-interactive flags: `--yes` (pick everything already registered in a client), `--dry-run`, `--overwrite`, `--out <dir>`, `--no-running`, `--no-catalog`.

### Manual

```bash
jmunch-mcp --config examples/config.toml
```

Configure your MCP client to launch `jmunch-mcp --config <path>` instead of the upstream server directly. Add `--report` to print a session summary on shutdown.

## Gateway (universal proxy)

The MCP proxy above saves tokens for MCP clients. The **gateway** saves tokens for *any* AI application that speaks the OpenAI or Anthropic HTTP API — LangChain, LlamaIndex, CrewAI, AutoGen, Continue, Cline, Aider, or a raw SDK. No code changes in the app; just point `base_url` at jmunch.

```bash
pip install 'jmunch-mcp[gateway]'
jmunch-mcp gateway --config configs/gateway.example.toml
# listening on http://127.0.0.1:7879
```

Point your app:

```bash
# OpenAI SDK, LangChain, Aider, Continue, Cline, Ollama-compat apps:
export OPENAI_API_BASE=http://127.0.0.1:7879/v1

# Native Anthropic SDK / Claude Code:
export ANTHROPIC_BASE_URL=http://127.0.0.1:7879
```

What it does, transparently:

- **Handle-ifies fat tool_results** in outgoing requests — your app's tool returns 100KB of JSON, the model sees a 1KB summary + opaque handle.
- **Context-aware compression** — handle-ification is gated on the request size *relative to the model's context window*. On a big-context model (Qwen3 262k, GPT-4.1 1M) a request that comfortably fits is forwarded verbatim; compression only kicks in once the request approaches a configurable fraction of the window. The most recent N tool_results are always left verbatim — they are the agent's live working set.
- **Injects jmunch verbs** (`peek`, `slice`, `search`, `aggregate`, `describe`, `summarize`, `list_handles`) into the request's `tools` array so the model can drill in.
- **Short-circuits verb calls** — when the model calls `jmunch_peek`, the gateway resolves it locally against the handle registry and synthesizes the follow-up turn. The app never sees jmunch tool_calls; those completions cost zero upstream tokens.
- **Persists handles** to `~/.jmunch/handles.db` with a configurable TTL so they survive restarts and cross-session reads.
- **Streams both ways** — OpenAI SSE and Anthropic event streams are buffer-then-replayed with correct verb resolution.

### Context-aware compression

Handle-ification is built for chatty servers and small context windows. On a large-context model it can compress context that would have fit fine, and that lossy compression shows up as the agent "forgetting" mid-task. Three `[interception]` knobs scope it down:

- `context_fraction` — only handle-ify a request once its estimated token count reaches this fraction of the model's context window. `0.0` (default) disables the gate and compresses every request — the previous behavior. `0.5` starts compressing at half the window.
- `recency_window` — never handle-ify the last N tool_results in a request. They are the agent's active working set; compressing them mid-task drops context it needs *now*. Older tool_results are still compressed. `0` (default) disables it.
- `default_context_window` / `[interception.context_windows]` — the window size assumed per model. Common models (GPT, Claude, Qwen, Llama, Gemini, Mistral, DeepSeek) are recognized by name; the `context_windows` table teaches the gateway about custom or newly released models.

See [`configs/gateway.example.toml`](configs/gateway.example.toml) for a worked example.

Per-request controls via headers:

- `X-Jmunch-Upstream: <name>` — override the configured upstream.
- `X-Jmunch-Inject: false` — disable verb tool injection for this call (pure pass-through + request-side handle-ify only).

Metrics flow into the same dashboard as the MCP proxy. Filter with `?surface=gateway` or `?surface=mcp` on `/api/stats` and `/api/calls`.

### Gateway setup — quick start

The interactive way (needs the `[setup]` extra for the Textual TUI):

```bash
jmunch-mcp gateway setup
```

That walks you through: a listen address, one-or-more upstreams (name / kind / base URL / API key — entered in a masked field), the default upstream, the inject-tools mode, and the threshold. The upstream dialog has a **Test** button that probes `/v1/models` on the upstream with the API key — handy for catching typos before you save. It writes `~/.jmunch/gateway.toml` and `~/.jmunch/env` (the latter holds your API keys, mode `0600`), then offers to install the launchd / systemd service. After that you have a running gateway and you can carry on.

Manage upstreams later:

```bash
jmunch-mcp gateway add-upstream                       # interactive: one upstream + its API key
jmunch-mcp gateway remove-upstream --name openai
```

Manage the service:

```bash
jmunch-mcp gateway status
jmunch-mcp gateway restart      # bounce the service (e.g. after editing gateway.toml)
jmunch-mcp gateway install      # re-render the unit; needed after editing ~/.jmunch/env
jmunch-mcp gateway uninstall
```

#### Files the gateway uses

All under `~/.jmunch/`:

| path | what |
|---|---|
| `gateway.toml` | Config (upstreams, interception, listen). Hand-editable. |
| `env` | API keys, `KEY=VALUE` per line (mode `0600`). The wizard stores each upstream's key under its own variable named after the upstream (`<NAME>_API_KEY`, e.g. `AIROUTER_API_KEY`), so two upstreams of the same kind don't collide on one shared key. `gateway install` reads this and embeds the values into the service environment, so the running gateway actually sees them. |
| `logs/gateway.out.log` | Routine activity (DEBUG/INFO — requests, lifecycle). |
| `logs/gateway.err.log` | Warnings and errors only. A non-empty file always means a real problem. |
| `handles.db` | Handle store (SQLite). |
| `debug/` | Per-call upstream-request dumps — only when `JMUNCH_DEBUG_DUMP=1`. |

To rotate a key: edit `~/.jmunch/env`, run `jmunch-mcp gateway install` (re-renders the unit with the new value), then `jmunch-mcp gateway restart`. `install` reads `~/.jmunch/env` by default; override the path with `--env-file`.

#### Without the wizard

If you'd rather skip Textual entirely:

```bash
jmunch-mcp gateway init                  # writes a minimal ~/.jmunch/gateway.toml
$EDITOR ~/.jmunch/gateway.toml           # edit the [[upstream]] block
$EDITOR ~/.jmunch/env                    # add lines like: OPENAI_API_KEY=sk-...
chmod 600 ~/.jmunch/env
jmunch-mcp gateway install
```

#### Service mechanics

On macOS the unit is a launchd agent (`~/Library/LaunchAgents/sh.jmunch.gateway.plist`); on Linux, a systemd user unit (`~/.config/systemd/user/sh.jmunch.gateway.service`). The service runs the same interpreter that ran `install`, restarts on failure, and logs to `~/.jmunch/logs/`. Use `--label` to run more than one instance.

The generated unit declares `JMUNCH_DEBUG_DUMP=0` in the service environment (debug dumps off). Pass `--debug-dump` to `install` to set it to `1`.

## Dashboard

A read-only local web UI over the metrics DB each proxy writes to. Shows cumulative totals, per-upstream breakdowns, and a time series of forwarded calls.

```bash
jmunch-mcp dashboard              # http://127.0.0.1:7878
jmunch-mcp dashboard --open       # also open in your default browser
```

Flags: `--port` (default `7878`), `--host` (default `127.0.0.1`), `--db <path>` to point at a non-default metrics DB, `--open` to launch the browser. Metrics only populate once proxies have recorded calls, so run your client against a wrapped upstream first.

## Integrations — `contrib/`

The [`contrib/`](contrib/) directory holds **optional, agent-specific helpers**. Nothing there is part of the core package — it is not imported, not `pip`-installed, and not shipped in the wheel — so the proxy and gateway stay agent-agnostic. Each subdirectory targets one agent or framework.

### Hermes agent users

The Hermes agent uses jmunch-mcp through the **Gateway** ([Mode 2](#two-ways-to-run-jmunch-mcp)), not the MCP proxy: point Hermes' `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` at the gateway and it works with nothing installed inside the Hermes environment — the gateway does all the work.

[`contrib/hermes-agent/`](contrib/hermes-agent/) adds:

- **`update-jmunch.sh`** — pulls the latest build into the gateway's venv from a stable checkout path and restarts the service. A safe replacement for ad-hoc `pip install -e /tmp/...` flows.

See [`contrib/hermes-agent/README.md`](contrib/hermes-agent/README.md) for setup and the full list of options.

## License

jmunch-mcp is released under the [MIT License](LICENSE) — free to use, modify, distribute, and embed in commercial products.

Note that licensing of **upstream MCP services** you proxy through jmunch-mcp is governed by those services' own terms. This applies to third-party MCP servers (GitHub, filesystem providers, vendor APIs) and to any sibling tools in the broader retrieval ecosystem you may compose with — check each upstream's license before redistribution.
