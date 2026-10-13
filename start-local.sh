#!/usr/bin/env bash
# Music Studio + radio on this machine's RTX 4090, with Ollama songwriters (Qwen 3.6 35B on the CPU, Qwen 3.5 9B on the GPU).
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:7860/; then   # already running: just open it
    echo "Music Studio is already running - opening http://127.0.0.1:7860/radio"
    xdg-open http://127.0.0.1:7860/radio >/dev/null 2>&1; exit 0
fi
for m in $(ollama ps 2>/dev/null | awk 'NR>1 && $1 !~ /^qwen3.5-agent-fast|^qwen3.6-songwriter/{print $1}'); do ollama stop "$m"; done   # free VRAM held by big chat models
export STUDIO_DEVICES=cuda:0
export STUDIO_HOST=127.0.0.1
export STUDIO_LLM_UNIT=""
export STUDIO_LYRICS_LLMS="http://localhost:11434/v1|qwen3.6-songwriter|Qwen 3.6 35B (CPU),http://localhost:11434/v1|qwen3.5-agent-fast|Qwen 3.5 9B (GPU fallback)"
export STUDIO_LLM_EXTRA='{"reasoning_effort": "none"}'
export RADIO_TAKES=2
export RADIO_AHEAD=6
export RADIO_BURST_SECONDS=15
export STUDIO_LLM_TIMEOUT=600
export STUDIO_INSTRUMENTAL=$HOME/YuE/skills/yue2-music/instrumental/scripts
export STUDIO_DATA=$HOME/yue2-xpu-radio/library
export PYTHONUNBUFFERED=1
( sleep 5; xdg-open http://127.0.0.1:7860/radio >/dev/null 2>&1 ) &
exec ~/venv-yue2/bin/python ~/yue2-xpu-radio/studio/app.py
