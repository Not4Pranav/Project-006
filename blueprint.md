# 📘 Multi-Sniper — Technical Blueprint

> How the bot works, from the millisecond a member hits Enter to the emoji
> appearing under their message. This is the engineering deep-dive; the
> [README](README.md) is the setup guide.

---

## Table of contents

1. [Mission](#1-mission)
2. [System topology](#2-system-topology)
3. [Component map](#3-component-map)
4. [The message lifecycle (9-stage pipeline)](#4-the-message-lifecycle-9-stage-pipeline)
5. [The check engine](#5-the-check-engine)
6. [Reaction decision table](#6-reaction-decision-table)
7. [Defense layers (anti-rate-limit stack)](#7-defense-layers-anti-rate-limit-stack)
8. [Latency budget — the under-five-second response path](#8-latency-budget--the-under-five-second-response-path)
9. [Configuration reference](#9-configuration-reference)
10. [Failure-mode matrix](#10-failure-mode-matrix)
11. [Worked example — full trace of "vortex"](#11-worked-example--full-trace-of-vortex)
12. [Deployment architecture](#12-deployment-architecture)
13. [Security model](#13-security-model)
14. [Extension recipe — adding a platform](#14-extension-recipe--adding-a-platform)
15. [Known limitations (the honest bit)](#15-known-limitations-the-honest-bit)
16. [Testing & verification strategy](#16-testing--verification-strategy)

---

## 1. Mission

When a member posts a **bare username** in a watched Discord channel, the bot
determines — with a **4.5-second default internal response budget** (clamped
below five seconds) — whether that name is registrable on **Minecraft**,
**guns.lol** and (optionally, unofficially) **Discord**, then answers **with
reactions instead of chat spam**. The checker fan-out shares the budget and
Discord reactions run concurrently so retries cannot serialize past it:

| Reaction | Meaning |
| :---: | --- |
| 🕹️ | free on Minecraft |
| 🔫 | free on guns.lol |
| 🐈‍⬛ | free on Discord *(probe mode only)* |
| ❌ | not available anywhere that answered |
| ⚠️ | no free result can be confirmed: every check, or a required check, is unknown |
| ⏳ | user exceeded their check cooldown |
| *(silence)* | message was not a username / wrong channel / bot or webhook author |

---

## 2. System topology

```
========================================================================================
                                  DISCORD ECOSYSTEM
----------------------------------------------------------------------------------------
  [ Member client ] --types "vortex"--> [ Guild text channel ]
                                              │
                                              ▼
                                [ Discord WebSocket Gateway ]
                                              │  event: MESSAGE_CREATE
========================================================================================
                                              │
                                              ▼
========================================================================================
                            APPLICATION WORKER  (bot.py · SniperBot)
----------------------------------------------------------------------------------------
  STAGE 1  filter .......... ignore bots, webhooks, wrong channel, non-usernames
  STAGE 2  cooldown ........ per-user token bucket  ──hit──> react ⏳ and stop
  STAGE 3  cache ........... result seen in the last 300 s? ──yes─> jump to STAGE 6
  STAGE 4  fan-out ......... asyncio.gather — all platform checks AT ONCE
             │
             ├──► [Worker A] 🕹️ Mojang primary ──blocked/error──► fallback endpoint
             ├──► [Worker B] 🔫 guns.lol profile page (status + narrow page markers)
             └──► [Worker C] 🐈‍⬛ Discord probe  (mode=off ⇒ instant SKIPPED)
             │        each worker: validate name → GET (3 s default request
             │        cap, browser headers, optional proxy) → normalize result
             │        all workers share the remaining response-deadline budget
             ▼
  STAGE 5  aggregate ....... list[Result] → cache only if at least one answer is definitive
  STAGE 6  react ........... AVAILABLE emojis concurrently (fixed logical order)
  STAGE 7  fallback ........ none available? → ❌ (or ⚠️ if nothing definitive)
  STAGE 8  log ............. any hits? → post to 📋 LOG_CHANNEL_ID (optional)
========================================================================================
                                              │
                                              ▼
========================================================================================
                       DISCORD REST API  (PUT /channels/:id/messages/:id/reactions)
----------------------------------------------------------------------------------------
  Message now shows:   vortex
                       🕹️ 🔫        ← the only UI the member ever sees
========================================================================================
```

---

## 3. Component map

| File | Responsibility | Key symbols |
| --- | --- | --- |
| `bot.py` | Discord runtime: gateway events, filtering, cooldown, cache, reactions, logging | `SniperBot`, `on_message`, `_cooldown_hit`, `_cached`, `_react` |
| `checkers.py` | Platform knowledge: endpoints, name rules, status-code interpretation, parallel fan-out, CLI | `run_all_checks`, `check_minecraft/gunslol/discord`, `interpret_*`, `Result`, `BROWSER_HEADERS` |
| `test_bot.py` | 20 end-to-end pipeline tests with simulated Discord messages | — |
| `test_checkers.py` | 18 offline checker tests + 2 optional `LIVE=1` real-network tests | — |
| `.env` / `.env.example` | All runtime configuration; secrets never enter git | see §9 |
| `requirements.txt` | `discord.py` (gateway+REST), `aiohttp` (platform HTTP), `python-dotenv` | — |
| `Procfile` | Cloud start command (`worker: python bot.py`) | — |

**Why two modules?** `checkers.py` knows everything about *platforms* and
*nothing* about Discord; `bot.py` knows everything about *Discord* and nothing
about platform quirks. Each is testable without the other.

---

## 4. The message lifecycle (9-stage pipeline)

`SniperBot.on_message(message)` executes the following, in order:

**Stage 1 — Filter (reject fast, work never starts)**
```
webhook_id set  OR  author.bot   → return          (loop-prevention)
TARGET_CHANNEL_ID set AND channel ≠ target → return
content.strip() must fullmatch  ^[A-Za-z0-9._-]{1,32}$   → else return
```
The single-token regex silently ignores sentences, mentions (`<@123>`),
URLs, emoji, and empty messages. Cost: microseconds, zero network.

**Stage 2 — Cooldown (token bucket)**
Every user owns a `deque` of timestamps. A check is refused (react ⏳) when
the user already started `USER_MAX_CHECKS` (default 3) checks within the last
`USER_WINDOW_SECONDS` (default 60). Timestamps older than the window are
popped lazily. Buckets are pruned when >1000 users accumulate.

**Stage 3 — Cache**
`{ "vortex": (monotonic_time, [Result×3]) }`, keyed on the *lowercased* name,
TTL `RESULT_CACHE_TTL` (default 300 s). Expired entries are evicted on read,
plus a sweep when >5000 entries. Re-checking a name inside the TTL costs
**zero** outbound requests.

**Stage 4 — Parallel fan-out**
```python
results = await asyncio.gather(
    check_minecraft(session, name, proxy),
    check_gunslol(session, name, proxy),
    check_discord(session, name, proxy, mode, probe_url),
)
```
All three checks run **concurrently** on the event loop; total wall-time ≈ the
*slowest* check, not the sum. Every HTTP call goes through `aiohttp`
(async-native) — never blocking `requests` — so the gateway heartbeat keeps
flowing while checks are in flight.

**Stage 5 — Aggregate & cache** — the `Result` list is logged one line per
platform and stored in the cache.

**Stage 6 — React (the answer)**
One `message.add_reaction(emoji)` per AVAILABLE platform, in fixed platform
order (🕹️ → 🔫 → 🐈‍⬛). `Forbidden` (missing permission) is logged, never fatal.

**Stage 7 — Fallback reactions** — see the decision table in §6.

**Stage 8 — Hits log (optional)**
If any platform was AVAILABLE and `LOG_CHANNEL_ID` is set, the bot posts
`🎯 \`name\` is FREE on: … (found by @mention)` to that channel.

**Stage 9 — Done.** The handler returns; the bot never blocks waiting for
anything after this point.

---

## 5. The check engine

### 5.1 The `Result` model — one vocabulary for every platform

```python
@dataclass
class Result:
    platform: str    # "Minecraft" | "guns.lol" | "Discord"
    emoji:    str    # 🕹️ | 🔫 | 🐈‍⬛
    status:   str    # AVAILABLE | TAKEN | INVALID | BLOCKED | SKIPPED | ERROR
    detail:   str    # human note, e.g. "HTTP 404" — used in logs and the CLI
```

| Status | Meaning | Produces |
| --- | --- | --- |
| `AVAILABLE` | platform confirms the name is free | platform emoji |
| `TAKEN` | an active profile exists | nothing (contributes to ❌) |
| `INVALID` | name violates platform rules — checked **offline**, no request sent | nothing (contributes to ❌) |
| `BLOCKED` | anti-bot wall / rate limit — availability **unknown** | nothing; ⚠️ if nothing definitive |
| `SKIPPED` | checker disabled by config (Discord `off` mode) | nothing |
| `ERROR` | timeout / DNS / connection failure | nothing; ⚠️ if nothing definitive |

### 5.2 Platform matrix

| Platform | Endpoint(s) | AVAILABLE | TAKEN | INVALID (offline rule) | BLOCKED |
| --- | --- | --- | --- | --- | --- |
| 🕹️ Minecraft | `GET https://api.mojang.com/users/profiles/minecraft/<name>` → on blocked/transient error, retry `GET https://api.minecraftservices.com/minecraft/profile/lookup/name/<name>` | 204, 404 | 200 (profile JSON) | not `^[A-Za-z0-9_]{3,16}$` | 403, 405, 429 |
| 🔫 guns.lol | `GET https://guns.lol/<name>` (redirects followed; status **and** narrow response markers interpreted) | 404, 410, or a 200 “username not found”/unclaimed-title page | 200 profile page with no unclaimed/challenge marker | not `^[A-Za-z0-9._-]{2,24}$` | 403, 429, 503, or 200 Cloudflare challenge page |
| 🐈‍⬛ Discord | `off` → SKIPPED instantly. `probe` → `GET <explicit authorized DISCORD_PROBE_URL>`; blank URL also skips | custom checker 404 | custom checker 200, 401, 403 | not `^[a-z0-9._]{2,32}$` (lowercase-only!) | 429 |

Every other status code maps to `ERROR` (treated as "unknown", never silently
reported as taken or free).

### 5.3 Minecraft fallback chain

`api.mojang.com` is known to throw sporadic 403s and rate-limit datacenter
IPs. The checker therefore:

```
validate name ──invalid──► INVALID (no request)
     │ valid
     ▼
GET primary (api.mojang.com) ──BLOCKED / transient ERROR──► fallback lookup
     │ definitive (200/204/404/…)                              │
     ▼                                                         ▼
   return                                           definitive? return : unknown
network error on either ──► try the other ──► both fail ──► ERROR

The outer shared response deadline can cancel a slow fallback; its result then
becomes `ERROR` rather than making the member wait beyond the reaction budget.
```

### 5.4 Request profile (identical for every platform)

- **Method:** `GET`, redirects followed. Minecraft and the optional Discord
  probe map the final status code; guns.lol also reads its small semantic error
  page because an unclaimed name can be served as HTTP 200.
- **Headers:** realistic browser `User-Agent`, `Accept`, `Accept-Language`
  (`BROWSER_HEADERS`) so ordinary profile pages are requested consistently.
- **Timeouts:** `CHECK_TIMEOUT` defaults to 3 s per outbound request, while the
  three workers also share the remaining `RESPONSE_BUDGET_SECONDS` deadline
  (4.5 s default). The bot reserves `REACTION_TIMEOUT` (0.75 s default) for
  the Discord REST reactions.
- **Proxy:** every request can optionally use `PROXY_URL`
  (`http://user:pass@host:port`). A challenge/rate-limit response remains
  `BLOCKED`; the bot never claims it proves availability.
- **Concurrency:** the three checks share **one** `aiohttp.ClientSession`
  created once in `setup_hook()` (connection pooling, no per-message setup).

---

## 6. Reaction decision table

Let `A` = platforms with status AVAILABLE, and `S` = set of all statuses.

| Condition | Reaction(s) |
| --- | --- |
| `A ≠ ∅` | platform emoji of every AVAILABLE platform, fixed order 🕹️ 🔫 🐈‍⬛ |
| `A = ∅` and `S` contains `ERROR` or `BLOCKED` | ⚠️ *(another platform may still be free)* |
| `A = ∅` and `S ⊆ {SKIPPED}` (nothing was checkable) | ⚠️ |
| `A = ∅` and remaining statuses are only `TAKEN`, `INVALID`, and/or `SKIPPED` | ❌ |
| cooldown exceeded | ⏳ (only) |
| message rejected by Stage 1 | *(silence)* |

> The ⚠️ path is deliberate honesty: if an unblocked platform is still unknown,
> saying ❌ ("taken everywhere") would be a lie. Example: Discord check `off`
> + Mojang timeout + guns.lol 403 → ⚠️; likewise, Minecraft `TAKEN` + guns.lol
> `BLOCKED` is still ⚠️ because guns.lol might be free.

---

## 7. Defense layers (anti-rate-limit stack)

The platforms rate-limit hard (Mojang especially). Six layers stack:

| Layer | Mechanism | Default | Protects |
| --- | --- | --- | --- |
| 1. Input filter | regex + bot/webhook/channel gates | always on | stops junk traffic before it exists |
| 2. Per-user cooldown | token bucket, 3 checks / 60 s | 3/60 s | one member can't flood |
| 3. Result cache | complete definitive result sets only, TTL 300 s keyed `name.lower()` | 300 s | repeat names do not re-hit platforms; partial outages are not cached |
| 4. Offline validation | per-platform name rules | always on | impossible names cost 0 requests |
| 5. Per-request cap | `CHECK_TIMEOUT` | 3 s | one socket cannot hang the worker |
| 6. Shared response deadline | checks + parallel reactions | 4.5 s | fallback retries cannot push the visible answer past five seconds |

Plus resilience: Mojang fallback, body-aware guns.lol interpretation, and
per-check exception isolation — one platform melting down never cancels the
others. A timeout turns into an honest `ERROR` result rather than a fabricated
“taken” answer.

---

## 8. Latency budget — the under-five-second response path

The budget starts when `on_message` receives a valid Discord event (gateway
transport time occurs before that). It is deliberately smaller than five
seconds to leave scheduling headroom:

```
Stage 1–3 filter + cooldown + cache ..................      < 1 ms
Shared checker fan-out (parallel) ....................   200–800 ms typical
  └── hard cap: RESPONSE_BUDGET_SECONDS - REACTION_TIMEOUT
Parallel reaction REST calls ..........................   100–600 ms typical
  └── each hard-capped at REACTION_TIMEOUT
────────────────────────────────────────────────────────────────────────────
TYPICAL HANDLER TIME ..................................    ~0.3–1.4 s
DEFAULT HANDLER CEILING ...............................       4.5 s
```

With defaults, the fan-out has roughly **3.75 s** and reactions retain **0.75
s**. `run_all_checks` applies that same wall-clock cap to all workers; the bot
wraps it in a second outer timeout as a safety fence. If the checker budget is
exhausted, workers become `ERROR` and the bot can still add ⚠️ in the reserved
reaction time. Availability outcomes cannot guarantee Discord's own network
or API uptime, but the application never intentionally waits past its internal
4.5-second handler budget.

---

## 9. Configuration reference

Loaded once at startup from `.env` (see `.env.example`):

| Variable | Default | Used in | Notes |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | *(required)* | `bot.py` | from Developer Portal → Bot → Reset Token |
| `TARGET_CHANNEL_ID` | all channels | Stage 1 | watch exactly one channel |
| `LOG_CHANNEL_ID` | off | Stage 8 | 🎯 hits posted here |
| `DISCORD_CHECK_MODE` | `off` | checker | `off` \| `probe` |
| `DISCORD_PROBE_URL` | blank | checker | explicit authorized `{username}` template required for probe mode; Discord homepage is never used |
| `PROXY_URL` | direct | all requests | `http://user:pass@host:port` |
| `CHECK_TIMEOUT` | `3` | aiohttp session | seconds, hard cap per outbound request (clamped below response budget) |
| `RESPONSE_BUDGET_SECONDS` | `4.5` | stages 4–7 | total valid-message checker + reaction budget; clamped below 5 seconds |
| `REACTION_TIMEOUT` | `0.75` | Stage 6 | cap for each concurrent Discord reaction REST call |
| `USER_MAX_CHECKS` | `3` | Stage 2 | per user per window |
| `USER_WINDOW_SECONDS` | `60` | Stage 2 | window length |
| `RESULT_CACHE_TTL` | `300` | Stage 3 | seconds |

---

## 10. Failure-mode matrix

| What breaks | What the bot does | Member sees |
| --- | --- | --- |
| One platform times out | other platforms still answer | partial emojis or ❌ |
| Shared checker deadline expires | outstanding workers become ERROR; reaction reserve remains | ⚠️ or a definitive partial answer |
| All platforms unreachable | every check returns ERROR | ⚠️ |
| guns.lol Cloudflare wall (403/503 or 200 challenge page) | status BLOCKED, logged | ⚠️ or ❌ (with guns.lol omitted) |
| Mojang rate limit | automatic retry on fallback endpoint while deadline remains | normal emojis or an honest partial/⚠️ answer |
| Missing Add-Reactions permission | logged warning, checks continue | nothing (check server logs) |
| Bad/missing token | clean `SystemExit` with instructions | bot offline, console explains |
| Webhook mirrors channel content | ignored at Stage 1 — no reaction loops | — |
| Message Content Intent off in portal | bot runs but receives no content | silence (README troubleshooting) |
| User floods names | bucket refuses, ⏳ once per message | ⏳ |

---

## 11. Worked example — full trace of "vortex"

Member types `vortex` in channel 42 (`TARGET_CHANNEL_ID=42`, Discord mode off):

```
t=0 ms    MESSAGE_CREATE arrives via gateway
t=0 ms    Stage 1: author is human, channel 42 == 42,
          "vortex" fullmatches ^[A-Za-z0-9._-]{1,32}$   → accepted
t=0 ms    Stage 2: bucket[user] = [now]  (1/3 used)
t=0 ms    Stage 3: "vortex" not in cache → miss
t=1 ms    Stage 4: gather() launches three coroutines
            ├─ Minecraft: ^[A-Za-z0-9_]{3,16}$ ✓ → GET api.mojang.com/.../vortex
            ├─ guns.lol : ^[A-Za-z0-9._-]{2,24}$ ✓ → GET guns.lol/vortex
            └─ Discord  : mode=off → Result(SKIPPED) instantly
t=420 ms  Minecraft answers 204 → interpret → AVAILABLE
t=510 ms  guns.lol answers 404  → interpret → AVAILABLE
t=511 ms  Stage 5: cache["vortex"] = (now, [MC ✓, guns ✓, Discord skip])
          logs:
          Minecraft  available HTTP 204       (vortex)
          guns.lol   available HTTP 404       (vortex)
          Discord    skipped   check disabled (DISCORD_CHECK_MODE=off) (vortex)
t=600 ms  Stage 6: PUT reaction 🕹️
t=700 ms  Stage 6: PUT reaction 🔫
t=710 ms  Stage 7: A ≠ ∅ → no fallback needed
t=710 ms  Stage 8: LOG_CHANNEL_ID unset → skip
```

Channel shows:

```
vortex
 🕹️ 🔫
```

Total: ~0.7 s. A re-check of `vortex` within 5 minutes replays from cache in
~200 ms (two reactions only) and **zero** outbound requests.

---

## 12. Deployment architecture

**Local / dev**

```
[ python bot.py ] ──wss──► Discord gateway
        └──https──► platforms (direct or via PROXY_URL)
```

**Production (Render Background Worker, 24/7)**

```
GitHub repo (private; .env excluded by .gitignore)
        │  push / deploy
        ▼
Render Background Worker
  build:  pip install -r requirements.txt
  start:  python bot.py            (declared in Procfile)
  env:    DISCORD_TOKEN, TARGET_CHANNEL_ID, …  set in dashboard
        │
        ├── wss ──► Discord gateway (auto-reconnect handled by discord.py)
        └── https ─► platforms (PROXY_URL recommended for datacenter IPs)
```

The worker keeps a single long-lived process — no web server, no port. Logs
stream in the Render dashboard; the startup banner confirms config at a glance.

---

## 13. Security model

- **Secrets**: the token lives only in `.env` (git-ignored) or host env vars.
  `.env.example` ships placeholders only.
- **Least privilege**: the invite URL requests exactly `Read Messages/View
  Channels`, `Send Messages`, `Add Reactions` — no admin, no manage-guild.
- **Loop-safety**: bots *and* webhooks are ignored at Stage 1; the bot never
  reacts to its own output.
- **Blast-radius**: platform checks are read-only GETs; the bot cannot modify
  anything on the target platforms, and it never auto-registers names.
- **Input hygiene**: a strict single-token charset regex strips anything that
  isn't a plausible username before it ever reaches the network.

---

## 14. Extension recipe — adding a platform

Example: GitHub (its API happily answers 404 for missing users).

1. **Rule** — `GITHUB_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")`
2. **Interpreter** —
   ```python
   def interpret_github(status):
       if status == 200: return TAKEN
       if status == 404: return AVAILABLE
       if status in (403, 429): return BLOCKED
       return ERROR
   ```
3. **Checker** — copy `check_gunslol`, swap pattern/URL/emoji (e.g. 🐙)
   `https://api.github.com/users/{username}`.
4. **Register** — add one line to `run_all_checks`' `asyncio.gather(...)`.

The bot picks up the new emoji automatically — reactions are derived from
`Result.emoji`, nothing else changes. Add a test in `test_checkers.py` and
you're done.

---

## 15. Known limitations (the honest bit)

- **Discord has no public username-availability API.** `probe` mode re-creates
  the original blueprint's URL trick, but `discord.com/<name>` serves the web
  app — its answers are advisory at best. Checking for real requires a
  logged-in user session → violates Discord ToS → not implemented, on purpose.
- **guns.lol sits behind Cloudflare**; datacenter hosts (like Render) may see
  403 challenges. Status is reported as *blocked/unknown*, never faked.
  Residential/rotating proxies mitigate.
- **Minecraft "available" ≠ "claimable"**: the Mojang endpoint proves no
  profile exists; blocked/reserved words and migration edge cases can still
  refuse registration.
- **Single-process memory**: cooldown buckets and cache live in RAM; on
  restart they're empty (by design — they're protection, not data).
- **This is a notifier, not an auto-claimer.** Using it to mass-harvest names
  would violate the platforms' terms of service.

---

## 16. Testing & verification strategy

| Layer | File | What it proves | Needs network? |
| --- | --- | --- | --- |
| Status interpretation | `test_checkers.py` | every HTTP code maps to the right status | no |
| Name validation | `test_checkers.py` | platform rules accept/reject correctly | no |
| Checker I/O (mocked HTTP) | `test_checkers.py` | checkers handle 200/404/403/errors | no |
| Full pipeline (simulated messages) | `test_bot.py` | filters, cooldown ⏳, cache reuse, all reaction paths, config guards | no |
| Real endpoints | `LIVE=1 python test_checkers.py` | actual Mojang/guns.lol behaviour | yes |
| Live platform probe | `python checkers.py <name>` | endpoint truth from your machine | yes |
| Production | Render logs + banner | correct config, gateway connected | yes |

**40 tests in the suite; 38 run offline** — `python test_checkers.py &&
python test_bot.py` must print `OK` before every deploy. The two remaining
live-network tests run only when `LIVE=1` is set.
