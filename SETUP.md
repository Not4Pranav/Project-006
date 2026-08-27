# Multi‑Sniper: Minimal setup and deployment guide

This is a condensed guide for the default **Minecraft + guns.lol only** flow. All optional Discord‑availability modes (dnsrobot, account, probe) and multi‑host deployment details are omitted here but remain in the full `CLOUD_SETUP.md` if needed.

## 1. Prerequisites

- Python 3.10+.
- Git.
- A Discord account, a server, and a text channel the bot can watch.
- Outbound HTTPS (port 443) allowed.

## 2. Get the source and create the Python environment

```bash
# 2.1 Clone
git clone https://github.com/Not4Pranav/Project-006.git
cd Project-006

# 2.2 Create & activate venv (macOS/Linux)
python3 -m venv .venv
source .venv/bin/activate

# 2.2 (Windows PowerShell)
# py -3 -m venv .venv
# .venv\Scripts\Activate.ps1

# 2.3 Verify
python --version          # should be ≥3.10
which python              # should print .venv path
```

## 3. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> The `playwright` package is optional; it is only required if you later enable `DISCORD_CHECK_MODE=dnsrobot`.

## 4. Configure the Discord application (minimal)

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → name it → **Create**.
2. Open **Bot** → **Add Bot** → **Reset Token** → copy the token.
3. Enable **Message Content Intent** under **Privileged Gateway Intents**.
4. Open **OAuth2 → URL Generator** → tick `bot` and **Add Reactions** (plus **Send Messages** if you want hit logging).
5. Invite the bot to your server using the generated URL.
6. (Optional) Enable **Developer Mode** → right‑click the channel → **Copy Channel ID** → set `TARGET_CHANNEL_ID` in `.env`.

## 5. Create the environment file

```bash
cp .env.example .env
# Edit .env with a text editor and set at minimum:
DISCORD_TOKEN=paste-your-bot-token-here
TARGET_CHANNEL_ID=123456789012345678
# Keep DISCORD_CHECK_MODE=off (default). Do not add other Discord‑mode vars yet.
```

> `.env` is git‑ignored. Confirm it is not tracked: `git status --short` should not list `.env`.

## 6. Run local validation

```bash
python -m py_compile bot.py checkers.py && echo "compile OK"
python test_checkers.py          # 34 offline tests (live tests skipped)
python test_bot.py               # 30 pipeline tests
# Both should end with "OK".
```

## 7. Smoke‑test the checkers (live Minecraft + guns.lol, Discord skipped)

```bash
python checkers.py Notch
```

## 8. Start the bot locally

```bash
python bot.py
```

You should see:

```
🟢 MULTI‑SNIPER ONLINE as Multi‑Sniper
🔒 Watching channel : ALL CHANNELS
🕹️ Platforms        : Minecraft | guns.lol | Discord (mode: off)
🧊 Proxy            : off (direct)
⏳ User cooldown    : 3 checks / 60s
⚡ Response budget  : 4.50s (reaction cap 0.75s)
```

Type a bare username in the watched channel; the bot reacts with 🕹️ and/or 🔫.

Stop with `Ctrl+C`.

## 9. (Optional) Enable a Discord mode later

- **dnsrobot**: set `DISCORD_CHECK_MODE=dnsrobot` in `.env`, install Chromium (`python -m playwright install chromium`), and follow the full `CLOUD_SETUP.md` for browser details.
- **account / account_api / probe**: set the corresponding mode and add the required `.env` variables (see the original `SETUP.md` for full variable lists).

## 10. Deploy as a background worker (if you need 24/7)

- Use a **Background Worker** on Render, Railway, or a VPS with systemd.
- Build command: `python -m pip install -r requirements.txt` (add `&& python -m playwright install --with-deps chromium` only if `dnsrobot` is enabled).
- Start command: `python bot.py`.
- Set environment variables in the host's dashboard: `DISCORD_TOKEN`, `TARGET_CHANNEL_ID`, and any mode‑specific vars when you enable them.

> **Do not** run this bot as a web service that listens on a `PORT`; it is a long‑running worker.

---

*For the complete original guide (all Discord modes, detailed troubleshooting, per‑host steps, security rules, etc.) refer to the full `SETUP.md` that was originally in this repository.*