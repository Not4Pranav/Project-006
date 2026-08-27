# 🎯 Multi-Sniper v2.0 — Discord Username Availability Bot

A Discord bot that checks **username availability** across 8 platforms when a member posts a bare username in a watched channel, then reacts with platform emojis for each free result.

> **8 Platforms checked in parallel:** Minecraft | guns.lol | Discord | GitHub | Steam | Reddit | Instagram | Twitter/X

## ✨ v2.0 — What's New

- 🚀 **5 new platforms**: GitHub 💻, Steam 🎮, Reddit 👀, Instagram 📸, Twitter/X 🐦
- 🧊 **Proxy pool**: multiple proxies with round-robin rotation, health checking, and automatic failover
- 💾 **Smart caching**: taken names cached 10 min, available names cached 2 min (snipe protection)
- ⚡ **Faster connections**: TCP connection pooling, keep-alive, DNS caching, gzip compression
- 🔄 **Auto-retry**: transient failures are retried with backoff

## Quick local onboarding (10 steps)

| # | Action |
|---|--------|
| **1** | Clone the repo: `git clone https://github.com/Not4Pranav/Project-006.git && cd Project-006` |
| **2** | Create & activate a venv: `python3 -m venv .venv` then `source .venv/bin/activate` |
| **3** | Verify Python 3.10+: `python --version` |
| **4** | Install deps: `python -m pip install --upgrade pip && python -m pip install -r requirements.txt` |
| **5** | Create `.env` from template: `cp .env.example .env` |
| **6** | Fill minimal `.env` (only token + channel):<br>`DISCORD_TOKEN=YOUR_BOT_TOKEN`<br>`TARGET_CHANNEL_ID=123456789012345678`<br>(Leave `DISCORD_CHECK_MODE=off` – the default) |
| **7** | Run offline tests (no Discord token needed):<br>`python -m py_compile bot.py checkers.py proxies.py && echo compile OK`<br>`python test_checkers.py`<br>`python test_bot.py` – both should end with `OK` |
| **8** | Start the bot: `python bot.py` – you should see the startup banner listing all 8 platforms |
| **9** | In the watched Discord channel, type a single bare username (e.g. `Notch`). The bot reacts with all platform emojis where the name is free. |
| **10** | Stop with `Ctrl+C`. |

## How it works

1. Message filter ignores bots, webhooks, non-username text, and wrong channels.
2. Cooldown guards protect upstream services.
3. **All 8 platform checks run in parallel** (shared deadline ≤ 4.5 s).
4. Results are normalized to `available` / `taken` / `invalid` / `blocked`.
5. The bot adds the appropriate emoji reaction(s) to the **same** message.

## Platform status matrix

| Platform | Emoji | FREE | TAKEN | Blocked/unknown |
|----------|-------|------|-------|-----------------|
| Minecraft | 🕹️ | 204 or 404 | 200 (profile JSON) | 403 / 405 / 429 |
| guns.lol | 🔫 | 404/410 or unclaimed-page marker | 200 without unclaimed marker | 403 / 429 / 503 |
| Discord | 🐈‍⬛ | mode-dependent | mode-dependent | mode-dependent |
| GitHub | 💻 | 404 | 200 (user JSON with login) | 403 / 429 |
| Steam | 🎮 | 404 or "profile not found" page | 200 with profile content | 403 / 503 |
| Reddit | 👀 | 404 | 200 (user-about JSON) | 403 / 429 / 503 |
| Instagram | 📸 | 404 or "page isn't available" | 200 (profile page) | 401 / 403 / login wall |
| Twitter/X | 🐦 | 404 or "doesn't exist" page | 200 (profile page) | 403 / 429 / challenge |

## 🧊 Proxy Pool

Route checks through multiple proxies for better reliability and rate-limit avoidance:

```env
# Single proxy (backward compatible)
PROXY_URL=http://user:pass@proxy.example:8080

# Proxy pool (rotation + failover)
PROXY_URLS=http://proxy1:8080,http://proxy2:8080,http://user:pass@proxy3:8080
```

**Features:**
- Round-robin rotation across healthy proxies
- Automatic health checking every 30 seconds
- Dead-proxy cooldown (3 consecutive failures = marked down)
- Automatic recovery after 60s cooldown
- Falls back to direct connection when all proxies are down

## 💾 Smart Caching

- **Taken names**: cached for 10 minutes (they rarely free up quickly)
- **Available names**: cached for 2 minutes (snipe protection — names get grabbed fast!)
- Configurable via `CACHE_TTL_TAKEN` and `CACHE_TTL_AVAILABLE`

## ⚡ Performance Optimizations

- **TCP connection pooling**: 25 connections, 10 per host, reused across requests
- **DNS caching**: 5-minute TTL avoids repeated resolution
- **Keep-alive**: connections stay open for 30s after use
- **Gzip/deflate/br compression**: all requests ask for compressed responses
- **Parallel fan-out**: all 8 platforms checked simultaneously, not sequentially
- **Shared deadline**: all checks share one wall-clock budget

## Optional Discord modes

Set `DISCORD_CHECK_MODE=dnsrobot` / `account` / `account_api` / `probe` in `.env` and follow the full `SETUP.md` for browser/credential setup.

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `DISCORD_TOKEN` | *(required)* | Bot token from Discord Developer Portal |
| `TARGET_CHANNEL_ID` | *(blank=all)* | Channel to watch |
| `LOG_CHANNEL_ID` | *(blank=off)* | Channel to log available hits |
| `CHECK_TIMEOUT` | `3` | Per-request timeout (seconds) |
| `RESPONSE_BUDGET_SECONDS` | `4.5` | Total check + reaction budget |
| `REACTION_TIMEOUT` | `0.75` | Per-reaction REST call cap |
| `USER_MAX_CHECKS` | `3` | Checks per user per window |
| `USER_WINDOW_SECONDS` | `60` | Cooldown window |
| `CACHE_TTL_TAKEN` | `600` | Cache TTL for taken names (seconds) |
| `CACHE_TTL_AVAILABLE` | `120` | Cache TTL for available names (seconds) |
| `ENABLE_EXTRA_PLATFORMS` | `true` | Enable GitHub/Steam/Reddit/Instagram/Twitter |
| `PROXY_URL` | *(blank)* | Single proxy URL |
| `PROXY_URLS` | *(blank)* | Comma-separated proxy pool |

## Development / testing

- `python test_checkers.py` – offline tests (skip live network tests).
- `python test_bot.py` – end-to-end pipeline tests.
- `python checkers.py Notch` – one-off CLI report for all platforms.
- `python checkers.py Notch --no-extra` – CLI report for core platforms only.

---

*For the complete original guide (Discord modes, DNS-Robot, account API, cloud deployment, troubleshooting, etc.) see the full `SETUP.md` and `CLOUD_SETUP.md` files.*
