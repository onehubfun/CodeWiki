"""Trae provider integration tests using a real subprocess and scripted replies."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from codewiki.cli.main import cli
import codewiki.cli.config_manager as config_manager
from codewiki.cli.models.config import Configuration
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.backend import get_backend, is_caw_provider, is_cli_provider
from codewiki.src.be.caw_backend import CawBackend
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.trae_backend import TraeBackend, _TraeAgent
from codewiki.src.config import Config


def final(text="done"):
    return {"kind": "final", "name": "", "arguments_json": "{}", "text": text}


def call(name, **arguments):
    return {"kind": "tool", "name": name, "arguments_json": json.dumps(arguments), "text": ""}


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """Record argv/stdin and write canned final-message files, without API calls."""
    responses = tmp_path / "responses.json"
    log = tmp_path / "calls.jsonl"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "traecli"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
log = Path(os.environ["FAKE_TRAE_LOG"])
count = len(log.read_text().splitlines()) if log.exists() else 0
prompt = json.loads(sys.stdin.read())
with log.open("a") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt,
                            "cwd": os.getcwd()}) + "\\n")
reply = json.loads(Path(os.environ["FAKE_TRAE_RESPONSES"]).read_text())[count]
if isinstance(reply, dict) and "exit" in reply:
    print("Not logged in", file=sys.stderr)
    sys.exit(reply["exit"])
if isinstance(reply, dict) and "sleep" in reply:
    time.sleep(reply["sleep"])
if isinstance(reply, dict) and "missing" in reply:
    sys.exit(0)
output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(reply if isinstance(reply, str) else json.dumps(reply))
print("progress log (not the final answer)")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_TRAE_RESPONSES", str(responses))
    monkeypatch.setenv("FAKE_TRAE_LOG", str(log))

    def configure(replies):
        responses.write_text(json.dumps(replies))
        log.unlink(missing_ok=True)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return SimpleNamespace(configure=configure, calls=calls)


@pytest.fixture
def config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return Config.from_cli(
        repo_path=str(repo),
        output_dir=str(repo / "docs"),
        llm_base_url="",
        llm_api_key="",
        main_model="trae-main",
        cluster_model="trae-cluster",
        provider="trae",
        request_limit=10,
    )


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    root = tmp_path / "settings"
    monkeypatch.setenv("CODEWIKI_NO_KEYRING", "1")
    monkeypatch.setattr(config_manager, "CONFIG_DIR", root)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", root / "config.json")
    monkeypatch.setattr(config_manager, "CREDENTIALS_FILE", root / "credentials.json")
    return root


def test_provider_configuration_and_cli(fake_cli, isolated_settings):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "config",
            "set",
            "--provider",
            "trae",
            "--main-model",
            "trae-main",
            "--cluster-model",
            "trae-cluster",
        ],
    )
    assert result.exit_code == 0, result.output
    manager = config_manager.ConfigManager()
    assert manager.load() and manager.is_configured()
    assert manager.get_api_key() is None
    cfg = manager.get_config()
    cfg.validate()
    assert Configuration.from_dict(cfg.to_dict()).provider == "trae"
    shown = runner.invoke(cli, ["config", "show"])
    assert shown.exit_code == 0 and "traecli login" in shown.output
    checked = runner.invoke(cli, ["config", "validate", "--quick"])
    assert checked.exit_code == 0, checked.output
    assert "traecli CLI available" in checked.output


def test_provider_dispatch_and_missing_cli(config, fake_cli, monkeypatch):
    assert is_cli_provider("trae") and not is_caw_provider("trae")
    assert isinstance(get_backend(config), TraeBackend)
    monkeypatch.setattr("codewiki.src.be.trae_backend.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="traecli.*PATH"):
        get_backend(config)


def test_complete_uses_stdin_model_override_and_final_file(config, fake_cli):
    fake_cli.configure([final("<MODULE_TREE>{}</MODULE_TREE>")])
    backend = TraeBackend(config)
    result = backend.complete("代码" * 100_000, model="trae-cluster", system_prompt="Group modules")
    assert result == "<MODULE_TREE>{}</MODULE_TREE>"
    recorded = fake_cli.calls()[0]
    assert recorded["argv"][-1] == "-"
    assert "trae-cluster" in recorded["argv"]
    assert recorded["prompt"]["conversation"][0]["content"] == "代码" * 100_000
    assert recorded["prompt"]["system_prompt"] == "Group modules"
    assert recorded["cwd"] == config.repo_path
    assert "read-only" in recorded["argv"]
    assert "mcp_servers={}" in recorded["argv"]
    assert backend.last_usage is None
    # Temporary prompts, schemas and final-message files are removed after use.
    schema = recorded["argv"][recorded["argv"].index("--output-schema") + 1]
    assert not Path(schema).exists()


@pytest.mark.parametrize(
    "reply, message",
    [
        ({"exit": 1}, "status 1"),
        ({"missing": True}, "no final-message"),
        ("", "empty final message"),
    ],
)
def test_cli_failures_are_not_successful_completions(config, fake_cli, reply, message):
    fake_cli.configure([reply])
    with pytest.raises(RuntimeError, match=message):
        TraeBackend(config).complete("hello")


def test_timeout(config, fake_cli, monkeypatch):
    fake_cli.configure([{"sleep": 10}])
    monkeypatch.setenv("CODEWIKI_TRAE_TIMEOUT_SECONDS", "1")
    with pytest.raises(RuntimeError, match="timed out"):
        TraeBackend(config).complete("hello")


@pytest.mark.parametrize("value", ["0", "-1", "bad"])
def test_invalid_timeout(config, fake_cli, monkeypatch, value):
    monkeypatch.setenv("CODEWIKI_TRAE_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="positive integer"):
        TraeBackend(config)


def test_malformed_reply_can_be_corrected(config, fake_cli):
    fake_cli.configure(["not json", final("repaired")])
    assert TraeBackend(config).complete("hello") == "repaired"
    assert "Invalid response" in fake_cli.calls()[1]["prompt"]["conversation"][-1]["content"]


def test_bad_response_and_turn_limits(config, fake_cli):
    config.agent_retries = 0
    fake_cli.configure([call("not_a_tool")])
    with pytest.raises(RuntimeError, match="unavailable tool"):
        TraeBackend(config).complete("hello")
    config.request_limit = 1
    config.agent_retries = 3
    fake_cli.configure(["bad json"])
    with pytest.raises(RuntimeError, match="request_limit=1"):
        TraeBackend(config).complete("hello")


def component(config, name="f"):
    return Node(
        id=f"a.py::{name}",
        name=name,
        component_type="function",
        file_path=str(Path(config.repo_path, "a.py")),
        relative_path="a.py",
        source_code=f"def {name}(): return 1",
        language="python",
    )


def test_module_writes_through_validating_editor(config, fake_cli, monkeypatch):
    validations = []

    async def validate(path, name):
        validations.append((path, name))
        return "valid"

    monkeypatch.setattr("codewiki.src.be.utils.validate_mermaid_diagrams", validate)
    docs = Path(config.docs_dir)
    docs.mkdir(parents=True)
    tree = {"Core": {"components": ["a.py::f"], "children": {}}}
    Path(docs, "module_tree.json").write_text(json.dumps(tree))
    fake_cli.configure(
        [
            call("read_code_components", component_ids=["a.py::f"]),
            call(
                "str_replace_editor",
                working_dir="docs",
                command="create",
                path="Core.md",
                file_text="# Core\nFunction f returns 1.\n",
            ),
            final(),
        ]
    )
    backend = TraeBackend(config)
    result = asyncio.run(
        backend.run_module_agent(
            "Core",
            {"a.py::f": component(config)},
            ["a.py::f"],
            ["Core"],
            str(docs),
        )
    )
    assert result == tree
    assert Path(docs, "Core.md").read_text().startswith("# Core")
    assert len(validations) == 1
    records = fake_cli.calls()
    assert "def f()" in records[1]["prompt"]["conversation"][-1]["content"]
    assert "Mermaid validation" in records[2]["prompt"]["conversation"][-1]["content"]


def test_module_claim_without_document_is_failure(config, fake_cli):
    docs = Path(config.docs_dir)
    docs.mkdir(parents=True)
    Path(docs, "module_tree.json").write_text('{"Core": {"components": [], "children": {}}}')
    fake_cli.configure([final("Successfully wrote all pages")])
    with pytest.raises(RuntimeError, match="without writing"):
        asyncio.run(TraeBackend(config).run_module_agent("Core", {}, [], ["Core"], str(docs)))


def test_update_enforces_write_scope_and_disables_delegation(config, fake_cli, monkeypatch):
    async def validate(*args):
        return "valid"

    monkeypatch.setattr("codewiki.src.be.utils.validate_mermaid_diagrams", validate)
    docs = Path(config.docs_dir)
    docs.mkdir(parents=True)
    Path(docs, "Core.md").write_text("# Old core\n")
    Path(docs, "Other.md").write_text("# Other\n")
    deps = CodeWikiDeps(
        absolute_docs_path=str(docs),
        absolute_repo_path=config.repo_path,
        registry={},
        components={},
        path_to_current_module=["Core"],
        current_module_name="Core",
        module_tree={},
        max_depth=2,
        current_depth=1,
        config=config,
        allowed_write_paths={str(Path(docs, "Core.md").resolve())},
    )
    fake_cli.configure(
        [
            call(
                "str_replace_editor",
                working_dir="docs",
                command="str_replace",
                path="Other.md",
                old_str="# Other",
                new_str="# Bad",
            ),
            call(
                "str_replace_editor",
                working_dir="docs",
                command="create",
                path="../outside.md",
                file_text="escape",
            ),
            call("generate_sub_module_documentation", sub_module_specs={"Child": []}),
            call(
                "str_replace_editor",
                working_dir="docs",
                command="str_replace",
                path="Core.md",
                old_str="# Old core",
                new_str="# Updated core",
            ),
            final("updated"),
        ]
    )
    reply = asyncio.run(TraeBackend(config).run_update_agent("Update Core only", "Fix Core", deps))
    assert reply.text == "updated"
    assert Path(docs, "Core.md").read_text() == "# Updated core\n"
    assert Path(docs, "Other.md").read_text() == "# Other\n"
    assert not Path(config.repo_path, "outside.md").exists()
    assert reply.meta == {"turns": 5, "tool_calls": 3}


def test_recursive_modules_preserve_tree_and_depth(config, fake_cli, monkeypatch):
    async def validate(*args):
        return "valid"

    monkeypatch.setattr("codewiki.src.be.utils.validate_mermaid_diagrams", validate)
    config.max_token_per_leaf_module = 1
    docs = Path(config.docs_dir)
    docs.mkdir(parents=True)
    first = component(config)
    second = component(config, "g").model_copy(
        update={"file_path": "b.py", "relative_path": "b.py"}
    )
    tree = {"Core": {"components": [first.id, second.id], "children": {}}}
    Path(docs, "module_tree.json").write_text(json.dumps(tree))
    fake_cli.configure(
        [
            call("generate_sub_module_documentation", sub_module_specs={"Child": [first.id]}),
            call(
                "str_replace_editor",
                working_dir="docs",
                command="create",
                path="Core/Child.md",
                file_text="# Child\n",
            ),
            final("child complete"),
            call(
                "str_replace_editor",
                working_dir="docs",
                command="create",
                path="Core.md",
                file_text="# Core\nSee [Child](Core/Child.md).\n",
            ),
            final("parent complete"),
        ]
    )
    result = asyncio.run(
        TraeBackend(config).run_module_agent(
            "Core",
            {first.id: first, second.id: second},
            [first.id, second.id],
            ["Core"],
            str(docs),
        )
    )
    assert "Child" in result["Core"]["children"]
    assert Path(docs, "Core/Child.md").is_file()
    assert "generate_sub_module_documentation" not in fake_cli.calls()[1]["prompt"]["instructions"]


def test_existing_caw_factory_keeps_provider_and_tools(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "codewiki.src.be.caw_backend.CawAgent", lambda **kwargs: captured.append(kwargs)
    )
    backend = object.__new__(CawBackend)
    backend._model = "main"
    backend._caw_provider = "claude_code"
    toolkit = object()
    backend._create_agent("sys", model="cluster")
    backend._create_agent("sys", toolkit=toolkit)
    assert captured[0]["model"] == "cluster"
    assert captured[0]["provider"] == "claude_code"
    assert captured[0]["tool_servers"] == []
    assert captured[1]["tool_servers"] == [toolkit]


def test_invalid_tool_arguments_repair_without_execution(config, fake_cli):
    fake_cli.configure([call("read_code_components", unexpected="x"), final("corrected")])
    toolkit = SimpleNamespace(
        _allow_subagent=False,
        read_code_components=lambda component_ids: pytest.fail("must not execute"),
        str_replace_editor=lambda **kwargs: None,
    )
    response = _TraeAgent(TraeBackend(config), "", "main", toolkit).completion("hello")
    assert response.result == "corrected"
    assert response.total_tool_calls == 0
