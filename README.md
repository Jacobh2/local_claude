# local_claude

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) against local models via [LM Studio](https://lmstudio.ai) — no Anthropic API key needed.

A lightweight proxy sits between Claude Code and LM Studio, handling compatibility issues that would otherwise cause hangs or errors:

- **Haiku interception** — Claude Code sends background requests to Haiku models; these are faked instantly so they don't block your local model
- **Image relocation** — moves images from `tool_result` blocks (which LM Studio rejects) into the surrounding user message
- **Token counting** — fakes the `/count_tokens` endpoint
- **max_tokens boost** — increases `max_tokens` for thinking models that need headroom

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (automatically manages Python 3.14 for the proxy)
- [LM Studio](https://lmstudio.ai) running with a loaded model
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed (`npm install -g @anthropic-ai/claude-code`)

## Setup

```bash
git clone https://github.com/debbevi/local_claude.git
cd local_claude
cp config.example.yaml config.yaml  # optional — only needed for LiteLLM setups
chmod +x local_claude
```

## Usage

```bash
./local_claude
```

This will:
1. Ensure your model is loaded in LM Studio (with 262K context by default)
2. Start the proxy on port 4000
3. Launch Claude Code pointed at your local model

### Options

```
--model <name>   Model to load in LM Studio (default: qwen3.5-122b-a10b@4bit)
--ctx <length>   Context length (default: 262144)
--stop           Stop the proxy if it's running
```

### Examples

```bash
./local_claude --model qwen3-32b --ctx 131072
./local_claude --stop
```

## How it works

```
Claude Code  →  proxy.py (:4000)  →  LM Studio (:1234)
                    │
                    ├─ Haiku requests → instant fake response
                    ├─ /count_tokens → fake token count
                    ├─ Images in tool_results → relocated
                    └─ Everything else → forwarded to LM Studio
```

The `local_claude` script creates an isolated `$HOME` directory (`~/.local_claude`) so your regular Claude Code auth and config aren't affected.

## License

MIT
