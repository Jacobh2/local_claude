#!/bin/bash
# Start the Anthropic→OpenAI proxy for Claude Code → LM Studio
cd "$(dirname "$0")"
python3 proxy.py
