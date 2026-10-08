"""TraeCode CLI 2.0 provider with a CodeWiki-owned tool loop.

Each CLI call returns a schema-constrained final answer or tool request.
CodeWiki executes requests using the same toolkit as the caw backend, so
editing restrictions, Mermaid validation and recursive module generation
remain in Python. Native Trae writers are disabled; no MCP installation or
changes to the user's Trae configuration are needed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from types import SimpleNamespace

from codewiki.src.be.caw_backend import CawBackend
from codewiki.src.be.doc_layout import find_doc


class TraeBackend(CawBackend):
    """Reuse CLI module orchestration, replacing caw with ``traecli exec``."""

    def __init__(self, config):
        self._config = config
        self._caw_provider = "trae"
        self._model = config.main_model or None
        self._repo_root = str(Path(config.repo_path).resolve())
        self._cli = shutil.which("traecli")
        if self._cli is None:
            raise RuntimeError(
                "Trae provider requires TraeCode CLI 2.0 ('traecli') on PATH. "
                "Install it and run 'traecli login', then try again."
            )
        try:
            self._timeout = int(os.environ.get("CODEWIKI_TRAE_TIMEOUT_SECONDS", "600"))
            if self._timeout <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("CODEWIKI_TRAE_TIMEOUT_SECONDS must be a positive integer") from exc

    def _create_agent(self, system_prompt, *, model=None, toolkit=None):
        return _TraeAgent(self, system_prompt, model or self._model, toolkit)

    def _run_module_agent_sync(self, *args, **kwargs):
        # The common implementation skips existing pages and persists the tree.
        # A textual claim of success without a saved page must not count as a
        # successful module (or make the next run skip unfinished work).
        tree = super()._run_module_agent_sync(*args, **kwargs)
        bound = inspect.signature(CawBackend._run_module_agent_sync).bind(self, *args, **kwargs)
        module_name = bound.arguments["module_name"]
        working_dir = bound.arguments["working_dir"]
        if find_doc(working_dir, module_name, tree) is None:
            if bound.arguments["module_path"] or not Path(working_dir, "overview.md").is_file():
                raise RuntimeError(f"Trae finished without writing documentation for {module_name}")
        return tree

    def _exec(self, prompt: str, model: str | None, schema: dict) -> str:
        """Pass large prompts through stdin and read only the final message."""
        with tempfile.TemporaryDirectory(prefix="codewiki-trae-") as temp:
            root = Path(temp)
            schema_path = root / "response.schema.json"
            output_path = root / "response.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                self._cli,
                "exec",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if model:
                command += ["--model", model]
            # Tool calls are returned as JSON and executed by CodeWiki. Native
            # shell/writer/MCP tools must not bypass CodeWiki's editor guards.
            command += ["-c", "mcp_servers={}"]
            for tool in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"):
                command += ["--disallowed-tool", tool]
            command.append("-")
            # Logs can grow large; keep them on disk rather than buffering an
            # event stream in memory. They are never mistaken for the answer.
            with (
                (root / "stdout.log").open("w+") as stdout,
                (root / "stderr.log").open("w+") as stderr,
            ):
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    encoding="utf-8",
                    cwd=self._repo_root,
                    start_new_session=os.name != "nt",
                )
                try:
                    process.communicate(prompt, timeout=self._timeout)
                except subprocess.TimeoutExpired as exc:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise RuntimeError(
                        f"Trae CLI timed out after {self._timeout}s. "
                        "Increase CODEWIKI_TRAE_TIMEOUT_SECONDS if needed."
                    ) from exc
                if process.returncode:
                    stderr.seek(0)
                    detail = stderr.read()[-2000:]
                    raise RuntimeError(
                        f"Trae CLI exited with status {process.returncode}. "
                        f"Check 'traecli login status' and CLI 2.0 support.\n{detail}"
                    )
            if not output_path.is_file():
                raise RuntimeError("Trae CLI returned no final-message file")
            result = output_path.read_text(encoding="utf-8").strip()
            if not result:
                raise RuntimeError("Trae CLI returned an empty final message")
            return result


class _TraeAgent:
    """Bounded structured tool loop; never evaluates model-provided code."""

    def __init__(self, backend, system_prompt, model, toolkit):
        self.backend = backend
        self.system_prompt = system_prompt or ""
        self.model = model
        self.toolkit = toolkit

    def _tools(self):
        if self.toolkit is None:
            return {}
        tools = {
            "read_code_components": self.toolkit.read_code_components,
            "str_replace_editor": self.toolkit.str_replace_editor,
        }
        if self.toolkit._allow_subagent:
            tools["generate_sub_module_documentation"] = self.toolkit._run_sub_modules
        return tools

    def completion(self, prompt):
        tools = self._tools()
        schema = {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["final", "tool"]},
                "name": {"type": "string", "enum": ["", *tools]},
                "arguments_json": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["kind", "name", "arguments_json", "text"],
            "additionalProperties": False,
        }
        descriptions = "\n".join(
            f"{name}{inspect.signature(method)}" for name, method in tools.items()
        )
        protocol = (
            "CodeWiki controls this session. Do not use native CLI tools. "
            "Return ONLY an object matching the supplied response schema. "
            "To call a tool, set kind='tool', name to its name, arguments_json "
            "to a JSON-encoded object of its keyword arguments, and text=''. "
            "To finish, set kind='final', name='', arguments_json='{}', and text "
            "to the complete final answer (including any required output tags). "
            "Tool results are provided in the conversation on the next turn. "
            "Use str_replace_editor for all document writes; working_dir is "
            "'docs' or 'repo', command is 'view', 'create', 'str_replace', 'insert', "
            "or 'undo_edit'; path is relative to that directory. Repository "
            "access only supports 'view'. Fix any reported validation errors.\n"
            f"Available tools:\n{descriptions or '(none; return the answer directly)'}"
        )
        history = [{"role": "user", "content": prompt}]
        tool_calls = 0
        invalid_responses = 0
        for turn in range(self.backend._config.request_limit):
            request = json.dumps(
                {
                    "instructions": protocol,
                    "system_prompt": self.system_prompt,
                    "conversation": history,
                },
                ensure_ascii=False,
            )
            raw = self.backend._exec(request, self.model, schema)
            try:
                response = json.loads(raw)
                if not isinstance(response, dict) or set(response) != set(schema["required"]):
                    raise ValueError("Response must contain exactly the four required fields")
                if not all(isinstance(value, str) for value in response.values()):
                    raise ValueError("All response fields must be strings")
                arguments = json.loads(response["arguments_json"])
                if not isinstance(arguments, dict):
                    raise ValueError("arguments_json must encode an object")
                if response["kind"] == "final":
                    if response["name"] or arguments or not response["text"].strip():
                        raise ValueError(
                            "Final responses need non-empty text and no tool arguments"
                        )
                    return SimpleNamespace(
                        result=response["text"],
                        total_usage=None,
                        num_turns=turn + 1,
                        total_tool_calls=tool_calls,
                    )
                if response["kind"] != "tool" or response["name"] not in tools:
                    raise ValueError("Unknown or unavailable tool")
                method = tools[response["name"]]
                inspect.signature(method).bind(**arguments)
            except (ValueError, TypeError) as exc:
                invalid_responses += 1
                if invalid_responses > self.backend._config.agent_retries:
                    raise RuntimeError(f"Invalid Trae response: {exc}") from exc
                history.append({"role": "assistant", "content": raw})
                history.append({"role": "user", "content": f"Invalid response: {exc}. Correct it."})
                continue
            invalid_responses = 0
            history.append({"role": "assistant", "content": response})
            try:
                result = method(**arguments)
                if inspect.isawaitable(result):
                    result = asyncio.run(result)
            except Exception as exc:
                result = f"Error: {exc}"
            tool_calls += 1
            history.append({"role": "tool", "name": response["name"], "content": str(result)})
        raise RuntimeError(
            f"Trae agent exceeded request_limit={self.backend._config.request_limit}"
        )
