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
| 🐈‍⬛ | **free on Discord** when the opt-in DNS Robot browser flow, Account API, or explicit authorized probe confirms it |
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
             └──► [Worker C] 🐈‍⬛ Discord mode (off / dnsrobot / account / probe)
             │        each worker: validate name → GET or credential-free/account JSON POST
             │        (3 s default request cap, browser headers, optional proxy) → normalize result
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
| `test_bot.py` | 29 end-to-end pipeline tests with simulated Discord messages | — |
| `test_checkers.py` | 31 offline checker tests + 2 optional `LIVE=1` real-network tests | — |
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
plus a sweep when >5000 entries. Only complete, definitive result sets enter
the cache—timeouts, blocks, and partial outages are deliberately retried.

**Stage 4 — Parallel fan-out**
```python
results = await run_all_checks(
    session, name, proxy=proxy,
    discord_mode=mode,
    discord_account_api_url=account_api_url,
    discord_account_api_headers=account_api_headers,
    discord_probe_url=probe_url,
    discord_probe_headers=authorized_checker_headers,
    timeout=remaining_check_budget,
)
```
All three checks run **concurrently** on the event loop; total wall-time ≈ the
*slowest* check, not the sum. Every HTTP call goes through `aiohttp`
(async-native) — never blocking `requests` — so the gateway heartbeat keeps
flowing while checks are in flight. An outer `asyncio.wait` deadline fence
cancels late work without waiting for a faulty custom checker to clean up.

**Stage 5 — Aggregate & cache** — the `Result` list is logged one line per
platform and cached only if no platform is unknown/blocked.

**Stage 6 — React (the answer)**
One `message.add_reaction(emoji)` per AVAILABLE platform is dispatched
concurrently. `Forbidden` (missing permission) is logged, never fatal.

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
| 🐈‍⬛ Discord | `off` → SKIPPED. `dnsrobot` → mirror the browser request documented at `https://dnsrobot.net/username-checker?u=<name>` with a credential-free JSON `POST`. `account`/`account_api` → JSON `POST` to `DISCORD_ACCOUNT_API_URL`. `probe` → `GET <authorized HTTP(S) DISCORD_PROBE_URL>` | JSON `taken: false` | JSON `taken: true` | not `^[a-z0-9._]{2,32}$` (lowercase-only!) or an explicit invalid response | 401, 403, 429, malformed URL/JSON, or network failure (all unknown) |

> **Naming clarification:** [http://Gung.lol](http://Gung.lol) was a parked domain
> when this implementation was verified, not a profile-availability service. The checker
> deliberately targets the active `https://guns.lol/<name>` platform instead.

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

- **Method:** `GET`, redirects followed for Minecraft/guns.lol and external
  probe mode. DNS Robot mode mirrors the page's browser-side `POST` JSON
  `{ "username": name }` to Discord and reads `{ "taken": true|false }`; it
  uses no credential and does not launch a browser. Account mode uses the same
  JSON shape at its configured endpoint and never calls the claim endpoint.
  guns.lol also reads its small semantic error page because an unclaimed name
  can be served as HTTP 200.
- **Headers:** realistic browser `User-Agent`, `Accept`, `Accept-Language`
  (`BROWSER_HEADERS`) are used for ordinary pages. DNS Robot mode adds only its
  public page `Origin`/`Referer` and JSON content headers; it has no credential
  input. Optional account/probe credentials are sent **only** to their
  explicitly configured endpoint, never to Minecraft, guns.lol, or DNS Robot,
  and are never logged.
- **Timeouts:** `CHECK_TIMEOUT` defaults to 3 s per outbound request, while the
  three workers also share the remaining `RESPONSE_BUDGET_SECONDS` deadline
  (4.5 s default). The bot reserves `REACTION_TIMEOUT` (0.75 s default) for
  the Discord REST reactions.
- **Proxy:** every request can optionally use a user-supplied HTTP(S)
  `PROXY_URL`. It is validated at startup, remains only in private environment
  configuration, and any credential-like text is redacted from error details.
  A challenge/rate-limit response remains `BLOCKED`; the bot never claims it
  proves availability.
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
adds an outer `asyncio.wait` fence that cancels late work without waiting for
cancellation cleanup. If the checker budget is exhausted, workers become
`ERROR` and the bot can still add ⚠️ in the reserved reaction time. Availability
outcomes cannot guarantee Discord's own network or API uptime, but the
application never intentionally waits past its internal 4.5-second handler
budget.

---

## 9. Configuration reference

Loaded once at startup from `.env` (see `.env.example`):

| Variable | Default | Used in | Notes |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | *(required)* | `bot.py` | from Developer Portal → Bot → Reset Token |
| `TARGET_CHANNEL_ID` | all channels | Stage 1 | watch exactly one channel |
| `LOG_CHANNEL_ID` | off | Stage 8 | 🎯 hits posted here |
| `DISCORD_CHECK_MODE` | `off` | checker | `off` \| `dnsrobot` \| `account` \| `account_api` (compatibility alias) \| `probe` |
| `DISCORD_ACCOUNT_API_URL` | Discord first-party eligibility route | checker | HTTP(S) account endpoint accepting `{ "username": name }` and returning a strict boolean |
| `DISCORD_ACCOUNT_API_TOKEN` | blank | account checker only | optional authorized API/OAuth credential; never reuse `DISCORD_TOKEN` or a personal client token |
| `DISCORD_ACCOUNT_API_TOKEN_HEADER` | `Authorization` | account checker only | optional credential header name |
| `DISCORD_ACCOUNT_API_TOKEN_SCHEME` | `Bearer` | account checker only | optional credential prefix; blank sends raw |
| `DISCORD_PROBE_URL` | blank | checker | authorized HTTP(S) `{username}` template for probe mode; blank skips the probe; Discord homepage is never used |
| `DISCORD_PROBE_TOKEN` | blank | Discord checker only | optional private auth token; never logged or sent to Minecraft/guns.lol |
| `DISCORD_PROBE_TOKEN_HEADER` | `Authorization` | Discord checker only | optional token header name (for example `X-API-Key`) |
| `DISCORD_PROBE_TOKEN_SCHEME` | `Bearer` | Discord checker only | optional token prefix; blank sends the raw token |
| `PROXY_URL` | direct | all requests | user-supplied HTTP(S) proxy, validated at startup; credentials stay in private env config |
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
| One platform times out | other platforms still answer | partial emojis or ⚠️ (never a misleading ❌) |
| Shared checker deadline expires | late work is cancelled; reaction reserve remains | ⚠️ or a definitive partial answer |
| All platforms unreachable | every check returns ERROR | ⚠️ |
| guns.lol Cloudflare wall (403/503 or 200 challenge page) | status BLOCKED, logged | ⚠️ or ❌ (with guns.lol omitted) |
| Mojang rate limit | automatic retry on fallback endpoint while deadline remains | normal emojis or an honest partial/⚠️ answer |
| Missing Add-Reactions permission | logged warning, checks continue | nothing (check server logs) |
| Missing bot token / malformed proxy / malformed checker URL | clean `SystemExit` before Discord connects | bot offline, console explains without exposing secrets |
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
        └── https ─► platforms (direct, or a privately supplied PROXY_URL)
```

The worker keeps a single long-lived process — no web server, no port. Logs
stream in the Render dashboard; the startup banner confirms config at a glance.

---

## 13. Security model

- **Secrets**: bot tokens, external-checker tokens, and proxy credentials live
  only in ignored `.env` files or host secret variables. `.env.example` ships
  blank credential fields—no usable token, proxy, or API key is committed.
- **Secret scope and logs**: DNS Robot mode has no credential input and adds
  only public origin headers. Account/probe credentials are attached only to
  their explicitly configured checker request. Error details redact URL
  user-info and common token/key query fragments before they reach logs or the
  checker CLI; the bot token is never forwarded to the account API or DNS Robot.
- **Least privilege**: the invite URL requests exactly `Read Messages/View
  Channels`, `Send Messages`, `Add Reactions` — no admin, no manage-guild.
- **Loop-safety**: bots *and* webhooks are ignored at Stage 1; the bot never
  reacts to its own output.
- **Blast-radius**: Minecraft/guns.lol checks are read-only GETs and the
  account check only asks for eligibility; it never calls the claim/update
  endpoint or auto-registers names.
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

- **Discord's public bot API has no username search endpoint.** DNS Robot's
  page is a browser UI that makes a credential-free direct request to Discord.
  Opt-in `dnsrobot` mode mirrors that exact published browser flow for speed; it
  does not pretend the page has a server API or launch a slow browser. A
  malformed response or 401/403/429 remains unknown. `account`/`account_api` and
  `probe` remain available, while `off` remains the safe default.
- **The account route is not a claim operation.** The checker never sends a
  username update and never stores or forwards a personal Discord client token;
  confirm any candidate in Discord's own UI and follow current policies.
- **guns.lol sits behind Cloudflare**; datacenter hosts (like Render) may see
  403 challenges. Status is reported as *blocked/unknown*, never faked. If you
  use a proxy, supply it privately through `PROXY_URL`; none is bundled.
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
| Checker I/O (mocked HTTP) | `test_checkers.py` | status/page parsing, auth-header scope, URL validation, redaction, 200/404/403/errors | no |
| Full pipeline (simulated messages) | `test_bot.py` | filters, cooldown ⏳, cache reuse, deadline fence, reaction paths, config guards | no |
| Real endpoints | `LIVE=1 python test_checkers.py` | actual Mojang/guns.lol behaviour | yes |
| Live platform probe | `python checkers.py <name>` | endpoint truth from your machine | yes |
| Production | Render logs + banner | correct config, gateway connected | yes |

**62 tests in the suite; 60 run offline** — `python test_checkers.py &&
python test_bot.py` must print `OK` before every deploy. The two remaining
live-network tests run only when `LIVE=1` is set.
