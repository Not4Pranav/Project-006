# 🎯 Multi-Sniper — Discord Username Availability Bot

Post a bare username in a watched Discord channel. The bot checks it across **8 platforms in parallel** and reacts to that same message with one emoji per platform where the name is free.

> **Platforms:** Minecraft 🕹️ · guns.lol 🔫 · Discord 🐈‍⬛ · GitHub 💻 · Steam 🎮 · Reddit 👀 · Instagram 📸 · Twitter/X 🐦

```
you:  vortex
bot:  🕹️ 💻 👀        ← free on Minecraft, GitHub, and Reddit
```

---

## Contents

- [What the reactions mean](#what-the-reactions-mean)
- [Quick start](#quick-start)
- [How a lookup works](#how-a-lookup-works)
- [Speed: why answers are instant](#speed-why-answers-are-instant)
- [Platform status matrix](#platform-status-matrix)
- [Proxy pool](#proxy-pool)
- [Smart caching](#smart-caching)
- [Discord check modes](#discord-check-modes)
- [Configuration reference](#configuration-reference)
- [Development and testing](#development-and-testing)
- [Troubleshooting](#troubleshooting)
- [Responsible use](#responsible-use)

---

## What the reactions mean

| Reaction | Meaning |
|---|---|
| 🕹️ 🔫 🐈‍⬛ 💻 🎮 👀 📸 🐦 | The name is **free** on that platform |
| ❌ | Every platform answered, and none of them were free |
| ⚠️ | At least one check could not be confirmed (block, rate limit, timeout) — treat the result as unknown |
| ⏳ | You tripped the per-user flood guard; try again immediately |

The bot never reacts to bots, webhooks, messages in other channels, or anything that is not a single bare username token.

---

## Quick start

```bash
# 1. Clone and enter the repo
git clone https://github.com/Not4Pranav/Project-006.git
cd Project-006

# 2. Create a virtualenv (Python 3.10+)
python3 -m venv .venv && source .venv/bin/activate
python --version                  # must be >= 3.10

# 3. Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 4. Configure
cp .env.example .env
#    Then edit .env and set at minimum:
#       DISCORD_TOKEN=your-bot-token
#       TARGET_CHANNEL_ID=123456789012345678

# 5. Verify offline (no token or network needed)
python test_checkers.py           # 79 tests
python test_bot.py                # 37 tests

# 6. Run
python bot.py
```

Discord-side setup (creating the application, the **Message Content Intent**, and the invite URL) is covered step by step in **[SETUP.md](SETUP.md)**.

Try it without Discord at all:

```bash
python checkers.py Notch          # one-off report for all 8 platforms
python checkers.py vortex --no-extra
```

---

## How a lookup works

1. **Filter** — bots, webhooks, wrong channels, and non-username text are dropped before any budget is spent.
2. **Flood guard** — a sub-second token bucket per user (default: 5 checks per 0.5 s).
3. **Cache** — a recent definitive answer is returned instantly, with no network calls.
4. **Parallel fan-out** — all 8 checks start at once under one shared wall-clock deadline, so total latency is the *slowest single platform*, not the sum.
5. **Normalise** — every response maps to `available` / `taken` / `invalid` / `blocked` / `skipped` / `error`.
6. **React as results land** — each free platform's emoji is added the moment that platform answers. The ❌ / ⚠️ summary is the only verdict that has to wait for everyone.
7. **Log hits** — optionally mirror free names into a private channel, always after the user-visible reaction.

Everything after step 1 shares a single response budget (4.5 s by default, hard-clamped below Discord's 5 s interaction feel), so a slow platform can never delay the reaction.

---

## Speed: why answers are instant

**Time-to-first-reaction is the number that matters** — how long before you see *any* answer. Streaming reactions cuts it by an order of magnitude:

| | First reaction | All reactions |
|---|---|---|
| Batched (old) | 1602 ms | 1604 ms |
| **Streaming (default)** | **121 ms** | 1603 ms |

*Measured over 8 platforms with one 1.6 s straggler; total time is still bounded by the slowest platform, but you no longer wait for it to learn the fast ones.*

| Technique | Effect |
|---|---|
| **Streaming reactions** | Each emoji lands the instant that platform answers — a fast free result is never held hostage by a slow site |
| Parallel fan-out with a shared deadline | 8 platforms cost one platform's latency, not the sum |
| Result cache | Repeat lookups answer in microseconds, zero requests |
| **Connection pre-warming** | TLS to all 8 hosts is established at startup, so the first lookup skips DNS + TCP + TLS |
| **Bounded page reads (96 KB)** | Steam/Instagram/X markers sit at the top of the document; the rest of a multi-MB page is never downloaded |
| **Hedged Minecraft request** | The backup Mojang endpoint starts only if the primary stalls 150 ms — one request when healthy, no doubled latency when not |
| TCP pooling + keep-alive (30 s) | No repeated handshakes between lookups |
| DNS cache (5 min) | No repeated resolution per check |
| Sub-second flood guard (5 / 0.5 s) | Back-to-back checks are not throttled in practice |
| Per-request proxy rotation | The 8 checks spread across 8 IPs instead of queueing behind one |
| One retry on transient errors | A single connection reset does not become a ⚠️ |
| gzip/deflate compression | Smaller bodies for the HTML-scraped platforms |

With streaming on, emojis appear in **completion order** rather than platform order. Set `STREAM_REACTIONS=false` if you prefer the old fixed ordering.

Brotli is deliberately **not** requested: aiohttp cannot decode `br` without the optional `Brotli` package, and advertising it makes real sites return bodies the bot cannot read.

---

## Platform status matrix

| Platform | Emoji | Reported FREE | Reported TAKEN | Unknown / blocked |
|---|---|---|---|---|
| Minecraft | 🕹️ | 204 or 404 | 200 with profile JSON | 403 / 405 / 429 |
| guns.lol | 🔫 | 404/410, or an unclaimed-page marker | 200 without that marker | 403 / 429 / 503, Cloudflare challenge |
| Discord | 🐈‍⬛ | mode-dependent | mode-dependent | mode-dependent (see below) |
| GitHub | 💻 | 404 | 200 with a `login` field | 403 / 429 (rate limit) |
| Steam | 🎮 | 404, or "profile could not be found" | 200 with profile content | 403 / 429 / 503 |
| Reddit | 👀 | 404 | 200 with user-about JSON | 403 / 429 / 503 |
| Instagram | 📸 | 404, or "this page isn't available" | 200 profile page | login wall, checkpoint, 401 / 403 / 429 |
| Twitter/X | 🐦 | 404, or "this account doesn't exist" | 200 profile page | rate limit, Arkose challenge, 403 / 429 |

Instagram and X are **best-effort**: both aggressively gate unauthenticated traffic. When they gate the bot, the result is honestly reported as unknown (⚠️) rather than guessed. Page matching folds typographic apostrophes to ASCII, so the real `doesn’t` served by those sites is matched correctly.

Minecraft is checked against `api.mojang.com` first and falls back to `api.minecraftservices.com` if the first endpoint is blocked or errors.

---

## Proxy pool

Proxies are optional. Configure them and every outbound check is routed through the rotation:

```env
# Single proxy (backward compatible)
PROXY_URL=http://user:pass@proxy.example:8080

# Pool — comma or newline separated
PROXY_URLS=http://proxy1:8080,http://proxy2:8080,http://user:pass@proxy3:8080
```

What the pool does:

- **Per-request round-robin.** The proxy is resolved for each individual request, so the 8 checks in one lookup go out over 8 different proxies.
- **Live health reporting.** A proxy that fails a *real* check is recorded immediately and benched after 3 consecutive failures — no waiting for the next health sweep.
- **Automatic retry on a different proxy.** A transient failure is retried once, and the retry resolves a fresh proxy.
- **Concurrent health sweeps** every 30 s: all proxies are probed at the same time, so N proxies cost one timeout instead of N.
- **Recovery** after a 60 s cooldown, then the proxy rejoins the rotation.

### When every proxy is down

By default the pool **keeps using proxies** (it resets the failure counters and retries them) instead of going direct. That is deliberate: if you configured proxies to keep your real IP away from these platforms, a silent direct fallback would leak exactly what you were hiding.

If you treat proxies purely as a speed optimisation, opt in:

```env
PROXY_ALLOW_DIRECT_FALLBACK=true
```

Proxy credentials are redacted from every log line and from the startup banner.

---

## Smart caching

A cache hit is the fastest possible answer: no sockets, no proxies, microseconds.

| Result | Default TTL | Why |
|---|---|---|
| Taken names | 600 s (`RESULT_CACHE_TTL` × 2) | Claimed names rarely free up quickly |
| Available names | 120 s (`RESULT_CACHE_TTL` × 0.4) | A free name may get sniped by someone else |

Only **complete, definitive** answers are cached. A lookup where any platform returned `blocked` or `error` is never cached, so a transient outage cannot pin a wrong answer for ten minutes.

The cache is pruned on write — stale entries first, then oldest — and capped by `CACHE_MAX_ENTRIES` (default 5000), so a busy server cannot grow it without bound.

---

## Discord check modes

Discord has no public username-availability API, so this check is **off by default**. Set `DISCORD_CHECK_MODE` to enable one:

| Mode | How it works | Needs |
|---|---|---|
| `off` *(default)* | Skipped; reported as `skipped` | — |
| `dnsrobot` | Loads `dnsrobot.net/username-checker` in a headless Chromium context and reads the rendered result | `python -m playwright install chromium` |
| `account` / `account_api` | POSTs `{"username": "..."}` to Discord's username-eligibility route | Optionally an authorised credential |
| `probe` | GETs your own authorised checker URL template (`200` = taken, `404` = free) | `DISCORD_PROBE_URL` |

The bot never claims a name, and never sends the Discord bot token to any of these endpoints. Any credential you configure is sent **only** to the endpoint it belongs to.

---

## Configuration reference

Every value has a safe default except `DISCORD_TOKEN`. Out-of-range or malformed values are clamped rather than crashing the bot.

### Core

| Setting | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token from the Discord Developer Portal |
| `TARGET_CHANNEL_ID` | *(blank = all)* | The single channel to watch |
| `LOG_CHANNEL_ID` | *(blank = off)* | Private channel that receives free-name hits |
| `ENABLE_EXTRA_PLATFORMS` | `true` | Include GitHub, Steam, Reddit, Instagram, Twitter/X |
| `STREAM_REACTIONS` | `true` | React per platform as it answers (fastest); `false` batches them |
| `PREWARM_CONNECTIONS` | `true` | Open TLS to all platform hosts at startup |

### Latency and throttling

| Setting | Default | Range | Description |
|---|---|---|---|
| `RESPONSE_BUDGET_SECONDS` | `4.5` | 0.5 – 4.8 | Total budget for checks + reactions |
| `CHECK_TIMEOUT` | `3` | 0.05 – budget | Per-request outbound timeout |
| `REACTION_TIMEOUT` | `0.75` | 0.05 – budget−0.05 | Cap per Discord reaction call |
| `USER_MAX_CHECKS` | `5` | 1 – 10000 | Checks allowed per user per window |
| `USER_WINDOW_SECONDS` | `0.5` | ≥ 0.01 | Flood-guard window — sub-second so checks feel instant |

### Caching

| Setting | Default | Description |
|---|---|---|
| `RESULT_CACHE_TTL` | `300` | Base TTL; the two below default to multiples of it |
| `CACHE_TTL_TAKEN` | `600` | TTL for taken names (0 disables) |
| `CACHE_TTL_AVAILABLE` | `120` | TTL for available names (0 disables) |
| `CACHE_MAX_ENTRIES` | `5000` | Hard ceiling on cached usernames |

### Proxies

| Setting | Default | Description |
|---|---|---|
| `PROXY_URL` | *(blank)* | Single proxy; also joins the pool if one is configured |
| `PROXY_URLS` | *(blank)* | Comma/newline separated pool |
| `PROXY_ALLOW_DIRECT_FALLBACK` | `false` | Go direct when every proxy is down (leaks the host IP) |

### Discord check

| Setting | Default | Description |
|---|---|---|
| `DISCORD_CHECK_MODE` | `off` | `off` / `dnsrobot` / `account` / `account_api` / `probe` |
| `DISCORD_ACCOUNT_API_URL` | Discord eligibility route | Endpoint for `account` mode |
| `DISCORD_ACCOUNT_API_TOKEN` | *(blank)* | Credential sent only to that endpoint |
| `DISCORD_ACCOUNT_API_TOKEN_HEADER` | `Authorization` | Header carrying that credential |
| `DISCORD_ACCOUNT_API_TOKEN_SCHEME` | `Bearer` | Prefix; blank sends the raw token |
| `DISCORD_PROBE_URL` | *(blank)* | Template containing `{username}` |
| `DISCORD_PROBE_TOKEN` | *(blank)* | Credential sent only to the probe |
| `DISCORD_PROBE_TOKEN_HEADER` | `Authorization` | Header carrying the probe token |
| `DISCORD_PROBE_TOKEN_SCHEME` | `Bearer` | Prefix; blank sends the raw token |

---

## Development and testing

```bash
python test_checkers.py       # 79 offline tests (interpreters, request layer, proxies)
python test_bot.py            # 37 pipeline tests (filters, budget, cache, reactions)
LIVE=1 python test_checkers.py   # additionally hit the real Mojang / guns.lol endpoints

python -m pyflakes *.py       # lint
python checkers.py Notch      # manual CLI report
```

| File | Responsibility |
|---|---|
| `bot.py` | Discord client, message pipeline, budget, cache, reactions |
| `checkers.py` | Per-platform checkers, pure interpreters, request layer, CLI |
| `proxies.py` | `ProxyPool` rotation and health, `ProxyProvider` handed to checkers |
| `test_checkers.py` / `test_bot.py` | Offline test suites (no network required) |

The status interpreters (`interpret_minecraft`, `interpret_github`, …) are pure functions of `(status, body)`, which is why the suites can cover every platform without touching the network.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Bot ignores every message | **Message Content Intent** is off in the Developer Portal, or `TARGET_CHANNEL_ID` points at another channel |
| Bot answers but adds no reaction | Missing the **Add Reactions** permission in that channel — check the log line |
| Always ⚠️ on Instagram / X | Those sites are gating the host IP; configure `PROXY_URLS` |
| ⚠️ on every platform | Outbound HTTPS is blocked, or every proxy is down (check the startup banner and `Proxy … benched` logs) |
| Discord shows `skipped` | Expected: `DISCORD_CHECK_MODE=off` is the default |
| `DNS Robot browser unavailable` | Run `python -m playwright install chromium` |
| ⏳ on ordinary use | Raise `USER_MAX_CHECKS` or lower `USER_WINDOW_SECONDS` |

---

## Responsible use

This tool reports *availability*; it does not register, reserve, or claim anything. Keep the flood guard on, respect each platform's terms of service and rate limits, and keep tokens and proxy credentials in `.env` (git-ignored) or your host's secret manager — never in the repository.
