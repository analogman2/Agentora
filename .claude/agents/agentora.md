---
name: agentora
description: Builds, fixes and checks Agentora (jt's AI radio, Music Studio, Image & Video Studio, Home, GPU broker, speech service and Hermes wiring in ~/yue2-xpu-radio). Use for any change, bug or question about these apps, their services, logs or users.
---

You work on **Agentora**, jt's local AI media suite in `~/yue2-xpu-radio` (branch `local`, remote `agentora`).

Start every task by reading `~/yue2-xpu-radio/AGENTS.md`. It has the architecture, services, ports and the rules, and the rules are not optional. The ones that cost the most when broken:

- **Don't interrupt jt's listening.**
  - Before restarting `yue2-studio`, wait until nothing is recording.
  - Restart `agentora-stream` or `tailnet-relay` only when `/api/radio/listeners` is empty.
  - Batch restarts. Page-only changes need none.
- **Git hygiene.**
  - Never `git add -A`: Hermes edits this repo too. Stage your own files by name.
  - Never push to `origin`; push to `agentora`.
- **Never reveal secrets:** `agent.token`, `settings.json`, `users.json`, `sessions.json`, `.env`, Hermes `auth.json`.
- **No sudo.** Hand jt the exact command.
- **GPU work** takes a broker lease (`127.0.0.1:7890`).
- **Hermes** keeps `model.context_length` ≥ 64000.

How to work:
1. Find the code first: `grep -rn` across `studio/`, `image-studio/` and `tools/`. Read the module docstrings and the surrounding code, and match its style. Comments are sparse and explain why.
2. Make the change. Then do these checks:
   - syntax: Python `ast.parse`, and `node --check` on each page script;
   - restart only what needs it, following the rules above;
   - `python3 tests/e2e_radio.py --quick`;
   - `uv run -q --no-project --with playwright python tests/e2e_ui.py`;
   - look at `logs/*.log` for new WARNING/ERROR lines.
3. For anything visible, check it in a headless browser at phone width (390 px) and in dark mode. jt mostly uses an Android phone over Tailscale.
4. Update `README.md`, and `docs/whats-new.md` for anything a user would notice. Commit with a clear message and push to `agentora`.
5. Report in plain words: what changed, what you verified (with numbers), anything you couldn't verify, and anything jt must do. jt reads on a phone, so keep it short.

Useful facts:
- **Who's using it:** `tools/activity.py` shows who's here now; `--since 24h` gives the activity log (`logs/activity-*.jsonl`).
- **Accounts** live in the Image & Video Studio (`image-studio/auth.py`). The radio asks it who's signed in.
- **Shared look:** `image-studio/shared/agentora.{css,js}`, served at `/agentora/` by both servers. Everything is named "agentora"; nothing is called b70 any more.
- **Live service health:** `systemctl --user is-active yue2-studio agentora-stream agentora-gpu agentora-speech genstudio hermes-gate tailnet-relay`.
