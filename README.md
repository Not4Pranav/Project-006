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
| 🐈‍⬛ | **Free on Discord** (DNS Robot browser flow, opt-in Account API, or an explicit authorized probe confirms it) |
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
            │        └──► 🐈‍⬛ Discord mode (optional, off by default)
            │                ├─ dnsrobot: mirrors https://dnsrobot.net/username-checker?u=<name>
            │                │  with one credential-free JSON eligibility request
            │                ├─ account/account_api: configured account JSON endpoint
            │                └─ probe: explicitly authorized external 200/404 checker
            │                   (all share the same deadline)
            ▼
[ Status engine maps each response/body to free/taken/blocked ]
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
| Minecraft | 🕹️ | `https://api.mojang.com/users/profiles/minecraft/<name>` (+ `api.minecraftservices.com/minecraft/profile/lookup/name/<name>` fallback for blocked/transient primary calls) | **204 or 404** (no profile exists) | **200** (profile JSON returned) | 403 / 405 / 429 (Mojang rate limit or method block) |
| guns.lol | 🔫 | `https://guns.lol/<name>` | **404/410**, or a 200 page with the specific “username not found”/unclaimed title marker | **200** profile page without a challenge/unclaimed marker | 403 / 429 / 503, or a 200 Cloudflare challenge page |
| Discord | 🐈‍⬛ | `dnsrobot` mirrors the browser flow documented at `https://dnsrobot.net/username-checker?u=<name>`: one credential-free `POST https://discord.com/api/v9/unique-username/username-attempt-unauthed`; `account`/`account_api` use the configured account endpoint; `probe` uses its explicit URL | JSON `{"taken": false}` | JSON `{"taken": true}` | 401 / 403 / 429, malformed response, or network failure |

> **Target naming note:** At implementation time, [http://Gung.lol](http://Gung.lol)
> is a parked domain rather than a profile-availability service. This project therefore
> checks the active [guns.lol](https://guns.lol) profile platform; it does not
> pretend that the parked `Gung.lol` domain can answer username availability.

Every check also validates the name against the platform's rules *before*
sending a request (Minecraft: `3–16` chars `A-Za-z0-9_`; guns.lol: `2–24` chars
`A-Za-z0-9._-`; Discord: `2–32` chars lowercase `a-z0-9._`), so impossible names
are reported as **invalid** without wasting a request.

**About the Discord check (important):** Discord’s public bot API does not
provide a username search endpoint. DNS Robot’s
`https://dnsrobot.net/username-checker` is a browser UI: its published page
loads `?u=<name>` and then makes one credential-free browser `POST` to
Discord’s `unique-username/username-attempt-unauthed` route. To get the result
as quickly as possible, `DISCORD_CHECK_MODE=dnsrobot` mirrors that exact
browser-flow request instead of launching a full browser or scraping a page.
It uses DNS Robot as the documented source/origin, never calls
`discord.com/<username>`, and never forwards the bot, account, or probe token.

The DNS Robot browser flow is still subject to Discord’s network and anti-bot
rules. An HTTP 403/429, malformed response, or network failure is reported as
unknown, not available. The mode is off by default; use it only for a
reasonable, non-automated lookup rate and confirm candidates in Discord’s own
UI before attempting a rename.

The `account` mode remains available for an explicitly authorized account/API
integration. It sends a JSON `POST` containing the candidate name and reads the
strict boolean-style response (`taken: true` means TAKEN; `taken: false` means
FREE). It never uses the bot token for this request and never calls the
username-claim endpoint.

The account route is not a general-purpose public bot API and Discord may
restrict it or return a challenge/rate limit. An `HTTP 200` without a strict
boolean response is reported as unknown, not taken or free. The mode is off by
default; enable it only for an authorized account/API integration and follow
Discord’s current policies. The default URL is
`https://discord.com/api/v10/unique-username/username-attempt-unauthed`; set
`DISCORD_ACCOUNT_API_URL` only when your authorized gateway exposes the same
`{"username": "..."}` → `{"taken": true|false}` contract. For an authorized
account-scoped endpoint, configure its URL explicitly (for example
`https://discord.com/api/v10/users/@me/pomelo-attempt`) and use only the
credential type that endpoint documents; never paste a personal client token
into this bot.

The older `probe` mode remains available for an external GET checker. Its
contract is **200 = taken**, **404 = free**, and **401/403/429 = unknown**. If
that service needs credentials, put them only in the private
`DISCORD_PROBE_TOKEN` environment value; the bot sends it only to that endpoint
and never logs it.

## 📁 Project layout

```
.
├── bot.py            # the runtime: gateway events, filters, cooldown, reactions
├── checkers.py       # platform registry + parallel HTTP checks (+ CLI self-test)
├── blueprint.md      # technical deep-dive: how every stage works internally
├── CLOUD_SETUP.md    # detailed 24/7 cloud deployment guide (Render, Railway, Heroku, Fly.io, VPS)
├── test_checkers.py  # 31 offline checker tests + 2 optional LIVE=1 network tests
├── test_bot.py       # 29 end-to-end pipeline tests (simulated Discord messages)
├── .env.example      # copy to .env and fill in your secrets
├── requirements.txt  # discord.py, aiohttp, python-dotenv
├── Procfile          # cloud deployment start command
└── .gitignore        # keeps your .env out of git
```

---

## 🚀 Quick start (copy-paste version)

**You need three things before you start:**

1. **Python 3.9 or newer** (the bot uses modern type hints; check with `python --version`).
2. **A Discord bot application + token** from the [Discord Developer Portal](https://discord.com/developers/applications).
3. **A channel you own** where you want the bot to watch usernames.

Run these from the repo root:

```bash
# 1. Clone / enter the repo
git clone <this repo> && cd Project-006

# 2. Create an isolated environment (keeps packages out of your system Python)
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your private config file
cp .env.example .env

# 5. Open .env, paste your bot token into DISCORD_TOKEN=, then start the bot
python bot.py
```

> **Do not skip the virtual environment.** `requirements.txt` constrains the
> Discord, aiohttp, and dotenv versions; an isolated `venv` avoids conflicts
> with other projects.
>
> **`venv` and `.env` are already git-ignored** — go ahead and use them locally
> without worrying about committing secrets.

If you want to see the bot work in a channel, continue with the numbered phases
below. Each phase is deliberately small so you can verify it before moving on.

### The two-minute "no Discord token" smoke test

You do not **need** a Discord token to verify the install. The checkers and the
full offline test suite run without connecting to Discord at all:

```bash
python checkers.py Notch                # live: Minecraft + guns.lol, Discord skipped by default
python test_checkers.py                 # 31 offline tests (+ 2 LIVE tests skipped)
python test_bot.py                      # 29 end-to-end pipeline tests
```

If those print `OK`, the Python environment is set up correctly; the only thing
left is the Discord token and intents described in [Phase 2](#phase-2--discord-developer-portal).

## Phase 1 — Local setup (do this once)

The fast path above is enough, but here is what each command does so you can
troubleshoot it:

```bash
# 1. Create a virtual environment named "venv" (only needed once).
python -m venv venv

# 2. Activate it. Every shell you use for this project must be activated first.
source venv/bin/activate          # macOS / Linux
venv\Scripts\activate             # Windows (cmd)
venv\Scripts\Activate.ps1         # Windows (PowerShell)

# 3. Confirm you are using the venv (should print a venv path, not /usr/bin/python).
python --version
which python                       # macOS/Linux
where python                       # Windows

# 4. Install the runtime dependencies (version constraints are in requirements.txt).
pip install -r requirements.txt

# 5. (Optional but useful) fast byte-code compilation check of every file.
python -m py_compile bot.py checkers.py test_bot.py test_checkers.py && echo "compile OK"
```

**What gets installed (from `requirements.txt`):**

| Package | Why it is here |
| ------- | -------------- |
| `discord.py>=2.3,<3` | Discord gateway + `messageCreate` events, REST reactions |
| `aiohttp>=3.9,<4` | Async HTTP clients for the Minecraft / guns.lol / DNS Robot browser-flow / Account API / probe checks |
| `python-dotenv>=1.0,<2` | Loads `.env` into environment variables at startup |

### Common Phase 1 problems

| Problem | Fix |
| ------- | --- |
| `command not found: python` (macOS) | Use `python3`, or install Python 3.9+ from [python.org](https://www.python.org). |
| `No module named pip` | Reinstall Python with pip, or run `python -m ensurepip`. |
| `ERROR: Could not install packages` | Try `python -m pip install --upgrade pip`, then re-run `pip install -r requirements.txt`. |
| Command still uses the old Python | Re-activate the venv (`source venv/bin/activate`) and run `which python`. |

## Phase 2 — Create the Discord application (do this once)

The bot needs a Discord "application" with a **bot user**, a **token**, and the
**Message Content intent**. It also needs to be **invited** to your server with
the right permissions.

1. **Create the application**
   Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application** → name it (e.g. *Multi-Sniper*) → **Create**. You'll
   land on the application's *General Information* page.

2. **Create the bot user**
   In the left sidebar → **Bot** → **Add Bot** → confirm. You now have a bot
   account with a name and icon.

3. **Copy the token**
   On the same **Bot** page → **Reset Token** → copy it. This is your
   `DISCORD_TOKEN`. **Never** share it, commit it, or paste it into this README;
   it is the password to the bot.

4. **Enable the Message Content intent (critical)**
   On the same **Bot** page, scroll to **Privileged Gateway Intents** and turn
   **Message Content Intent ON**. Without this the bot receives messages as
   empty content and will never react. (You do **not** need *Presence* or *Server
   Members*.)

5. **Generate an invite link**
   Left sidebar → **OAuth2 → URL Generator**:
   - **Scopes**: tick `bot`
   - **Bot permissions**: tick
     - `Read Messages/View Channels`
     - `Send Messages`
     - `Add Reactions`
     - *(optional, for future auto-clean features): `Manage Messages`*
   - Copy the generated URL at the bottom.

6. **Invite the bot**
   Open the copied URL in a browser → select your server → **Authorise** →
   complete any CAPTCHA. The bot should now appear in your server's member list.

7. **Find the channel IDs (optional but recommended)**
   Discord → **Settings → Advanced → Developer Mode ON**.
   - Right-click the target channel → **Copy Channel ID** → put it in
     `TARGET_CHANNEL_ID`.
   - If you want hit logging, copy the log channel ID into `LOG_CHANNEL_ID`.

> **Common mistake:** creating the app but never clicking **Add Bot**. There is
> no token to copy until the bot user exists.
> **Checklist:** Bot exists → token copied → Message Content Intent ON → bot is
> actually in your server.

## Phase 3 — Configure the bot

`bot.py` reads its settings from environment variables. Locally these come from a
file named `.env`, which the bot loads automatically at startup (via
`python-dotenv`). Create it from the tracked template:

```bash
cp .env.example .env
```

> **Never** commit `.env`. The template `DISCORD_TOKEN=` is deliberately blank —
> it is not secret and is safe to keep in git. Any real token, probe token, or
> proxy credential belongs only in your private `.env` (or your host's secret
> vault).

### The absolute minimum `.env` for a working bot

```dotenv
DISCORD_TOKEN=your-bot-token-here          # REQUIRED
TARGET_CHANNEL_ID=123456789012345678      # optional; blank = every channel
```

That is all the bot needs. Everything below has a safe default.

### Every setting (with defaults and how the bot validates them)

| Variable | Required | Default | Allowed / clamped values | What it does |
| -------- | :------: | ------- | ------------------------ | ------------ |
| `DISCORD_TOKEN` | ✅ | — | non-empty, no line break | Bot token from Phase 2, step 3. Missing/blank → bot exits at startup. |
| `TARGET_CHANNEL_ID` | — | *(all channels)* | Snowflake ID (dev mode → *Copy Channel ID*) | Only react to messages in this channel. Blank = watch every channel the bot can see. |
| `LOG_CHANNEL_ID` | — | off | Snowflake ID, or blank | When set, every name found free is posted to this channel. Blank = off. |
| `DISCORD_CHECK_MODE` | — | `off` | `off`, `dnsrobot`, `account`, `account_api` (compatibility alias), or `probe` (case-insensitive) | `off` = skip Discord. `dnsrobot` = mirror DNS Robot's credential-free browser flow. `account` = POST the account API JSON contract. `probe` = query your own authorized checker URL. |
| `DISCORD_ACCOUNT_API_URL` | — | Discord first-party eligibility route | absolute HTTP(S) URL | Optional override for the account API; it must accept `{"username": "..."}` and return a strict boolean result. |
| `DISCORD_ACCOUNT_API_TOKEN` | — | blank | any string, no CR/LF | Optional credential sent **only** to `DISCORD_ACCOUNT_API_URL`; never reuse `DISCORD_TOKEN` or a personal client token. |
| `DISCORD_ACCOUNT_API_TOKEN_HEADER` | — | `Authorization` | valid HTTP header name | Header that carries the authorized account API credential. |
| `DISCORD_ACCOUNT_API_TOKEN_SCHEME` | — | `Bearer` | string or blank | Prefix before the account API credential; blank sends it raw. |
| `DISCORD_PROBE_URL` | — | blank | HTTP(S) URL template containing `{username}` | The external GET checker used with `DISCORD_CHECK_MODE=probe`; *never* defaults to `discord.com`. |
| `DISCORD_PROBE_TOKEN` | — | blank | any string, no CR/LF | Optional credential sent **only** to `DISCORD_PROBE_URL`. Never logged or written to tracked files. |
| `DISCORD_PROBE_TOKEN_HEADER` | — | `Authorization` | valid HTTP header name (e.g. `X-API-Key`) | Header that carries the probe token. |
| `DISCORD_PROBE_TOKEN_SCHEME` | — | `Bearer` | string or blank | Prefix before the probe token; blank sends the raw token with no scheme. |
| `PROXY_URL` | — | direct | valid http/https/https-with-credentials URL | Route all outbound checks through this proxy. Validated at startup; rejected before connecting if malformed. |
| `CHECK_TIMEOUT` | — | `3` | float, clamped to `[0.05, RESPONSE_BUDGET_SECONDS]` | Per-outbound-request timeout (seconds) for each platform check. |
| `RESPONSE_BUDGET_SECONDS` | — | `4.5` | float, clamped to `[0.5, 4.8]` | Hard wall-clock budget from a valid message through checks **and** reactions. Kept under 5 s on purpose. |
| `REACTION_TIMEOUT` | — | `0.75` | float, clamped to `[0.05, budget − 0.05]` | Cap for each Discord reaction REST call; free-platform reactions run concurrently. |
| `USER_MAX_CHECKS` | — | `3` | int, `1`–`10000` | Most checks one user may fire in a cooldown window before getting ⏳. |
| `USER_WINDOW_SECONDS` | — | `60` | float ≥ `0.1` | Length of the per-user cooldown window. |
| `RESULT_CACHE_TTL` | — | `300` | float ≥ `0` | Reuse a previous answer for a name for this many seconds (rate-limit shield). |

**How validation works:** startup checks the *required* token, the
`DISCORD_CHECK_MODE` value, `PROXY_URL`, the account API URL, the probe URL
template, and any configured auth header **before** connecting to Discord.
Malformed or non-finite numeric values fall back to a safe default rather than
crashing mid-run. Credential-containing values are redacted from log output.

### Example: enable the DNS Robot browser-flow check

```dotenv
DISCORD_CHECK_MODE=dnsrobot
```

No DNS Robot token, account token, or extra URL is required. The adapter mirrors
what the page does in a browser: it sends the candidate as JSON to Discord with
DNS Robot's page origin and referer. This avoids a slow headless-browser launch
while preserving the page's current transport. If Discord blocks the hosting
IP, the bot reacts with ⚠️/a partial result rather than treating the name as
available.

### Example: enable the Discord Account API check

```dotenv
DISCORD_CHECK_MODE=account
# Blank uses Discord's first-party account eligibility route.
DISCORD_ACCOUNT_API_URL=
# Leave blank unless an authorized gateway requires an API/OAuth credential.
DISCORD_ACCOUNT_API_TOKEN=keep-me-private
DISCORD_ACCOUNT_API_TOKEN_HEADER=Authorization
DISCORD_ACCOUNT_API_TOKEN_SCHEME=Bearer
```

The account request is a JSON `POST` with `{"username": "candidate"}`. A strict
`{"taken": false}` response adds 🐈‍⬛; `{"taken": true}` does not.

### Example: enable the optional Discord probe

```dotenv
DISCORD_CHECK_MODE=probe
DISCORD_PROBE_URL=https://my-checker.example/name/{username}
DISCORD_PROBE_TOKEN=keep-me-private
DISCORD_PROBE_TOKEN_HEADER=X-API-Key
DISCORD_PROBE_TOKEN_SCHEME=
```

> Only set these if you actually run an authorized checker service. See
> [the honest bit](#-the-honest-bit-limitations) for why Discord is off by default.

## Phase 4 — Run & test

### 4a. First run (and what you should see)

```bash
source venv/bin/activate     # if you opened a new terminal
python bot.py
```

On success you'll get a startup banner listing what the bot is configured to do:

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

Keep it running while you test. Stop it with `Ctrl+C`.

### 4b. Behavior in your channel

Type a single bare username (no spaces) in the watched channel:

| You send | Expected reaction | Why |
| -------- | ----------------- | --- |
| `Notch` | ❌ | Taken on Minecraft and guns.lol |
| `zxqw_99182vlt` | 🕹️ 🔫 | Free on both |
| two words / a sentence | *(no reaction)* | Not a bare username — filtered out |
| same user again within 60 s (past 3 checks) | ⏳ | Cooldown reached |

A name that one platform rejects as impossible (e.g. Minecraft names shorter
than 3 chars) never sends an HTTP request and is treated as **invalid** for that
platform.

### 4c. Run the offline test suite

The tests need **no Discord token and make no network calls** (except the two
live tests that are skipped by default):

```bash
python test_checkers.py      # 31 offline checker tests (+ 2 live tests skipped)
python test_bot.py           # 29 end-to-end pipeline tests
```

Both should end with `OK`. The two live tests run only when you ask for them:

```bash
LIVE=1 python test_checkers.py
```

If every test prints `OK`, the environment is correct and the only remaining
variable is your Discord setup, not the code.

## Phase 5 — 24/7 hosting on Render (free tier)

A Discord bot only runs while its process is alive, so for always-on behavior
you run it on a host. Render's free **Background Worker** is the default path
for this repo; Railway and any Python 3.9+ VPS work the same way.

> **Full walkthroughs for five hosts** (Render, Railway, Heroku, Fly.io, VPS +
> systemd — including a Dockerfile, a `render.yaml` blueprint, secrets
> handling, redeploys, and per-host troubleshooting) live in
> [CLOUD_SETUP.md](CLOUD_SETUP.md). The steps below are the quick Render path.

### Render, step by step

1. **Push to a private repo.**
   Create a private GitHub repo and push this project (`.env` is already
   git-ignored; the tracked `.env.example` contains *no* credentials).

2. **Create the service.**
   On [Render](https://render.com) → **New + → Background Worker** → connect
   your private repo.

3. **Build command**
   ```bash
   pip install -r requirements.txt
   ```

4. **Start command**
   ```bash
   python bot.py
   ```
   The included `Procfile` already declares exactly this (`worker: python bot.py`),
   so you can also leave the start command field on its Procfile default.

5. **Set environment variables.**
   In Render's **Environment** tab, add at minimum:
   - `DISCORD_TOKEN` → your bot token
   - `TARGET_CHANNEL_ID` → the channel to watch (optional; blank = all channels)

   Add any optional vars from [Phase 3](#phase-3--configure-the-bot)
   (`LOG_CHANNEL_ID`, `PROXY_URL`, timeouts, etc.) there too. Never put these in
   git.

6. **Deploy & observe.**
   Trigger **Deploy**, then open the **Logs** tab. You should see the startup
   banner and, after you type a name in Discord, one line per platform check.
   Background workers have no public URL; the logs are where you watch it.

### Other hosts (same pattern)

| Host | Service type | Start command | Notes |
| ---- | ------------ | ------------- | ----- |
| [Render](https://render.com) | Background Worker | `python bot.py` | Free tier; use the `Procfile` |
| [Railway](https://railway.app) | Service | `python bot.py` | Add env vars in the dashboard |
| [Fly.io](https://fly.io) | `fly launch` + `fly deploy` | `python bot.py` | Add `[processes] app = "python bot.py"` |
| Any VPS | systemd / tmux / screen | `python bot.py` | `pip install -r requirements.txt` first |

> **Do not** use a host that requires a listening HTTP port for a background
> worker — this is a long-running *worker*, not a web service.

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
Ran 33 tests in 0.03s
OK (skipped=2)      <- the 2 live tests run only with LIVE=1
Ran 29 tests in 0.14s
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
- **DNS Robot browser flow** — set `DISCORD_CHECK_MODE=dnsrobot` to mirror the
  request made by `https://dnsrobot.net/username-checker?u=<name>`. It is the
  fastest server-compatible path because DNS Robot itself checks Discord from
  the browser with one credential-free JSON request; no headless browser or
  DNS Robot credential is needed. Discord blocks/rate limits remain unknown.
- **Discord Account API** — set `DISCORD_CHECK_MODE=account` to POST the
  candidate to the account eligibility route. The adapter reads strict JSON
  `taken`/`available` booleans, treats malformed responses as unknown, and
  never sends `DISCORD_TOKEN`. Keep the mode off unless the endpoint is
  authorized for your use.
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
- **DNS Robot is a browser UI, not a DNS Robot server API.** Its published
  page checks Discord from the visitor's browser. The opt-in `dnsrobot` mode
  mirrors that one credential-free request with the page's origin/referer so
  the bot gets the result without a headless-browser startup. A hosting IP
  blocked by Discord still produces ⚠️/unknown, never FREE.
- **Discord availability is not exposed through the public bot API.** The
  opt-in `account` mode uses Discord’s account-flow eligibility endpoint (or an
  explicitly configured compatible gateway), reads only its JSON boolean, and
  reports 401/403/429 or malformed responses as unknown. It never uses a
  personal client token, sends the bot token, or calls the claim endpoint.
- **The DNS Robot/account endpoint may be restricted or change.** Keep the
  result as a hint and confirm availability in Discord’s own UI before
  attempting a rename.
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
| DNS Robot always *blocked* or *unknown* | DNS Robot checks from a browser, and Discord may reject datacenter IPs or change the route. Keep the result unknown; do not add a personal client token. |
| Account API always *blocked* or *unknown* | The first-party route is restricted on some hosts; keep the result unknown, verify the endpoint/policy, or use an authorized compatible gateway. Never substitute a personal client token. |
| `Improper token has been passed` | Re-copy the token; it must be alone on the `DISCORD_TOKEN=` line |
| Tests print `PyNaCl is not installed` | Harmless — that's Discord *voice* support, which this bot doesn't use |

## 🧪 Verifying your install (summary)

```bash
python test_checkers.py      # 31 offline tests (+ 2 skipped live tests) — should print OK
python test_bot.py           # 29 pipeline tests — should print OK
python checkers.py Notch           # live endpoints; Discord is skipped by default
python checkers.py vortex --mode dnsrobot  # DNS Robot browser-flow check
python checkers.py vortex --mode account    # opt-in Account API check
LIVE=1 python test_checkers.py   # enables the 2 real-network tests
python bot.py                # startup banner, then post names in Discord
```
