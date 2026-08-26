# 🎯 Multi-Sniper — Discord Username Availability Bot

A Discord bot that works like a username sniper's scout: when a member posts a
name in the watched channel, the bot checks **Minecraft**, **guns.lol** and
(optionally) **Discord** in parallel — all within a ~1–5 second window — and
reacts to the message with one emoji per platform where the name is **FREE**.

| Reaction | Meaning |
| -------- | ------- |
| 🕹️ | **Free on Minecraft** (Mojang has no profile with that name) |
| 🔫 | **Free on guns.lol** (no profile page exists) |
| 🐈‍⬛ | Free on Discord *(best-effort probe only — see [the honest bit](#-the-honest-bit-limitations))* |
| ❌ | Not available on any checked platform |
| ⚠️ | Every check failed (network down / IP blocked) |
| ⏳ | User is checking too fast (cooldown) |

---

## 🗺️ How it works (system topology)

```
[ Member types "vortex" in the watched channel ]
            │
            ▼  (Discord WebSocket gateway → messageCreate event)
[ bot.py  ·  SniperBot ]
            │
            ├──► 1. Filter: ignore bots, wrong channel, multi-word/invalid input
            ├──► 2. Cooldown: ⏳ if the user is checking too fast
            ├──► 3. Cache:   reuse results checked in the last 5 minutes
            │
            ├──► 4. Parallel fan-out (asyncio.gather — all at once, not one-by-one)
            │        ├──► 🕹️ GET https://api.mojang.com/users/profiles/minecraft/<name>
            │        ├──► 🔫 GET https://guns.lol/<name>
            │        └──► 🐈‍⬛ GET <discord probe URL>          (optional, off by default)
            │                └─ each request: 3s timeout, browser headers,
            │                   optional proxy, never blocks the event loop
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
| Minecraft | 🕹️ | `https://api.mojang.com/users/profiles/minecraft/<name>` | **204 or 404** (no profile exists) | **200** (profile JSON returned) | 403 / 429 (Mojang rate limit) |
| guns.lol | 🔫 | `https://guns.lol/<name>` | **404** (no profile page) | **200** (page renders) | 403 / 503 (Cloudflare bot wall) |
| Discord | 🐈‍⬛ | *no public API exists* — see note below | (probe: 404) | (probe: 200/401) | 429 |

**About the Discord check (important):** Discord does **not** expose any public
endpoint for checking whether an arbitrary username is available — that was
confirmed at the very start of the original thread, and the
`https://discord.com{username}` idea that appeared later in it is not a real
API (it just serves Discord's homepage). Attempting to claim-check names
requires a logged-in user session, which violates Discord's ToS. So this bot
ships the Discord check **disabled** (`DISCORD_CHECK_MODE=off`). If you set it
to `probe`, it performs a clearly-labelled best-effort GET against
`DISCORD_PROBE_URL` using the blueprint's status mapping — don't trust it.

## 📁 Project layout

```
.
├── bot.py            # the runtime: gateway events, filters, cooldown, reactions
├── checkers.py       # platform registry + parallel HTTP checks (+ CLI self-test)
├── test_checkers.py  # offline unit tests (no network, no Discord needed)
├── .env.example      # copy to .env and fill in your secrets
├── requirements.txt  # discord.py, aiohttp, python-dotenv
├── Procfile          # cloud deployment start command
└── .gitignore        # keeps your .env out of git
```

---

## Phase 1 — Local setup

```bash
# 1. get the code and enter the folder
cd Project-006

# 2. create + activate an isolated environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. install dependencies
pip install -r requirements.txt

# 4. sanity-check the checkers from your own machine (no bot needed)
python checkers.py Notch          # famous name -> Minecraft should show [TAKEN]
python checkers.py zxqw99182vlt   # random junk -> should show [FREE]

# 5. run the offline test suite
python test_checkers.py           # 14 tests, all should pass
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

Edit `.env`:

```env
DISCORD_TOKEN=paste_your_real_token
TARGET_CHANNEL_ID=123456789012345678   # blank = watch all channels
LOG_CHANNEL_ID=                        # optional hits-log channel
DISCORD_CHECK_MODE=off                 # 'probe' = unofficial best-effort
PROXY_URL=                             # optional http://user:pass@host:port
```

> To copy a channel ID: Discord **Settings → Advanced → Developer Mode ON**,
> then right-click the channel → **Copy Channel ID**.

## Phase 4 — Run & test

```bash
python bot.py
```

You should see:

```
==========================================================
🟢 MULTI-SNIPER ONLINE as Multi-Sniper#....
🔒 Watching channel : 123456789012345678
🕹️ Platforms        : Minecraft | guns.lol | Discord (mode: off)
🧊 Proxy            : off (direct)
⏳ User cooldown    : 3 checks / 60s
==========================================================
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

## 🔧 Optional features

- **Rotating proxy** — set `PROXY_URL=http://user:pass@backconnect-host:port`
  and every outbound check rides a fresh exit IP. HTTP(S) proxies are built in;
  SOCKS needs `pip install aiohttp-socks` plus a small code change.
- **Hits logging** — set `LOG_CHANNEL_ID` and every name found free is posted
  to that channel with the finder's mention.
- **Tuning** — `CHECK_TIMEOUT`, `USER_MAX_CHECKS`, `USER_WINDOW_SECONDS`,
  `RESULT_CACHE_TTL` are all in `.env`.
- **Add a platform** — copy a 15-line checker in `checkers.py` (e.g. GitHub:
  `https://api.github.com/users/<name>` → 404 = free), add an emoji and add it
  to `run_all_checks`. That's it.

## 🛡️ The honest bit: limitations

- **Mojang rate-limits hard.** The cooldown + cache exist so your server IP
  doesn't get blocked. Don't lower them for a busy server.
- **guns.lol sits behind Cloudflare** and may answer `403` to datacenter IPs —
  the bot reports that as *unknown* rather than lying to you. A residential/
  rotating proxy usually helps.
- **Discord availability is not publicly checkable** (see matrix above). The
  `🐈‍⬛` reaction only appears in unofficial `probe` mode and may be wrong.
- This bot **notifies** — it never auto-registers accounts, and using it to
  mass-harvest names would violate the platforms' terms. Keep it friendly.

## 🧰 Troubleshooting

| Symptom | Fix |
| ------- | --- |
| Bot online but never reacts | Enable **Message Content Intent**; check `TARGET_CHANNEL_ID`; ensure the bot can see the channel |
| No reactions + `Missing 'Add Reactions' permission` in logs | Re-invite with the permission list from Phase 2 |
| Always ⚠️ | Outbound HTTPS blocked (hosting firewall) — test with `python checkers.py Notch`, try a proxy |
| guns.lol always *blocked* | Cloudflare wall — use a residential/rotating `PROXY_URL` |
| Minecraft suddenly *blocked* | Mojang rate limit — raise `RESULT_CACHE_TTL` / lower `USER_MAX_CHECKS` |
| `Improper token has been passed` | Re-copy the token; it must be alone on the `DISCORD_TOKEN=` line |
