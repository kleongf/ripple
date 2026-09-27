"""The Codex runner, tested against a stub executable (tests never call Codex, D48)."""

import json
import stat
from pathlib import Path

import pytest

from ripple.codex import CodexConfig, CodexError, CodexRunner, default_config

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}

STUB = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path(os.environ["STUB_LOG"])
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.155.0-alpha.16.4")
    sys.exit(0)
if args[:2] == ["features", "list"]:
    print("shell_tool                  stable   true")
    print("apps                        stable   true")
    print("enable_request_compression  stable   true")
    print("sqlite                      removed  false")
    sys.exit(0)
prompt = sys.stdin.read()
log.write_text(json.dumps({"args": args, "prompt": prompt}))
mode = os.environ.get("STUB_MODE", "ok")
if mode == "fail":
    print("error: usage limit reached", file=sys.stderr)
    sys.exit(1)
out = Path(args[args.index("-o") + 1])
schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
assert schema["required"] == ["answer"]
out.write_text("not json" if mode == "bad_json" else json.dumps({"answer": "42"}))
print(json.dumps({"type": "thread.started"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5400, "output_tokens": 12}}))
"""


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "codex"
    path.write_text(STUB)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "call.json"))
    return path


def last_call(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "call.json").read_text())


def test_run_returns_parsed_output_and_usage(stub: Path, tmp_path: Path) -> None:
    result = CodexRunner(CodexConfig(executable=stub)).run("What?", SCHEMA, "Be precise.")
    assert result.data == {"answer": "42"}
    assert result.usage == {"input_tokens": 5400, "output_tokens": 12}


def test_prompt_goes_on_stdin_with_lean_flags(stub: Path, tmp_path: Path) -> None:
    CodexRunner(CodexConfig(executable=stub, model="gpt-6-astra", effort="high")).run(
        "Map these lines.", SCHEMA, "Be precise."
    )
    call = last_call(tmp_path)
    args = call["args"]
    assert call["prompt"] == "Map these lines."
    assert args[0] == "exec" and args[-1] == "-"
    assert args[args.index("-m") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="high"' in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    for flag in ("--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--json"):
        assert flag in args


def test_only_known_tool_features_are_disabled(stub: Path, tmp_path: Path) -> None:
    CodexRunner(CodexConfig(executable=stub)).run("x", SCHEMA, "y")
    args = last_call(tmp_path)["args"]
    disabled = [args[i + 1] for i, a in enumerate(args) if a == "--disable"]
    # shell_tool and apps are tools; request compression is not; names the stub does not list
    # (for example browser_use) are never passed, so a renamed feature cannot break a call.
    assert disabled == ["apps", "shell_tool"]


def test_instructions_are_passed_as_a_file(stub: Path, tmp_path: Path) -> None:
    CodexRunner(CodexConfig(executable=stub)).run("x", SCHEMA, "Reply only with JSON.")
    args = last_call(tmp_path)["args"]
    [setting] = [a for a in args if a.startswith("model_instructions_file=")]
    assert setting  # the temp file is gone after the call; its presence is enough


def test_failure_raises_with_stderr(
    stub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "fail")
    with pytest.raises(CodexError, match="usage limit"):
        CodexRunner(CodexConfig(executable=stub)).run("x", SCHEMA, "y")


def test_invalid_json_raises(stub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_MODE", "bad_json")
    with pytest.raises(CodexError, match="JSON"):
        CodexRunner(CodexConfig(executable=stub)).run("x", SCHEMA, "y")


def test_default_config_prefers_the_environment(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIPPLE_CODEX_BIN", str(stub))
    assert default_config().executable == stub
    assert default_config().model == "gpt-6-astra"


def test_missing_executable_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CodexRunner(CodexConfig(executable=tmp_path / "nope"))
    with pytest.raises(CodexError, match="not found"):
        runner.run("x", SCHEMA, "y")


def test_version(stub: Path) -> None:
    assert CodexRunner(CodexConfig(executable=stub)).version() == "codex-cli 0.155.0-alpha.16.4"
