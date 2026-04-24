"""Gateway config loader. Sibling to the MCP proxy's `config.py` — kept
separate so the gateway shape (multiple upstreams, interception policy,
handle TTL) doesn't leak into the MCP proxy's single-upstream model.
"""
from __future__ import annotations

import os
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UpstreamSpec:
    name: str
    kind: str                 # "openai" | "anthropic"
    base_url: str
    api_key_env: str | None = None  # env var to read key from; default by kind
    scrub_params: tuple[str, ...] = ()  # top-level request fields to drop before forwarding

    @property
    def api_key(self) -> str | None:
        env_name = self.api_key_env or _default_api_key_env(self.kind)
        if env_name is None:
            return None
        return os.environ.get(env_name)


def _default_api_key_env(kind: str) -> str | None:
    return {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(kind)


@dataclass
class Interception:
    threshold_tokens: int = 2000
    inject_tools: str = "auto"   # "auto" | "always" | "never"


@dataclass
class HandleStore:
    ttl_seconds: int = 3600
    store_path: str = "~/.jmunch/handles.db"
    max_bytes: int = 2_000_000_000


@dataclass
class GatewayConfig:
    listen: str = "127.0.0.1:7879"
    default_upstream: str = "openai"
    upstreams: list[UpstreamSpec] = field(default_factory=list)
    interception: Interception = field(default_factory=Interception)
    handles: HandleStore = field(default_factory=HandleStore)
    log_level: str = "INFO"

    def upstream(self, name: str) -> UpstreamSpec | None:
        for u in self.upstreams:
            if u.name == name:
                return u
        return None

    def resolve_upstream(self, *, header: str | None, model: str | None) -> UpstreamSpec:
        """Pick upstream by header → model prefix → default."""
        if header:
            found = self.upstream(header)
            if found:
                return found
        if model:
            if model.startswith("claude"):
                found = self.upstream_by_kind("anthropic")
                if found:
                    return found
            if model.startswith("gpt") or model.startswith("o1") or model.startswith("o3"):
                found = self.upstream_by_kind("openai")
                if found:
                    return found
        default = self.upstream(self.default_upstream)
        if default is None:
            raise ValueError(f"default_upstream '{self.default_upstream}' is not defined in config")
        return default

    def upstream_by_kind(self, kind: str) -> UpstreamSpec | None:
        for u in self.upstreams:
            if u.kind == kind:
                return u
        return None


def load(path: str | os.PathLike) -> GatewayConfig:
    p = Path(path)
    data = tomllib.loads(p.read_text(encoding="utf-8"))

    g = data.get("gateway") or {}
    listen = str(g.get("listen", "127.0.0.1:7879"))
    default_upstream = str(g.get("default_upstream", "openai"))
    log_level = str(g.get("log_level", "INFO"))

    upstreams = []
    for raw in data.get("upstream", []) or []:
        if "name" not in raw or "kind" not in raw or "base_url" not in raw:
            raise ValueError(f"{p}: each [[upstream]] needs name, kind, base_url")
        upstreams.append(UpstreamSpec(
            name=str(raw["name"]),
            kind=str(raw["kind"]),
            base_url=str(raw["base_url"]).rstrip("/"),
            api_key_env=raw.get("api_key_env"),
            scrub_params=tuple(str(x) for x in (raw.get("scrub_params") or ())),
        ))
    if not upstreams:
        raise ValueError(f"{p}: at least one [[upstream]] is required")

    inter_raw = data.get("interception") or {}
    interception = Interception(
        threshold_tokens=int(inter_raw.get("threshold_tokens", 2000)),
        inject_tools=str(inter_raw.get("inject_tools", "auto")),
    )
    if interception.inject_tools not in ("auto", "always", "never"):
        raise ValueError(f"{p}: interception.inject_tools must be auto|always|never")

    handles_raw = data.get("handles") or {}
    handles = HandleStore(
        ttl_seconds=int(handles_raw.get("ttl_seconds", 3600)),
        store_path=str(handles_raw.get("store_path", "~/.jmunch/handles.db")),
        max_bytes=int(handles_raw.get("max_bytes", 2_000_000_000)),
    )

    return GatewayConfig(
        listen=listen,
        default_upstream=default_upstream,
        upstreams=upstreams,
        interception=interception,
        handles=handles,
        log_level=log_level,
    )
