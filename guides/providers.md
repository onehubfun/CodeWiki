# Providers and models

CodeWiki calls an LLM for module clustering and for writing each page. You
pick where those calls go with `codewiki config set --provider ...`. Eight
providers are supported. Three authenticate through a local CLI without a CodeWiki API key.

| Provider | Needs | Good for |
| --- | --- | --- |
| `openai-compatible` (default) | API key + base URL | Any OpenAI-style endpoint: OpenAI, a LiteLLM proxy, vLLM, OpenRouter, and so on |
| `atlas-cloud` | API key | 300+ hosted models behind one OpenAI-compatible API |
| `anthropic` | API key | Direct Anthropic API |
| `azure-openai` | API key + resource URL + deployment | Azure-hosted OpenAI models |
| `bedrock` | AWS credentials + region | Anthropic and other models on AWS Bedrock |
| `claude-code` | Claude Code CLI logged in | Run on a Claude Pro or Max subscription, no per-token billing |
| `codex` | Codex CLI logged in | Run on a Codex subscription, no per-token billing |
| `trae` | TraeCode CLI 2.0 logged in | Use Trae through `traecli exec`; billing follows your Trae CLI account |

`config set` only changes the keys you pass. When you switch provider, pass
`--main-model` and `--cluster-model` again so no old model name is left over.

## API-key providers

```bash
# OpenAI-compatible (default)
codewiki config set \
  --provider openai-compatible \
  --api-key YOUR_API_KEY \
  --base-url https://api.example.com/v1 \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4 \
  --fallback-model glm-4p5

# Anthropic
codewiki config set \
  --provider anthropic \
  --api-key YOUR_API_KEY \
  --base-url https://api.anthropic.com \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4

# Azure OpenAI
codewiki config set \
  --provider azure-openai \
  --api-key YOUR_AZURE_KEY \
  --base-url https://YOUR_RESOURCE.openai.azure.com \
  --azure-deployment YOUR_DEPLOYMENT \
  --main-model gpt-4o \
  --cluster-model gpt-4o

# AWS Bedrock (uses your AWS credentials)
codewiki config set \
  --provider bedrock \
  --aws-region us-east-1 \
  --main-model anthropic.claude-sonnet-4-v2:0 \
  --cluster-model anthropic.claude-sonnet-4-v2:0
```

### Atlas Cloud

[Atlas Cloud](https://www.atlascloud.ai) is an inference platform that exposes
LLM, image, and video models behind a single OpenAI-compatible API. The base
URL is set for you (`https://api.atlascloud.ai/v1`), and the key is read from
`$ATLASCLOUD_API_KEY` when `--api-key` is omitted.

```bash
codewiki config set \
  --provider atlas-cloud \
  --main-model anthropic/claude-sonnet-4.6 \
  --cluster-model anthropic/claude-sonnet-4.6 \
  --fallback-model zai-org/GLM-4.6
```

Browse model IDs at the [models endpoint](https://api.atlascloud.ai/v1/models)
and pick a strong coding model. Their
[coding plan](https://www.atlascloud.ai/console/coding-plan) offers
budget-friendly API access.

## Subscription mode (no API key)

Subscription mode sends every LLM call through the local `claude` or `codex`
CLI binary, using the [`caw`](https://github.com/zzjas/caw) library. You pay
with your existing subscription instead of per token.

```bash
# Claude Code: install the CLI and run `claude login` first
codewiki config set \
  --provider claude-code \
  --main-model claude-sonnet-4-6 \
  --cluster-model claude-sonnet-4-6

# Codex: install the CLI and run `codex login` first
codewiki config set \
  --provider codex \
  --main-model gpt-5.4 \
  --cluster-model gpt-5.5
```

Things to know:

- **Model names are passed straight to the CLI.** Use the bare CLI name
  (`gpt-5.4`, `claude-sonnet-4-6`), not a `openai/...` or `anthropic/...`
  prefix. If you came from `openai-compatible`, re-run `config set` with both
  `--main-model` and `--cluster-model` to clear old prefixes.
- Claude Code's own `Write`, `Edit`, and `Bash` tools are disabled inside
  CodeWiki's agent loop. All documentation writes go through CodeWiki's
  editor, which validates Mermaid diagrams.
- Large repositories can push a module prompt past the CLI's input limit.
  CodeWiki trims the module tree in that case and retries.

## Which model to use

- The **main model** writes the pages. It needs strong code understanding
  and long output. Claude Sonnet-class or GPT-5-class models work well.
- The **cluster model** groups components into modules. It reads large
  inputs and writes a small JSON answer. The same model as main is fine.
- The **fallback model** takes over when the main model fails a call. Pick
  something cheaper that still handles long input.

Check the setup with:

```bash
codewiki config show
codewiki config validate
```

## Trae provider (CLI 2.0)

This provider lets **CodeWiki call Trae**, so you continue using
`codewiki generate` and `codewiki generate --update`. It does not require
adding CodeWiki's MCP server to the Trae IDE.

Install [TraeCode CLI 2.0](https://docs.trae.cn/cli_get-started-with-trae-cli)
and authenticate first:

```bash
traecli --version
traecli login
traecli login status

# Replace both placeholders with model IDs supported by your Trae CLI account.
codewiki config set --provider trae \
  --main-model YOUR_TRAE_MODEL --cluster-model YOUR_TRAE_MODEL
codewiki config validate --quick

cd /path/to/your/project
codewiki generate --language zh
codewiki generate --update
```

`traecli` must be on PATH. An IDE installation alone is not sufficient.
CodeWiki validates CLI availability; check authentication with
`traecli login status`. No CodeWiki API key or base URL is required. Models
are forwarded unchanged with `--model`; the fallback model is unused.
Trae CLI account access, quota and billing are separate from CodeWiki.

The adapter uses CLI 2.0's `exec`, stdin input, `--output-schema` and
`--output-last-message` features. Older CLI releases with only `--print`
are unsupported. See the [official CLI reference](https://docs.trae.cn/cli_command-line-parameters).

Trae returns a structured final answer or a CodeWiki tool request. CodeWiki
executes the source reader, document editor and recursive sub-module tools
locally, then supplies their results in the next request. This preserves
Mermaid validation, module depth limits and incremental-update write scopes.
Each turn starts an ephemeral CLI session and resends the conversation;
large modules can therefore incur more input usage than a persistent session.
Native shell/writer tools are disabled and the CLI runs with a read-only
sandbox. CodeWiki owns the document writes. The adapter disables configured
MCP servers and ignores user CLI configuration for these sessions; it does
not edit your Trae configuration files.

`--request-limit` bounds CLI calls per agent; `--agent-retries` bounds
consecutive malformed responses. Token usage is currently unavailable and
reported as unknown rather than estimated. A module that finishes without
saving its page is reported as a failure.

Each CLI call times out after 600 seconds. To change it:

```bash
export CODEWIKI_TRAE_TIMEOUT_SECONDS=1200
```

A nonzero exit, timeout, missing final-message file or invalid structured
response raises an error. Check CLI version, login status and model access
before retrying. Live compatibility still needs verification against your
installed Trae CLI; automated tests use a scripted CLI subprocess.
