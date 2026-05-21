"""Model context-window sizes for context-aware handle-ification.

Handle-ification only earns its keep when a request is large *relative to
the model's context window*. On a big-context model (Qwen3 262k, GPT-4.1 1M)
a 25k-token tool_result is a rounding error — compressing it just sheds
detail the model needs for no real budget win. This table maps a model name
to its context window so the gateway route can make that call.

Lookup is prefix-based and case-insensitive; the longest matching prefix
wins, so list order does not matter. Unknown models fall back to
`default_window`. Deployments can override or extend the table via the
`[interception.context_windows]` config section.
"""
from __future__ import annotations

# Prefix → context window (tokens). Longest matching prefix wins.
_WINDOWS: list[tuple[str, int]] = [
    ("gpt-3.5", 16_385),
    ("gpt-4o", 128_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4-turbo", 128_000),
    ("gpt-4-32k", 32_768),
    ("gpt-4", 8_192),
    ("gpt-5", 400_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    ("claude-3-5", 200_000),
    ("claude-3-7", 200_000),
    ("claude-3", 200_000),
    ("claude-haiku", 200_000),
    ("claude-sonnet", 200_000),
    ("claude-opus", 200_000),
    ("claude", 200_000),
    ("qwen3", 262_144),
    ("qwen2.5", 131_072),
    ("qwen2", 131_072),
    ("qwen", 32_768),
    ("llama-4", 1_000_000),
    ("llama-3.1", 131_072),
    ("llama-3.3", 131_072),
    ("llama-3", 8_192),
    ("llama", 128_000),
    ("mistral", 32_768),
    ("mixtral", 32_768),
    ("gemini-1.5", 1_000_000),
    ("gemini-2", 1_000_000),
    ("gemini", 1_000_000),
    ("deepseek", 65_536),
]

DEFAULT_WINDOW = 128_000


def window_for(
    model: str | None,
    *,
    default_window: int = DEFAULT_WINDOW,
    overrides: dict[str, int] | None = None,
) -> int:
    """Return the context-window size (tokens) for `model`.

    `overrides` is a config-supplied map of model name → window. It is
    consulted first (exact match, then prefix match) and beats the built-in
    table — that is how a deployment teaches the gateway about a custom or
    newly released model. Unknown models fall back to `default_window`.
    """
    if not model:
        return default_window
    m = model.lower()

    if overrides:
        if model in overrides:
            return overrides[model]
        best_override: tuple[int, int] | None = None
        for key, window in overrides.items():
            kl = key.lower()
            if m.startswith(kl) and (best_override is None or len(kl) > best_override[0]):
                best_override = (len(kl), window)
        if best_override is not None:
            return best_override[1]

    best: tuple[int, int] | None = None  # (prefix_len, window)
    for prefix, window in _WINDOWS:
        if m.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), window)
    return best[1] if best is not None else default_window
