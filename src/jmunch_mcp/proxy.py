"""Stdio proxy with interception.

c2s (client→upstream):
  - tools/call requests targeting `jmunch_*` are consumed here, dispatched
    against the local registry, and a synthesized response is written
    directly to the client. The upstream never sees them.
  - Everything else is forwarded verbatim.

s2c (upstream→client):
  - tools/list responses have our jmunch_* schemas spliced into the tools
    array before forwarding.
  - tools/call responses whose payload exceeds the configured token
    threshold are routed to the sniffer. Tabular payloads become handles;
    text/JSON/unknown fall through (M1 ships tabular only; later
    milestones add the other backends).

The JSON-RPC frame contract is MCP's: newline-delimited UTF-8 JSON, one
message per line. Framing is preserved byte-for-byte for passthrough.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import time
from typing import Any

from .config import Config
from .meta import SavingsTracker, envelope, timer_ms
from .metrics import MetricsDB
from .registry import HandleRegistry
from .sniffer import Kind, classify, extract_rows
from .stats import SessionStats
from .backends.jsontree import JSONBackend
from .backends.tabular import TabularBackend
from .backends.text import TextBackend
from .verbs import TOOL_SCHEMAS, Dispatcher, is_jmunch_tool

log = logging.getLogger("jmunch.proxy")


class Proxy:
    def __init__(self, config: Config, *, upstream_name: str = "upstream") -> None:
        self.config = config
        self.upstream_name = upstream_name
        self.registry = HandleRegistry()
        self.tracker = SavingsTracker()
        self.stats = SessionStats(tokens_saved_at_start=self.tracker.total)
        self.dispatcher = Dispatcher(self.registry, self.stats)
        self.metrics = MetricsDB()
        self._child: asyncio.subprocess.Process | None = None
        self._client_out_lock = asyncio.Lock()
        # request id → method, for response-side routing decisions
        self._pending: dict[Any, str] = {}
        # Parallel per-call metadata used for metrics (tool name, timing).
        self._pending_meta: dict[Any, dict[str, Any]] = {}

    async def run(self) -> int:
        env = {k: v for k, v in {**os.environ, **self.config.upstream.env}.items() if isinstance(v, str)}
        cmd = self.config.upstream.command
        if sys.platform == "win32" and cmd.lower() in ("npx", "npm", "node") and not cmd.endswith(".cmd"):
            cmd = cmd + ".cmd"
        self._child = await asyncio.create_subprocess_exec(
            cmd,
            *self.config.upstream.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
            env=env,
            limit=16 * 1024 * 1024,
        )
        log.info("spawned upstream pid=%s cmd=%s", self._child.pid, self.config.upstream.command)

        c2s = asyncio.create_task(self._pump_from_stdin(self._child.stdin), name="c2s")
        s2c = asyncio.create_task(self._pump_from_upstream(self._child.stdout), name="s2c")
        child_wait = asyncio.create_task(self._child.wait(), name="child")

        done, pending = await asyncio.wait(
            {c2s, s2c, child_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        rc = self._child.returncode if self._child.returncode is not None else 0
        log.info("upstream exited rc=%s", rc)
        self.metrics.close()
        return rc

    # -- c2s -----------------------------------------------------------------

    async def _pump_from_stdin(self, dst: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        stdin = sys.stdin.buffer
        while True:
            line = await loop.run_in_executor(None, stdin.readline)
            if not line:
                log.debug("c2s EOF")
                dst.close()
                return

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("c2s non-JSON frame (%d bytes)", len(line))
                dst.write(line)
                await dst.drain()
                continue

            handled_locally = await self._maybe_handle_local(msg)
            if handled_locally:
                continue

            method = msg.get("method")
            msg_id = msg.get("id")
            if method and msg_id is not None:
                self._pending[msg_id] = method
                if method == "tools/call":
                    params = msg.get("params") or {}
                    tool = params.get("name") if isinstance(params, dict) else None
                    self._pending_meta[msg_id] = {
                        "tool": tool,
                        "started_ns": time.perf_counter_ns(),
                        "request_bytes": len(line),
                    }

            dst.write(line)
            await dst.drain()

    async def _maybe_handle_local(self, msg: dict[str, Any]) -> bool:
        """If the request is a jmunch_* tools/call, handle it locally and write
        the response to the client. Returns True if consumed."""
        if msg.get("method") != "tools/call":
            return False
        params = msg.get("params") or {}
        name = params.get("name")
        if not isinstance(name, str) or not is_jmunch_tool(name):
            return False

        started = time.perf_counter_ns()
        args = params.get("arguments") or {}
        result = self.dispatcher.dispatch(name, args if isinstance(args, dict) else {})

        is_error = isinstance(result, dict) and "code" in result and "message" in result
        inner = envelope(
            result=None if is_error else result,
            error=result if is_error else None,
            raw_bytes=0,
            response_bytes=0,  # re-counted below for accuracy
            tracker=self.tracker,
            timing_ms=timer_ms(started),
        )
        inner_text = json.dumps(inner, default=str)
        # Recompute response_tokens now that we know the serialized size.
        inner["_meta"]["response_tokens"] = len(inner_text) // 4
        inner_text = json.dumps(inner, default=str)

        rpc_response = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "content": [{"type": "text", "text": inner_text}],
                "isError": is_error,
            },
        }
        await self._write_to_client(rpc_response)
        # Record the local verb call for the dashboard.
        emitted_bytes = len((json.dumps(rpc_response, default=str) + "\n").encode("utf-8"))
        duration_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        self.metrics.record(
            upstream=self.upstream_name,
            tool=name,
            response_bytes=emitted_bytes,
            duration_ms=duration_ms,
            is_error=is_error,
        )
        log.debug("c2s %s handled locally", name)
        return True

    # -- s2c -----------------------------------------------------------------

    async def _pump_from_upstream(self, src: asyncio.StreamReader) -> None:
        client_out = sys.stdout.buffer
        while True:
            line = await src.readline()
            if not line:
                log.debug("s2c EOF")
                return

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("s2c non-JSON frame (%d bytes)", len(line))
                client_out.write(line)
                client_out.flush()
                continue

            raw_bytes = len(line)
            msg_id = msg.get("id")
            meta = self._pending_meta.pop(msg_id, None) if msg_id is not None else None

            out_msg = self._maybe_rewrite_response(msg)
            if out_msg is msg:
                # unchanged: preserve original bytes exactly
                client_out.write(line)
                emitted_bytes = raw_bytes
            else:
                encoded = (json.dumps(out_msg, default=str) + "\n").encode("utf-8")
                client_out.write(encoded)
                emitted_bytes = len(encoded)
            client_out.flush()

            if meta is not None:
                self._record_tool_call(msg, out_msg, meta, raw_bytes, emitted_bytes)

    def _maybe_rewrite_response(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Inject jmunch tools into tools/list; handle-ify large tabular
        tools/call payloads. All other messages pass through."""
        msg_id = msg.get("id")
        method = self._pending.pop(msg_id, None) if msg_id is not None else None

        if method == "tools/list" and isinstance(msg.get("result"), dict):
            return self._inject_tools(msg)

        if method == "tools/call" and "result" in msg:
            return self._maybe_handle_ify(msg)

        return msg

    def _record_tool_call(
        self,
        original: dict[str, Any],
        emitted: dict[str, Any],
        meta: dict[str, Any],
        raw_bytes: int,
        emitted_bytes: int,
    ) -> None:
        tool = meta.get("tool") or "?"
        started = meta.get("started_ns") or time.perf_counter_ns()
        duration_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        saved = max(0, raw_bytes - emitted_bytes)
        handle_created = emitted is not original and saved > 0
        is_error = bool(
            isinstance(emitted.get("result"), dict) and emitted["result"].get("isError")
        )
        self.metrics.record(
            upstream=self.upstream_name,
            tool=tool,
            request_bytes=meta.get("request_bytes") or 0,
            raw_bytes=raw_bytes,
            response_bytes=emitted_bytes,
            saved_bytes=saved,
            duration_ms=duration_ms,
            handle_created=handle_created,
            is_error=is_error,
        )

    def _inject_tools(self, msg: dict[str, Any]) -> dict[str, Any]:
        result = msg["result"]
        tools = result.get("tools")
        if not isinstance(tools, list):
            return msg
        merged = list(tools)
        existing_names = {t.get("name") for t in merged if isinstance(t, dict)}
        for schema in TOOL_SCHEMAS:
            if schema["name"] not in existing_names:
                merged.append(copy.deepcopy(schema))
        out = copy.deepcopy(msg)
        out["result"]["tools"] = merged
        log.debug("s2c injected %d jmunch_* tool schemas", len(TOOL_SCHEMAS))
        return out

    def _maybe_handle_ify(self, msg: dict[str, Any]) -> dict[str, Any]:
        result = msg.get("result")
        if not isinstance(result, dict):
            return msg
        content = result.get("content")
        if not (isinstance(content, list) and content and isinstance(content[0], dict)):
            return msg

        first = content[0]
        if first.get("type") != "text":
            return msg
        text = first.get("text")
        if not isinstance(text, str):
            return msg

        threshold_bytes = self.config.threshold_tokens * 4
        if len(text) < threshold_bytes:
            self.stats.record_passthrough()
            return msg

        try:
            payload = json.loads(text)
            kind = classify(payload)
        except json.JSONDecodeError:
            # Non-JSON text blob above threshold → route to text backend.
            payload = text
            kind = Kind.TEXT
        started = time.perf_counter_ns()
        backend: Any
        summary_detail: dict[str, Any] = {}

        if kind is Kind.TEXT:
            text_payload = payload if isinstance(payload, str) else text
            try:
                backend = TextBackend(text_payload)
            except Exception as e:
                log.warning("text ingest failed, passing through: %s", e)
                return msg
            summary_detail = {"lines": len(backend._lines)}
        elif kind is Kind.TABULAR:
            rows = extract_rows(payload)
            if rows is None:
                self.stats.record_passthrough()
                return msg
            try:
                backend = TabularBackend(rows)
            except Exception as e:
                log.warning("tabular ingest failed, passing through: %s", e)
                return msg
            summary_detail = {"rows": len(rows)}
        elif kind is Kind.JSON:
            try:
                backend = JSONBackend(payload)
            except Exception as e:
                log.warning("json ingest failed, passing through: %s", e)
                return msg
            summary_detail = {"nodes": backend._node_count}
        else:
            self.stats.record_passthrough()
            return msg  # M3 will handle TEXT/UNKNOWN

        handle = self.registry.register(backend, backend.size_bytes, backend.kind)
        self.stats.record_handle_created(backend.kind)

        raw_bytes = len(text)
        handle_result: dict[str, Any] = {
            "handle": handle.id,
            "kind": handle.kind,
            "summary": backend.summary(),
            "_hint": (
                "Use jmunch_peek/jmunch_slice/jmunch_search/jmunch_describe on this handle "
                "(jmunch_aggregate for tabular only; jmunch_summarize for text only). "
                "jmunch_list_handles lists all live handles."
            ),
        }
        # response_bytes omitted → the envelope self-measures and records the
        # true savings (passing 0 would over-credit the tracker by raw_bytes).
        env = envelope(
            result=handle_result,
            raw_bytes=raw_bytes,
            tracker=self.tracker,
            timing_ms=timer_ms(started),
        )
        env_text = json.dumps(env, default=str)

        out = copy.deepcopy(msg)
        out["result"]["content"] = [{"type": "text", "text": env_text}]
        log.info(
            "handle-ified %s payload: raw=%d bytes detail=%s handle=%s saved~%d tokens",
            backend.kind, raw_bytes, summary_detail, handle.id, env["_meta"]["tokens_saved"],
        )
        return out

    # -- util ----------------------------------------------------------------

    async def _write_to_client(self, msg: dict[str, Any]) -> None:
        line = (json.dumps(msg, default=str) + "\n").encode("utf-8")
        async with self._client_out_lock:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
