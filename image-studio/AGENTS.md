# Image & Video Studio (genstudio)

A web front end over ComfyUI for pictures and videos, with user accounts. Served by the user systemd unit
`genstudio` on http://127.0.0.1:7870 (also reachable on the LAN/tailnet through `tailnet-relay`).

## Files
- `server.py`: aiohttp app. Routes are registered at the bottom (`app.router.add_*`). `guard` middleware does
  sign-in (cookie `gs_session`, or the assistant's bearer token from `agent.token`) and a same-origin check on writes.
  Every handler that touches jobs, media, uploads or workflows checks ownership (`owner` field), and admin-only
  actions call `require_admin`. Keep it that way: one user must never see another user's creations.
- `workflows.py`: importing ComfyUI workflows from pictures/videos/Civitai, finding model and node-pack sources,
  install tasks (downloads, node packs, ComfyUI restart), building prompts to run.
- `models.py`: the Models page (Civitai / Hugging Face browse and download into ComfyUI's model folders).
- `auth.py`: accounts (scrypt hashes), sessions, lockout.
- `hermes_gate.py`: unit `hermes-gate` on :7880, lets only signed-in Studio admins through to the Hermes web dashboard
  (unit `hermes-dashboard`, 127.0.0.1:9119), WebSockets included.
- `shared/agentora.css`, `shared/agentora.js`: the look shared by every app (switcher, now-playing bar); served at /agentora/ by this
  server and by the radio (which reads them from here). Pages set `<html data-agentora-app=...>` and `<div data-agentora-apps>`.
- `home.html`: the /home page (status of every app; public summary from /api/home).
- `ratings.py`: PG-13 ratings for content models (non-admins only see PG-13 LoRAs/workflows/models).
- `index.html`: the whole front end (one file: CSS tokens on :root with a dark-mode block, then markup, then one script).
  `$` is `document.querySelector` here. Use `escapeHtml` for anything user- or web-supplied.
- Data (never commit, never print): `settings.json` (holds API keys), `users.json`, `sessions.json`, `agent.token`,
  `jobs.json`, `uploads.json`, `workflows/`.

ComfyUI lives in ~/ComfyUI (unit `comfyui`, port 8188, its venv `~/ComfyUI/.venv` also runs this server).
Models: ~/ComfyUI/models/<folder>; outputs: ~/ComfyUI/output/genstudio/.

## How to change it safely
1. `git status` first; the folder is a git repo. Commit or note the starting point.
2. Make the change. Match the surrounding style (short helpers, comments only where the why isn't obvious).
3. Check syntax: `~/ComfyUI/.venv/bin/python -c "import ast;ast.parse(open('server.py').read())"`; for index.html,
   extract the <script> and run `node --check` on it.
4. Test on a throwaway copy before touching the live service: copy the .py files and index.html (not the data files)
   to a temp folder and run `GEN_PORT=7871 ~/ComfyUI/.venv/bin/python server.py` there (no `-I`, it imports local
   modules). It starts in first-run setup mode: create a throwaway admin. Drive it with the browser tool, then stop it
   and delete the folder. Never sign in as or change the password of the real `jt` account.
5. Only then `systemctl --user restart genstudio`, and confirm `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7870/` gives 200.
   If it doesn't start: `journalctl --user -u genstudio -n 30`.
6. `git commit` with a message saying what changed and why.

Never install ComfyUI node packs yourself; jt does that from the Workflows page.
