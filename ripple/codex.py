"""Run OpenAI Codex non-interactively for schema-constrained JSON answers (D46, D47).

Each call is `codex exec` with a read-only sandbox, no approvals, web search off and tool
features disabled, so the model only reads the prompt and answers. `--output-schema`
constrains the reply; `--json` events carry token usage. Milestone M16 adds a pinned binary,
concurrency limits and resumable jobs on top of this.
"""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
BIN_ENV = "RIPPLE_CODEX_BIN"
DEFAULT_MODEL = "gpt-6-astra"

# Features that give the model tools. Only those the installed codex lists as enabled are
# disabled, so a feature renamed in a later (alpha) release cannot break a call.
TOOL_FEATURES = {
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "unified_exec_tty",
    "view_image",
    "workspace_dependencies",
    "worktrees",
}


# Features an MCP-only run keeps on because codex routes MCP tool calls through them (D116).
MCP_ROUTING = {"code_mode_host"}


class CodexError(Exception):
    """A Codex call failed or returned something unusable."""


@dataclass(frozen=True)
class McpServer:
    """A stdio MCP server handed to `codex exec` (M35)."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    server: str
    tool: str
    arguments: dict[str, Any]
    ok: bool


@dataclass(frozen=True)
class McpResult:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class CodexConfig:
    executable: Path
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    timeout: float = 900.0


@dataclass(frozen=True)
class CodexResult:
    data: dict[str, Any]
    usage: dict[str, int] = field(default_factory=dict)


def default_config(**overrides: Any) -> CodexConfig:
    """$RIPPLE_CODEX_BIN, else `codex` on PATH, else the copy inside the ChatGPT app."""
    env = os.environ.get(BIN_ENV)
    found = shutil.which("codex")
    executable = Path(env) if env else Path(found) if found else APP_CODEX
    return CodexConfig(executable=executable, **overrides)


class CodexRunner:
    def __init__(self, config: CodexConfig) -> None:
        self.config = config
        self._disable: list[str] | None = None

    def version(self) -> str:
        return self._call(["--version"], stdin="", timeout=60).stdout.strip()

    def features_to_disable(self) -> list[str]:
        if self._disable is None:
            listing = self._call(["features", "list"], stdin="", timeout=60).stdout
            enabled = {
                parts[0]
                for line in listing.splitlines()
                if (parts := line.split()) and parts[-1] == "true"
            }
            self._disable = sorted(enabled & TOOL_FEATURES)
        return self._disable

    def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult:
        with tempfile.TemporaryDirectory(prefix="ripple-codex-") as tmp:
            work = Path(tmp)
            (work / "schema.json").write_text(json.dumps(schema))
            (work / "instructions.md").write_text(instructions)
            out = work / "out.json"
            args = [
                "exec",
                "-m",
                self.config.model,
                "-c",
                f'model_reasoning_effort="{self.config.effort}"',
                "--sandbox",
                "read-only",
                "-c",
                'approval_policy="never"',
                "-c",
                'web_search="disabled"',
                "-c",
                f"model_instructions_file={work / 'instructions.md'}",
                *[arg for feature in self.features_to_disable() for arg in ("--disable", feature)],
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "-C",
                str(work),
                "--output-schema",
                str(work / "schema.json"),
                "-o",
                str(out),
                "--json",
                "-",
            ]
            completed = self._call(args, stdin=prompt, timeout=self.config.timeout)
            try:
                data = json.loads(out.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise CodexError(f"Codex did not return valid JSON: {exc}") from exc
        return CodexResult(data=data, usage=_usage(completed.stdout))

    def run_with_mcp(
        self, prompt: str, instructions: str, servers: dict[str, McpServer]
    ) -> McpResult:
        """A free-text answer where the model's only tools are the given MCP servers (M35).

        Shell, exec, web search and every other tool feature stay off, as in `run`. The one
        exception is `code_mode_host`: codex 0.155 routes MCP tool calls through it, and with it
        disabled the model reports no tools at all (the M35 spike, D116).
        """
        with tempfile.TemporaryDirectory(prefix="ripple-codex-") as tmp:
            work = Path(tmp)
            (work / "instructions.md").write_text(instructions)
            out = work / "out.md"
            server_args: list[str] = []
            for name, server in servers.items():
                env = ", ".join(f"{k}={json.dumps(v)}" for k, v in server.env.items())
                server_args += ["-c", f"mcp_servers.{name}.command={json.dumps(server.command)}"]
                server_args += ["-c", f"mcp_servers.{name}.args={json.dumps(server.args)}"]
                server_args += ["-c", f"mcp_servers.{name}.env={{{env}}}"]
            disable = [f for f in self.features_to_disable() if f not in MCP_ROUTING]
            args = [
                "exec",
                "-m",
                self.config.model,
                "-c",
                f'model_reasoning_effort="{self.config.effort}"',
                "--sandbox",
                "read-only",
                "-c",
                'approval_policy="never"',
                "-c",
                'web_search="disabled"',
                "-c",
                f"model_instructions_file={work / 'instructions.md'}",
                *[arg for feature in disable for arg in ("--disable", feature)],
                *server_args,
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "-C",
                str(work),
                "-o",
                str(out),
                "--json",
                "-",
            ]
            completed = self._call(args, stdin=prompt, timeout=self.config.timeout)
            try:
                text = out.read_text()
            except OSError as exc:
                raise CodexError(f"Codex wrote no final message: {exc}") from exc
        return McpResult(
            text=text, usage=_usage(completed.stdout), calls=_tool_calls(completed.stdout)
        )

    def _call(self, args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess:
        try:
            completed = subprocess.run(
                [str(self.config.executable), *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CodexError(f"codex executable not found: {self.config.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CodexError(f"codex timed out after {timeout:.0f} s") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise CodexError(f"codex exited with {completed.returncode}: {detail}")
        return completed


def _tool_calls(events: str) -> list[ToolCall]:
    """Completed MCP tool calls from the `--json` event stream, arguments only."""
    calls: list[ToolCall] = []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        if item.get("type") == "mcp_tool_call":
            calls.append(
                ToolCall(
                    server=str(item.get("server")),
                    tool=str(item.get("tool")),
                    arguments=item.get("arguments") or {},
                    ok=item.get("error") is None and item.get("status") != "failed",
                )
            )
    return calls


def _usage(events: str) -> dict[str, int]:
    usage: dict[str, int] = {}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = {k: int(v) for k, v in event.get("usage", {}).items()}
    return usage
