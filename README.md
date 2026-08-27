# 🎯 Multi-Sniper — Discord Username Availability Bot (Minimal)

A Discord bot that checks **Minecraft** and **guns.lol** availability when a member posts a bare username in a watched channel, then reacts with 🕹️ (free on Minecraft) and/or 🔫 (free on guns.lol).

> **Only Minecraft + guns.lol are checked by default.** Discord availability modes (dnsrobot, account, probe) are optional and can be enabled later via `.env`.

## Quick local onboarding (10 steps)

| # | Action |
|---|--------|
| **1** | Clone the repo: `git clone https://github.com/Not4Pranav/Project-006.git && cd Project-006` |
| **2** | Create & activate a venv: `python3 -m venv .venv` then `source .venv/bin/activate` (or Windows equivalent) |
| **3** | Verify Python 3.10+: `python --version` |
| **4** | Install deps: `python -m pip install --upgrade pip && python -m pip install -r requirements.txt` |
| **5** | Create `.env` from template: `cp .env.example .env` |
| **6** | Fill minimal `.env` (only token + channel):<br>`DISCORD_TOKEN=YOUR_BOT_TOKEN`<br>`TARGET_CHANNEL_ID=123456789012345678`<br>(Leave `DISCORD_CHECK_MODE=off` – the default) |
| **7** | Run offline tests (no Discord token needed):<br>`python -m py_compile bot.py checkers.py && echo compile OK`<br>`python test_checkers.py`<br>`python test_bot.py` – both should end with `OK` |
| **8** | Start the bot: `python bot.py` – you should see a startup banner listing platforms as “Minecraft | guns.lol | Discord (mode: off)” |
| **9** | In the watched Discord channel, type a single bare username (e.g. `Notch`). The bot reacts with 🕹️ and/or 🔫 according to the platform status. |
| **10** | Stop with `Ctrl+C`. |

## How it works (brief)

1. Message filter ignores bots, webhooks, non‑username text, and wrong channels.  
2. Cooldown guards protect upstream services.  
3. Minecraft and guns.lol checks run **in parallel** (shared deadline ≤ 4.5 s).  
4. Results are normalised to `available` / `taken` / `invalid` / `blocked`.  
5. The bot adds the appropriate emoji reaction to the **same** message.

## Platform status matrix (core only)

| Platform | Emoji | FREE | TAKEN | Blocked/unknown |
|----------|-------|------|-------|-----------------|
| Minecraft | 🕹️ | 204 or 404 | 200 (profile JSON) | 403 / 405 / 429 |
| guns.lol | 🔫 | 404/410 or unclaimed‑page marker | 200 without unclaimed marker | 403 / 429 / 503 |

## Optional Discord modes (enable later)

Set `DISCORD_CHECK_MODE=dnsrobot` / `account` / `account_api` / `probe` in `.env` and follow the full `SETUP.md` for browser/credential setup.

## Development / testing

- `python test_checkers.py` – 34 offline tests (skip live network tests).  
- `python test_bot.py` – 30 end‑to‑end pipeline tests.  
- `python checkers.py Notch` – one‑off CLI report.

---

*For the complete original guide (Discord modes, DNS‑Robot, account API, cloud deployment, troubleshooting, etc.) see the full `SETUP.md` and `CLOUD_SETUP.md` files.*