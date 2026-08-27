# Multi-Sniper — setup and deployment guide

Complete, copy-pasteable instructions for running the bot locally and deploying it. For what the bot *does* and every configuration value, see **[README.md](README.md)**. For **free** 24/7 hosting specifically, see **[CLOUD_SETUP.md](CLOUD_SETUP.md)**.

The default configuration checks **Minecraft, guns.lol, GitHub, Steam, Reddit, Instagram, and Twitter/X**, with the Discord check switched off. Nothing beyond a bot token is required to get started.

---

## 1. Prerequisites

- **Python 3.10 or newer** (the code uses modern type-hint syntax)
- **Git**
- A Discord account, a server you can manage, and a text channel for the bot
- Outbound HTTPS (port 443) allowed from the host
- *Only for `DISCORD_CHECK_MODE=dnsrobot`:* a Chromium build installed via Playwright

---

## 2. Get the source and create an environment

```bash
git clone https://github.com/Not4Pranav/Project-006.git
cd Project-006

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows PowerShell
# py -3 -m venv .venv
# .venv\Scripts\Activate.ps1

python --version      # must be >= 3.10
```

---

## 3. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

| Package | Purpose |
|---|---|
| `discord.py` | Gateway client and REST calls |
| `aiohttp` | Async HTTP for the platform checks |
| `python-dotenv` | Loads `.env` at startup |
| `playwright` | Only used by `DISCORD_CHECK_MODE=dnsrobot` |

Playwright installs the Python package but not a browser. Install Chromium only if you plan to use `dnsrobot` mode:

```bash
python -m playwright install chromium
```

---

## 4. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → name it → **Create**.
2. Go to **Bot** → **Reset Token** → copy it. This is your `DISCORD_TOKEN`; treat it like a password.
3. Still under **Bot**, enable **Message Content Intent** under *Privileged Gateway Intents*. **The bot cannot read usernames without this** — it is the single most common setup mistake.
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot permissions: **Read Messages/View Channels**, **Send Messages** (required for the default reply mode), **Read Message History** (required to reply to a message), and **Add Reactions** (only for `RESPONSE_MODE=react`)
5. Open the generated URL and invite the bot to your server.
6. Enable **Settings → Advanced → Developer Mode**, then right-click your channel → **Copy Channel ID** for `TARGET_CHANNEL_ID`.

---

## 5. Configure `.env`

```bash
cp .env.example .env
```

Minimum viable configuration:

```env
DISCORD_TOKEN=paste-your-bot-token-here
TARGET_CHANNEL_ID=123456789012345678
```

Everything else has a working default. `.env` is git-ignored — confirm with `git status --short`, which must not list it.

> **Never commit real tokens or proxy credentials.** On a hosting provider, set these as environment variables in the dashboard rather than shipping a `.env` file.

---

## 6. Validate before going live

```bash
python -m py_compile bot.py checkers.py proxies.py && echo "compile OK"
python test_checkers.py     # 134 offline tests
python test_bot.py          # 54 pipeline tests
python test_stress.py       # 17 stress tests
```

Both suites must end in `OK`. Neither needs a Discord token or network access (three live tests are skipped unless you set `LIVE=1`).

Smoke-test the real checkers without touching Discord:

```bash
python checkers.py Notch
python checkers.py zxqw99182vlt
```

You should get a per-platform report and the reaction the bot would have added.

---

## 7. Run the bot

```bash
python bot.py
```

Expected startup banner:

```
==============================================================
🟢 MULTI-SNIPER v2.0 ONLINE as Multi-Sniper#1234
🔒 Watching channel : 123456789012345678
🕹️ Platforms        : Minecraft | guns.lol | Discord | GitHub | Steam | Reddit | Instagram | Twitter/X
🧊 Proxy            : off (direct)
⏳ User cooldown    : 5 checks / 0.50s
⚡ Response budget  : 4.50s (reaction cap 0.75s)
💾 Cache TTL        : 120s (free) / 600s (taken)
==============================================================
```

Post a bare username in the watched channel. The bot replies almost immediately and fills the list in as each platform reports:

```
Minecraft: Available
guns.lol: Unavailable
Discord: Unavailable
GitHub: Available
Steam: Unavailable
Reddit: Available
Instagram: Unknown
Twitter/X: Unavailable
```

Set `RESPONSE_MODE=react` if you would rather have emoji reactions on the original message. Stop with `Ctrl+C`.

---

## 8. Optional: add proxies

Instagram and X gate unauthenticated traffic hard, and every platform rate-limits by IP. A proxy pool spreads the eight checks in one lookup across eight different IPs.

**The quickest way — paste your vendor's list into a file:**

```bash
cp proxies.txt.example proxies.txt
# then paste your proxies in, one per line, exactly as your vendor gave them:
#   gate.example-vendor.com:7000:myuser:mypassword
#   203.0.113.9:8080
```

Then verify them before going live — this validates every line and probes each proxy concurrently:

```bash
python proxies.py            # loads proxies.txt and reports which are alive
```

That file is read automatically at startup and is gitignored, so credentials never reach a commit. Point somewhere else with `PROXY_FILE=/etc/multi-sniper/proxies.txt`, or set `PROXY_FILE=` to switch it off.

Formats are normalised for you — `host:port`, `host:port:user:pass`, `user:pass@host:port`, `user:pass:host:port` and full `http://` URLs all work, mixed freely in one file. Blank lines and `#` comments are ignored, duplicates dropped, bad lines skipped with a warning.

**Using a big public list?** Point the bot at its URL instead of storing it:

```env
PROXY_LIST_URL=https://drive.google.com/file/d/<id>/view
```

It is downloaded at startup, cached in `.proxy-cache.txt` for 6 hours, filtered of SOCKS-only ports, sampled down to `PROXY_MAX_POOL` (300), and probed — only proxies that actually answer end up serving traffic. Expect most of a free list to be dead; that is normal and handled.

Environment variables work too, and are merged with the file:

```env
PROXY_URLS=user:pass@proxy1.example:8080,user:pass@proxy2.example:8080
```

At startup the banner switches to a pool summary, and you can watch health in the logs:

```
🧊 Proxy pool       : 3 proxies | 3 alive (from proxies.txt)
   └─ http://proxy1.example:8080 (alive, 0% fail), ...
```

Behaviour worth knowing:

- A proxy is benched after **3 consecutive failures** and rejoins after a **60 s** cooldown.
- Failures are recorded from **real check traffic**, not just the 30 s health sweep.
- When every proxy is down, the pool keeps retrying proxies rather than going direct, so your real IP is not exposed. Set `PROXY_ALLOW_DIRECT_FALLBACK=true` to change that.

Validate proxy URLs before deploying — the bot refuses to start on a malformed one, and a proxy URL must not contain a path, query string, or fragment. **SOCKS proxies are not supported** and stop startup with a clear message; a silent skip would run the bot with no proxy at all and expose your real IP.

---

## 9. Optional: enable the Discord check

Discord publishes no availability API, so pick a mode consciously.

### `dnsrobot`

```env
DISCORD_CHECK_MODE=dnsrobot
```

```bash
python -m playwright install chromium          # local
python -m playwright install --with-deps chromium   # Linux servers, pulls system libs
```

A long-lived Chromium process is started once; each lookup opens a short-lived isolated context. Expect noticeably higher memory use (~300 MB+) — size your host accordingly. If Chromium is missing, the Discord result is reported as an error and every other platform keeps working.

### `account` / `account_api`

```env
DISCORD_CHECK_MODE=account
DISCORD_ACCOUNT_API_URL=            # blank uses Discord's eligibility route
DISCORD_ACCOUNT_API_TOKEN=          # only for an authorised gateway
```

### `probe`

Point the bot at a checker you control or are authorised to use. `200` = taken, `404` = free.

```env
DISCORD_CHECK_MODE=probe
DISCORD_PROBE_URL=https://my-checker.example/name/{username}
DISCORD_PROBE_TOKEN=optional-credential
```

The `{username}` placeholder is mandatory, and the URL must be `https://`. Configured credentials are sent only to their own endpoint; the Discord bot token never is.

---

## 10. Deploy for 24/7 operation

This is a **background worker**, not a web service. Do not deploy it as something that must bind a `PORT` — there is no HTTP server, and the platform's health check will kill it.

A `Procfile` is included:

```
worker: python bot.py
```

> Looking for a **free** 24/7 host? [CLOUD_SETUP.md](CLOUD_SETUP.md) covers Oracle Cloud Always Free, Render's free tier with the built-in keepalive server, and other zero-cost options.

### Render / Railway / Heroku-style

| Field | Value |
|---|---|
| Service type | Background Worker |
| Build command | `python -m pip install -r requirements.txt` |
| Start command | `python bot.py` |
| Environment | `DISCORD_TOKEN`, `TARGET_CHANNEL_ID`, plus any optional vars |

Add `&& python -m playwright install --with-deps chromium` to the build command only if `dnsrobot` is enabled.

### VPS with systemd

`/etc/systemd/system/multi-sniper.service`:

```ini
[Unit]
Description=Multi-Sniper Discord username checker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sniper
WorkingDirectory=/opt/Project-006
EnvironmentFile=/opt/Project-006/.env
ExecStart=/opt/Project-006/.venv/bin/python bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now multi-sniper
journalctl -u multi-sniper -f
```

Lock the secrets down: `chmod 600 .env && chown sniper:sniper .env`.

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t multi-sniper .
docker run -d --restart unless-stopped --env-file .env --name multi-sniper multi-sniper
```

For `dnsrobot` mode, base the image on `mcr.microsoft.com/playwright/python` instead so Chromium and its system libraries are present.

---

## 11. Operating notes

- **Logs** print one line per platform per lookup (`Minecraft  taken  HTTP 200  (vortex)`), with credentials redacted.
- **Reconnects** are normal; the startup banner prints only once, and resumes are logged at INFO.
- **Tuning latency:** `USER_WINDOW_SECONDS` controls throttling, `CHECK_TIMEOUT` controls how long a slow platform may stall a lookup, and the cache TTLs control how often names are re-fetched.
- **Reaction ordering:** with the default `STREAM_REACTIONS=true`, emojis appear as each platform answers (fastest first). Set it to `false` if you want them batched in fixed platform order instead.
- **Startup pre-warm:** `Pre-warmed 8/8 platform connections in 0.4s` in the log means the connection pool is hot; a lower count just means some hosts were unreachable at boot and will connect on demand.
- **Reducing load:** set `ENABLE_EXTRA_PLATFORMS=false` to check only Minecraft, guns.lol, and Discord.
- **Updating:**

  ```bash
  git pull
  python -m pip install -r requirements.txt
  python test_checkers.py && python test_bot.py
  sudo systemctl restart multi-sniper     # or redeploy
  ```

---

## 12. Setup troubleshooting

| Symptom | Fix |
|---|---|
| `DISCORD_TOKEN missing` on startup | `.env` is absent or the token line is blank; the bot exits before connecting on purpose |
| `Improper token has been passed` | Token was truncated or wrapped — re-copy it onto a single line with no quotes |
| Bot online but silent | **Message Content Intent** is disabled, or `TARGET_CHANNEL_ID` is the wrong channel |
| `Missing 'Add Reactions' permission` | Fix the channel permission overwrite for the bot's role |
| Every platform ⚠️ | Outbound HTTPS blocked, or all proxies down — check the banner and `benched` log lines |
| `DNS Robot browser unavailable` | `python -m playwright install --with-deps chromium` |
| Worker restarts on a hosting provider | It was deployed as a web service; switch it to a background worker |
| ⏳ during normal use | Raise `USER_MAX_CHECKS` or lower `USER_WINDOW_SECONDS` |

Still stuck? Run `python checkers.py <name>` on the host itself: it exercises the exact request path with no Discord involvement and prints the raw HTTP status behind each verdict.
