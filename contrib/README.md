# contrib/

Optional, agent-specific integration helpers.

Nothing here is part of the core `jmunch-mcp` package. It is not imported
by the library, not installed by `pip`, and not shipped in the wheel
(`pyproject.toml` packages only `src/jmunch_mcp`). The proxy and gateway
themselves stay fully agent-agnostic — these helpers just make a given
agent or framework easier to wire up.

Each subdirectory targets one agent or framework:

- [`hermes-agent/`](hermes-agent/) — helpers for running the jmunch
  gateway alongside the Hermes agent.

To add support for another agent, create a new subdirectory here.
