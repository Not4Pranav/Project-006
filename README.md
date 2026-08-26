# 🎯 Multi-Sniper — Discord Username Availability Bot

A Discord bot that works like a username scout: when a member posts a name in
the watched channel, the bot checks **Minecraft**, **guns.lol** and
(optionally) **Discord** in parallel, then reacts to that same message with one
emoji per platform where the name is **FREE**. The valid-message handler has a
**4.5-second default response budget** (clamped below five seconds): checkers
share it and reactions run in parallel.

> **Domain note:** this project checks the active **guns.lol** profile platform.
> `gung.lol` is a different, parked domain; it is not an alias for guns.lol.

| Reaction | Meaning |
| -------- | ------- |
| 🕹️ | **Free on Minecraft** (Mojang has no profile with that name) |
| 🔫 | **Free on guns.lol** (404/410, or its semantic “username not found” page) |
| 🐈‍⬛ | Free on Discord *(only through your authorized external checker — see [the honest bit](#-the-honest-bit-limitations))* |
| ❌ | Not available on any checked platform |
| ⚠️ | No free result can be confirmed because every check — or a required check — failed/was blocked |
| ⏳ | User is checking too fast (cooldown) |
| *(no reaction)* | Message ignored — not a bare username, wrong channel, or sent by a bot/webhook |

---

## 🗺️ How it works (system topology)

```
[ Member types "vortex" in the watched channel ]
            │
            ▼  (Discord WebSocket gateway → messageCreate event)
[ bot.py  ·  SniperBot ]
            │
            ├──► 1. Filter: ignore bots & webhooks, wrong channel, invalid input
            ├──► 2. Cooldown: ⏳ if the user is checking too fast
            ├──► 3. Cache:   reuse results checked in the last 5 minutes
            │
            ├──► 4. Parallel fan-out (asyncio.gather — all at once, not one-by-one)
            │        ├──► 🕹️ GET https://api.mojang.com/users/profiles/minecraft/<name>
            │        │        └─ fallback: https://api.minecraftservices.com/.../lookup/name/<name>
            │        ├──► 🔫 GET https://guns.lol/<name> (status + unclaimed-page marker)
            │        └──► 🐈‍⬛ GET <discord probe URL>          (optional, off by default)
            │                └─ each request: 3s cap by default, browser headers,
            │                   optional proxy; all share one response deadline
            ▼
[ Status-code engine maps each HTTP response to free/taken/blocked ]
            │
            ▼
[ React to the message: every free platform's emoji, else ❌ / ⚠️ ]
            └──► optional: log hits to a private 📋 log channel
```

## 📊 Platform status matrix (corrected endpoints)

The original AI Mode blueprint used malformed URLs (`https://mojang.com{username}`
is missing its `/` and isn't the real API). These are the verified ones:

| Platform | Emoji | Real endpoint | FREE | TAKEN | Blocked / unknown |
| -------- | :---: | ------------- | ---- | ----- | ----------------- |
| Minecraft | 🕹️ | `https://api.mojang.com/users/profiles/minecraft/<name>` (+ `api.minecraftservices.com/minecraft/profile/lookup/name/<name>` fallback for blocked/transient primary calls) | **204 or 404** (no profile exists) | **200** (profile JSON returned) | 403 / 429 (Mojang rate limit) |
| guns.lol | 🔫 | `https://guns.lol/<name>` | **404/410**, or a 200 page with the specific “username not found”/unclaimed title marker | **200** profile page without a challenge/unclaimed marker | 403 / 429 / 503, or a 200 Cloudflare challenge page |
| Discord | 🐈‍⬛ | *no public API exists* — disabled unless you provide an authorized checker URL | custom checker: **404** | custom checker: **200** | 401 / 403 / 429, malformed endpoint, or network failure |

> **Target naming note:** At implementation time, [http://Gung.lol](http://Gung.lol)
> is a parked domain rather than a profile-availability service. This project therefore
> checks the active [guns.lol](https://guns.lol) profile platform; it does not
> pretend that the parked `Gung.lol` domain can answer username availability.

Every check also validates the name against the platform's rules *before*
sending a request (Minecraft: `3–16` chars `A-Za-z0-9_`; guns.lol: `2–24` chars
`A-Za-z0-9._-`; Discord: `2–32` chars lowercase `a-z0-9._`), so impossible names
are reported as **invalid** without wasting a request.

**About the Discord check (important):** Discord does **not** expose any public
endpoint for checking whether an arbitrary username is available — that was
confirmed at the very start of the original thread, and the
`https://discord.com{username}` idea that appeared later in it is not a real
API (it just serves Discord's homepage). Attempting to claim-check names
requires a logged-in user session, which violates Discord's ToS. So this bot
ships the Discord check **disabled** (`DISCORD_CHECK_MODE=off`). If you have
an authorized checker service of your own, set `DISCORD_CHECK_MODE=probe` **and**
provide an HTTP(S) `{username}` URL template in `DISCORD_PROBE_URL`; the bot
otherwise skips Discord. Its explicit contract is **200 = taken**, **404 =
free**, and **401/403/429 = unknown**. If that service needs credentials, put
them only in your private `DISCORD_PROBE_TOKEN` environment value; the bot sends
it only to that endpoint and never logs it. It never pretends
`discord.com/<username>` is a valid checker.

## 📁 Project layout

```
.
├── bot.py            # the runtime: gateway events, filters, cooldown, reactions
├── checkers.py       # platform registry + parallel HTTP checks (+ CLI self-test)
├── blueprint.md      # technical deep-dive: how every stage works internally
├── test_checkers.py  # 22 offline checker tests + 2 optional LIVE=1 network tests
├── test_bot.py       # 25 end-to-end pipeline tests (simulated Discord messages)
├── .env.example      # copy to .env and fill in your secrets
├── requirements.txt  # discord.py, aiohttp, python-dotenv
├── Procfile          # cloud deployment start command
└── .gitignore        # keeps your .env out of git
```

---

## 🚀 Quick start

```bash
git clone <this repo> && cd Project-006
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste your bot token into .env
python bot.py
```

Detailed phases below.

## Phase 1 — Local setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Phase 2 — Discord Developer Portal

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → name it (e.g. *Multi-Sniper*) → **Create**.
2. **Bot** tab → **Reset Token** → copy the token into `.env` as `DISCORD_TOKEN`. Never share it.
3. Same tab → **Privileged Gateway Intents** → turn **Message Content Intent ON** (the bot cannot read messages without it).
4. **OAuth2 → URL Generator** → tick the `bot` scope → tick permissions:
   - `Read Messages/View Channels`
   - `Send Messages`
   - `Add Reactions`
   *(+ `Manage Messages` if you later add auto-clean features)*
5. Open the generated URL in a browser → pick your server → **Authorise**.

## Phase 3 — Configure

```bash
cp .env.example .env
```

> The tracked template contains **blank credential fields**. Put bot tokens,
> external-checker tokens, and proxy credentials only in your ignored `.env` or
> your deployment provider's secret manager.

| Variable | Required | Default | What it does |
| -------- | -------- | ------- | ------------ |
| `DISCORD_TOKEN` | ✅ | — | Bot token from the Developer Portal |
| `TARGET_CHANNEL_ID` | — | *(all channels)* | Only react in this channel |
| `LOG_CHANNEL_ID` | — | off | Post every "free" hit to this channel |
| `DISCORD_CHECK_MODE` | — | `off` | `off`, or `probe` with an authorized external checker |
| `DISCORD_PROBE_URL` | — | blank | Required HTTP(S) `{username}` URL template when `probe` is enabled; never defaults to Discord's homepage |
| `DISCORD_PROBE_TOKEN` | — | blank | Optional credential sent **only** to `DISCORD_PROBE_URL`; never logged or stored in tracked files |
| `DISCORD_PROBE_TOKEN_HEADER` | — | `Authorization` | Header used for the optional probe token (for example `X-API-Key`) |
| `DISCORD_PROBE_TOKEN_SCHEME` | — | `Bearer` | Prefix before the optional token; set blank to send the raw token |
| `PROXY_URL` | — | direct | User-supplied HTTP(S) proxy for outbound checks; validated at startup and never stored in the repo |
| `CHECK_TIMEOUT` | — | `3` | Per-outbound-request timeout (seconds; clamped below the response budget) |
| `RESPONSE_BUDGET_SECONDS` | — | `4.5` | Hard budget for checks **and** reactions after a valid message; clamped below 5 seconds |
| `REACTION_TIMEOUT` | — | `0.75` | Cap for each Discord reaction REST call; platform reactions run concurrently |
| `USER_MAX_CHECKS` | — | `3` | Checks allowed per user per window |
| `USER_WINDOW_SECONDS` | — | `60` | Cooldown window (seconds) |
| `RESULT_CACHE_TTL` | — | `300` | Cache repeat lookups (seconds) |

> Startup rejects malformed `PROXY_URL` values, external-checker URL templates,
> and configured auth-header names before connecting. Malformed/non-finite numeric settings
> safely fall back to defaults; credential values are never printed.

> To copy a channel ID: Discord **Settings → Advanced → Developer Mode ON**,
> then right-click the channel → **Copy Channel ID**.

## Phase 4 — Run & test

```bash
python bot.py
```

Then, in the watched channel:

| You send | Expected reaction |
| -------- | ----------------- |
| `Notch` | ❌ (taken on Minecraft and guns.lol) |
| `zxqw_99182vlt` | 🕹️ 🔫 (free on both) |
| two words / a sentence | *(ignored — no reaction)* |
| spamming more than 3 names in 60s | ⏳ |

## Phase 5 — 24/7 hosting on Render (free tier)

1. Push this project to a **private** GitHub repo (`.env` is already git-ignored — keep it that way).
2. On [Render](https://render.com) → **New + → Background Worker** → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`  *(the included `Procfile` already declares this)*
5. **Advanced → Environment Variables** — add `DISCORD_TOKEN`, `TARGET_CHANNEL_ID`, and any optional vars. Do **not** commit them to git.
6. **Deploy** — watch the live logs; you should see the startup banner.

Railway/Fly.io/any VPS with Python 3.9+ works identically.

---

## 📺 Sample output

**Bot startup (`python bot.py`):**

```
==========================================================
🟢 MULTI-SNIPER ONLINE as Multi-Sniper
🔒 Watching channel : 123456789012345678
🕹️ Platforms        : Minecraft | guns.lol | Discord (mode: off)
🧊 Proxy            : off (direct)
⏳ User cooldown    : 3 checks / 60s
⚡ Response budget  : 4.50s (reaction cap 0.75s)
==========================================================
```

**Console while members chat** (one line per platform per checked name):

```
21:14:55 INFO    Minecraft  taken     HTTP 200       (Notch)
21:14:55 INFO    guns.lol   taken     HTTP 200       (Notch)
21:14:55 INFO    Discord    skipped   check disabled (DISCORD_CHECK_MODE=off) (Notch)
21:15:31 INFO    Minecraft  available HTTP 404       (zxqw_99182vlt)
21:15:31 INFO    guns.lol   available HTTP 404       (zxqw_99182vlt)
21:15:36 INFO    cache hit for 'zxqw99182vlt'
```

**Checker CLI (`python checkers.py Notch` — no Discord needed):**

```
Availability report for 'Notch':
--------------------------------------------------------------
  🕹️ Minecraft  [TAKEN]  HTTP 200
  🔫 guns.lol   [TAKEN]  HTTP 200
  🐈‍⬛ Discord    [SKIP]   check disabled (DISCORD_CHECK_MODE=off)
--------------------------------------------------------------
  Bot would react: ❌
```

**Test suite (`python test_checkers.py && python test_bot.py`):**

```
Ran 24 tests in 0.03s
OK (skipped=2)      <- the 2 live tests run only with LIVE=1
Ran 25 tests in 0.14s
OK
```

**Live endpoint tests (from your own machine):**

```bash
LIVE=1 python test_checkers.py     # hits the real Mojang + guns.lol APIs
python checkers.py Notch           # inspect Minecraft + guns.lol (Discord is skipped by default)
python checkers.py zxqw7k3vlt9m42q # inspect a random candidate name
```

---

## 🔧 Optional features

- **User-supplied proxy** — set `PROXY_URL` only in your private `.env` or host
  secret manager. Every outbound check uses it; HTTP(S) URLs are validated at
  startup. SOCKS needs `pip install aiohttp-socks` plus a small code change.
- **Hits logging** — set `LOG_CHANNEL_ID` and every name found free is posted
  to that channel with the finder's mention.
- **Tuning** — `CHECK_TIMEOUT`, `RESPONSE_BUDGET_SECONDS`,
  `REACTION_TIMEOUT`, `USER_MAX_CHECKS`, `USER_WINDOW_SECONDS`, and
  `RESULT_CACHE_TTL` are all in `.env`. The response budget is capped below
  five seconds deliberately.
- **Add a platform** — copy a 15-line checker in `checkers.py` (e.g. GitHub:
  `https://api.github.com/users/<name>` → 404 = free), add an emoji and add it
  to `run_all_checks`. That's it.

## 🛡️ The honest bit: limitations

- **Mojang rate-limits hard.** The cooldown + cache + fallback endpoint exist
  so your server IP doesn't get blocked. Don't lower them for a busy server.
- **guns.lol sits behind Cloudflare** and may answer `403` to datacenter IPs —
  the bot reports that as *unknown* rather than lying to you. If you choose to
  use a proxy, supply it privately through `PROXY_URL`; no proxy is bundled.
- **Discord availability is not publicly checkable** (see matrix above). The
  `🐈‍⬛` reaction only appears when you explicitly configure an authorized
  external `probe` URL; its semantics are your checker's responsibility.
- This bot **notifies** — it never auto-registers accounts, and using it to
  mass-harvest names would violate the platforms' terms. Keep it friendly.

## 🧰 Troubleshooting

| Symptom | Fix |
| ------- | --- |
| Bot online but never reacts | Enable **Message Content Intent**; check `TARGET_CHANNEL_ID`; ensure the bot can see the channel |
| No reactions + `Missing 'Add Reactions' permission` in logs | Re-invite with the permission list from Phase 2 |
| Always ⚠️ | Outbound HTTPS blocked (hosting firewall) — test with `python checkers.py Notch`, try a proxy |
| guns.lol always *blocked* | Cloudflare wall — use a residential/rotating `PROXY_URL` |
| Minecraft suddenly *blocked* | Mojang rate limit — raise `RESULT_CACHE_TTL` / lower `USER_MAX_CHECKS` (the fallback endpoint already retries once automatically) |
| `Improper token has been passed` | Re-copy the token; it must be alone on the `DISCORD_TOKEN=` line |
| Tests print `PyNaCl is not installed` | Harmless — that's Discord *voice* support, which this bot doesn't use |

## 🧪 Verifying your install (summary)

```bash
python test_checkers.py      # 22 offline tests (+ 2 skipped live tests) — should print OK
python test_bot.py           # 25 pipeline tests — should print OK
python checkers.py Notch     # live endpoints from your machine
LIVE=1 python test_checkers.py   # enables the 2 real-network tests
python bot.py                # startup banner, then post names in Discord
```
