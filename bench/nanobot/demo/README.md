# Two-terminal side-by-side demo

## One-time setup (both terminals)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install -e 'C:/MCPs/jmunch-mcp[gateway,exact-tokens]' nanobot-ai
uvx mcp-server-fetch --help   # warms the cache so it doesn't download on camera
```

## On camera

**Left terminal:**
```bash
cd C:/MCPs/jmunch-mcp/bench/nanobot/demo
bash left.sh
```

**Right terminal:**
```bash
cd C:/MCPs/jmunch-mcp/bench/nanobot/demo
bash right.sh
```

Start them at the same time. Both hit Anthropic with the same Wikipedia-scrape
prompt. Each terminal prints its own token totals at the end. `left` will be
~20,000 tokens to Anthropic; `right` will be ~300.

## What each side does

- **left.sh** — starts jmunch gateway on port 7879 in passthrough mode (no
  interception), runs nanobot against it, prints totals.
- **right.sh** — starts jmunch gateway on port 7880 with interception on,
  runs nanobot against it, prints totals.

Each side uses its own metrics DB (`.metrics-left.db` / `.metrics-right.db`)
and its own nanobot config (`~/.nanobot/config-left.json` /
`~/.nanobot/config-right.json`), so the two sides never step on each other.
You can rerun either script independently; each wipes its own state first.
