"""Textual TUI for the gateway setup wizard.

This module imports textual at module level — never import it unless the
[setup] extra is installed. `cli.wizard` does that lazily and surfaces a
friendly hint if the import fails.

The wizard writes:
  ~/.jmunch/gateway.toml — config
  ~/.jmunch/env          — KEY=VALUE secrets (mode 0600)

…and optionally calls `cli.service._install` to register the launchd /
systemd service.
"""
from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
)

from . import service
from .wizard import _read_gateway_toml, _write_gateway_toml


_KIND_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_DEFAULT_BASE = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}


# --------------------------------------------------------------------------
# modal: edit one upstream
# --------------------------------------------------------------------------

class UpstreamModal(ModalScreen[dict | None]):
    """Capture one upstream + optional API key. Dismisses with the
    upstream dict (containing a transient `_api_key` field) or None on
    cancel."""

    DEFAULT_CSS = """
    UpstreamModal {
        align: center middle;
    }
    UpstreamModal > VerticalScroll {
        background: $panel;
        padding: 1 2;
        width: 64;
        max-height: 90%;
        border: thick $primary;
    }
    UpstreamModal Input, UpstreamModal Select {
        margin-bottom: 1;
    }
    UpstreamModal Horizontal { height: 3; }
    UpstreamModal Button { margin-right: 1; min-width: 12; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, initial: dict | None = None) -> None:
        super().__init__()
        self.initial: dict[str, Any] = dict(initial or {})

    def compose(self) -> ComposeResult:
        i = self.initial
        # When editing, leave the API-key field blank — we don't have the
        # current value (it lives in ~/.jmunch/env, mode 0600). A blank
        # entry on save means "keep the existing env entry untouched".
        editing = bool(i)
        key_help = (
            "API key (blank = keep existing)" if editing
            else "API key (stored in ~/.jmunch/env, mode 0600; blank to skip)"
        )
        yield VerticalScroll(
            Label(f"[b]{'Edit upstream' if editing else 'Add upstream'}[/b]"),
            Label("Name"),
            Input(value=i.get("name", ""), id="name"),
            Label("Kind"),
            Select(
                [("openai", "openai"), ("anthropic", "anthropic")],
                value=i.get("kind", "openai"),
                id="kind",
                allow_blank=False,
            ),
            Label("Base URL"),
            Input(
                value=i.get("base_url", _DEFAULT_BASE["openai"]),
                id="base_url",
            ),
            Label(key_help),
            Input(password=True, id="api_key"),
            Horizontal(
                Button(label="Save", id="save", variant="success"),
                Button(label="Cancel", id="cancel", variant="error"),
            ),
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        # When kind changes, refresh the default base URL if the user
        # hasn't typed anything custom yet.
        if event.select.id != "kind":
            return
        base_input = self.query_one("#base_url", Input)
        if base_input.value in _DEFAULT_BASE.values() or not base_input.value.strip():
            base_input.value = _DEFAULT_BASE.get(str(event.value), base_input.value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.app.bell()
            self.app.notify("Name is required", severity="error")
            return
        kind = str(self.query_one("#kind", Select).value)
        base_url = self.query_one("#base_url", Input).value.strip()
        api_key = self.query_one("#api_key", Input).value
        self.dismiss({
            "name": name,
            "kind": kind,
            "base_url": base_url,
            "_api_key": api_key,
        })


# --------------------------------------------------------------------------
# main app: setup wizard
# --------------------------------------------------------------------------

class SetupApp(App):
    TITLE = "jmunch gateway — setup"
    SUB_TITLE = "configure & install the gateway service"

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+q", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    #form { padding: 1 2; }
    #upstream_table { height: 7; margin-bottom: 1; }
    Input, Select { margin-bottom: 1; }
    .section { color: $accent; margin-top: 1; text-style: bold; }
    .hint { color: $text-muted; margin-bottom: 1; }
    .actions { height: 3; margin-bottom: 1; }
    .actions Button { margin-right: 1; min-width: 18; }
    """

    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        super().__init__()
        ex = existing or {}
        self._existing = ex
        self.upstreams: list[dict[str, Any]] = list(ex.get("upstream", []))
        self.secrets: dict[str, str] = {}
        self.result: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        gw = self._existing.get("gateway") or {}
        inter = self._existing.get("interception") or {}
        yield VerticalScroll(
            Label("Listen address", classes="section"),
            Input(value=gw.get("listen", "127.0.0.1:7879"), id="listen"),

            Label("Upstreams  —  one per provider", classes="section"),
            Label(
                "Click 'Add upstream' to add a provider — its dialog has the "
                "masked API-key field.",
                classes="hint",
            ),
            DataTable(id="upstream_table", cursor_type="row"),
            Horizontal(
                Button(label="Add upstream", id="add", variant="primary"),
                Button(label="Edit selected", id="edit"),
                Button(label="Remove selected", id="remove"),
                classes="actions",
            ),

            Label("Default upstream", classes="section"),
            Input(value=gw.get("default_upstream", ""), id="default_upstream"),

            Label("Inject jmunch verbs", classes="section"),
            Select(
                [("auto", "auto"), ("always", "always"), ("never", "never")],
                value=inter.get("inject_tools", "auto"),
                id="inject_tools",
                allow_blank=False,
            ),

            Label(
                "Threshold tokens  —  compress tool_results larger than this",
                classes="section",
            ),
            Input(value=str(inter.get("threshold_tokens", 2000)), id="threshold"),

            Horizontal(
                Button(label="Save", id="save", variant="success"),
                Button(label="Cancel", id="cancel", variant="error"),
                classes="actions",
            ),
            id="form",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#upstream_table", DataTable)
        table.add_columns("Name", "Kind", "Base URL")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#upstream_table", DataTable)
        table.clear()
        for u in self.upstreams:
            table.add_row(u.get("name", ""), u.get("kind", ""), u.get("base_url", ""))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "add":
            self.push_screen(UpstreamModal(), self._on_upstream_added)
        elif bid == "edit":
            table = self.query_one("#upstream_table", DataTable)
            row = table.cursor_row
            if not (0 <= row < len(self.upstreams)):
                self.notify("Select a row first", severity="warning")
                return
            existing = dict(self.upstreams[row])

            def _on_edited(updated: dict | None, idx: int = row) -> None:
                if updated is None:
                    return
                # name collision check against OTHER upstreams
                for i, u in enumerate(self.upstreams):
                    if i != idx and u["name"] == updated["name"]:
                        self.notify(
                            f"'{updated['name']}' already exists",
                            severity="error",
                        )
                        return
                api_key = updated.pop("_api_key", "")
                self.upstreams[idx] = updated
                if api_key:
                    env_var = _KIND_ENV.get(updated["kind"])
                    if env_var:
                        self.secrets[env_var] = api_key
                self._refresh_table()
                self.notify(f"Updated '{updated['name']}'")

            self.push_screen(UpstreamModal(initial=existing), _on_edited)
        elif bid == "remove":
            table = self.query_one("#upstream_table", DataTable)
            row = table.cursor_row
            if 0 <= row < len(self.upstreams):
                removed = self.upstreams.pop(row)
                self._refresh_table()
                self.notify(f"Removed '{removed['name']}'")
        elif bid == "save":
            self.action_save()
        elif bid == "cancel":
            self.exit()

    def _on_upstream_added(self, upstream: dict | None) -> None:
        if upstream is None:
            return
        if any(u["name"] == upstream["name"] for u in self.upstreams):
            self.notify(f"'{upstream['name']}' already exists", severity="error")
            return
        api_key = upstream.pop("_api_key", "")
        self.upstreams.append(upstream)
        if api_key:
            env_var = _KIND_ENV.get(upstream["kind"])
            if env_var:
                self.secrets[env_var] = api_key
        self._refresh_table()
        # Auto-fill the default upstream on first add.
        if len(self.upstreams) == 1:
            self.query_one("#default_upstream", Input).value = upstream["name"]

    def action_cancel(self) -> None:
        self.exit()

    def action_save(self) -> None:
        if not self.upstreams:
            self.notify("Add at least one upstream", severity="error")
            return
        try:
            threshold = int(self.query_one("#threshold", Input).value)
            if threshold <= 0:
                raise ValueError
        except ValueError:
            self.notify("Threshold must be a positive integer", severity="error")
            return
        default_upstream = self.query_one("#default_upstream", Input).value.strip()
        if not any(u["name"] == default_upstream for u in self.upstreams):
            default_upstream = self.upstreams[0]["name"]
            self.notify(
                f"Default upstream coerced to '{default_upstream}'",
                severity="warning",
            )
        self.result = {
            "gateway": {
                "listen": self.query_one("#listen", Input).value.strip(),
                "default_upstream": default_upstream,
                "log_level": "INFO",
            },
            "upstream": self.upstreams,
            "interception": {
                "inject_tools": str(self.query_one("#inject_tools", Select).value),
                "threshold_tokens": threshold,
            },
            "handles": {
                "ttl_seconds": 3600,
                "store_path": "~/.jmunch/handles.db",
                "max_bytes": 2_000_000_000,
            },
            "_secrets": dict(self.secrets),
        }
        self.exit()


# --------------------------------------------------------------------------
# entry points called from cli.wizard
# --------------------------------------------------------------------------

def run_setup(*, force: bool) -> int:
    """Run the setup wizard, write config + env, optionally install."""
    config_path = service._default_config()
    env_path = service._default_env_path()
    if config_path.exists() and not force:
        # Confirmation outside Textual — a simple TTY prompt keeps this
        # snappy and works even with no terminal capabilities.
        print(f"A gateway config already exists at {config_path}.")
        ans = input("Overwrite? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted. Use `gateway add-upstream` to add an upstream, "
                  "or edit the file directly.")
            return 0

    existing = _read_gateway_toml(config_path) if config_path.is_file() else {}
    app = SetupApp(existing=existing)
    app.run()
    result = app.result
    if result is None:
        print("Aborted.")
        return 0

    secrets = result.pop("_secrets", {})
    _write_gateway_toml(config_path, result)
    print(f"Wrote {config_path}")
    if secrets:
        existing_env = service._read_env_file(env_path)
        existing_env.update(secrets)
        service._write_env_file(env_path, existing_env)
        print(f"Wrote {env_path}  (mode 0600 — keys: {', '.join(secrets)})")

    ans = input("\nInstall and start the gateway service now? [Y/n]: ").strip().lower()
    if ans in ("n", "no"):
        print("Done. Install later with: jmunch-mcp gateway install")
        return 0

    backend = service._backend()
    if backend is None:
        import sys
        print(f"(skipping install — gateway services are not supported on {sys.platform})")
        return 0
    return service._install(
        service.DEFAULT_LABEL,
        config_path.resolve(),
        backend,
        debug_dump=False,
        env_file=env_path,
    )


def run_add_upstream() -> int:
    """Open the upstream modal once; on save, append to gateway.toml +
    update the env file."""
    config_path = service._default_config()
    env_path = service._default_env_path()
    data = _read_gateway_toml(config_path)

    result_holder: dict[str, Any] = {}

    class _AddApp(App):
        def on_mount(self) -> None:
            def handle(upstream: dict | None) -> None:
                if upstream is not None:
                    result_holder["upstream"] = upstream
                self.exit()
            self.push_screen(UpstreamModal(), handle)

    _AddApp().run()

    upstream = result_holder.get("upstream")
    if upstream is None:
        print("Aborted.")
        return 0

    existing_names = {u.get("name") for u in data.get("upstream") or []}
    if upstream["name"] in existing_names:
        print(f"error: '{upstream['name']}' already exists.", file=__import__("sys").stderr)
        return 1

    api_key = upstream.pop("_api_key", "")
    data.setdefault("upstream", []).append(upstream)
    _write_gateway_toml(config_path, data)
    print(f"Updated {config_path}: added '{upstream['name']}'.")

    if api_key:
        env_var = _KIND_ENV.get(upstream["kind"])
        if env_var:
            existing_env = service._read_env_file(env_path)
            existing_env[env_var] = api_key
            service._write_env_file(env_path, existing_env)
            print(f"Updated {env_path}: {env_var}")

    print("\nRe-install + restart to apply:")
    print("  jmunch-mcp gateway install")
    return 0
