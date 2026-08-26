# ☁️ Multi-Sniper — Cloud Setup Guide

> How to run the Multi-Sniper Discord bot **24/7 on a cloud host**, step by
> step: Render, Railway, Heroku, Fly.io, or your own VPS. This is the
> deployment companion to the [README](README.md) (quick start + local setup)
> and the [blueprint](blueprint.md) (how the bot works internally).
>
> **Render users:** see [render.md](render.md) for the dedicated, step-by-step
> Render guide — it covers the full setup plus **every token/credential you
> need and exactly how to acquire each one**.

---

## Table of contents

1. [What the bot needs from a cloud host](#1-what-the-bot-needs-from-a-cloud-host)
2. [Pick a host in 30 seconds](#2-pick-a-host-in-30-seconds)
3. [Pre-flight checklist (do this first, on your machine)](#3-pre-flight-checklist-do-this-first-on-your-machine)
4. [The environment variables you will set](#4-the-environment-variables-you-will-set)
5. [Recommended: Render (Background Worker)](#5-recommended-render-background-worker)
6. [Railway](#6-railway)
7. [Heroku (worker dyno)](#7-heroku-worker-dyno)
8. [Fly.io (Machines)](#8-flyio-machines)
9. [Any VPS with systemd](#9-any-vps-with-systemd)
10. [Optional: Dockerfile & render.yaml (infra as code)](#10-optional-dockerfile--renderyaml-infra-as-code)
11. [Updating the deployed bot](#11-updating-the-deployed-bot)
12. [Monitoring, restarts & state](#12-monitoring-restarts--state)
13. [Secrets & security on cloud hosts](#13-secrets--security-on-cloud-hosts)
14. [Troubleshooting per host](#14-troubleshooting-per-host)
15. [Cost comparison table](#15-cost-comparison-table)

---

## 1. What the bot needs from a cloud host

Read this once — it drives every decision below.

| Requirement | What it means for you |
| ----------- | --------------------- |
| **Long-running process** | The bot must stay alive constantly. Anything that *sleeps* on inactivity kills it. |
| **No web server, no HTTP port** | The bot never listens. It only makes **outbound** HTTPS calls (Discord gateway/API, Mojang, guns.lol). Do **not** pick a plan type that requires an HTTP listener (e.g. Render "Web Service" with a health-check path — it will fail because nothing answers). |
| **Outbound HTTPS (port 443)** | Both the Discord WebSocket and the platform checks use TLS. No inbound firewall rules are needed at all; just don't block outbound 443. |
| **Environment variables** | All configuration comes from env vars (loaded from `.env` locally, from the host's env/secret manager in the cloud). There is no config file to edit on the server. |
| **Python 3.9+** | The code uses modern type hints. All hosts below support it. |
| **Modest RAM** | Peak usage is a few tens of MB: one aiohttp session, one Discord gateway connection, small in-memory cooldown/cache dictionaries. Every free/cheap tier has enough. |
| **State is RAM-only** | Cooldown buckets and the result cache live in memory. A restart clears them (by design — they are rate-limit protection, not data). No database, no volume, no persistent disk is required. |
| **Startup requires `DISCORD_TOKEN`** | If it's missing or blank the bot prints `❌ DISCORD_TOKEN missing…` and exits. The host will keep restarting it until you set it, so set env vars **before** the first deploy. |

The one-line summary: **you need a "worker" / background / long-running-process
host, not a "web" host**, and you need a place to store one secret.

---

## 2. Pick a host in 30 seconds

| Your situation | Pick |
| -------------- | ---- |
| Want the easiest managed setup and don't mind a few dollars | **Render** — Background Worker, **paid instance required** (~$7/mo Starter; Background Workers have **no free instance type**). For $0, see the VPS row or [render.md](render.md). |
| Want a modern dashboard + git deploys and don't mind a few dollars | **Railway** — service, `python bot.py`, $0 Free / $5 Hobby (verify current plans). |
| Already on Heroku / want the classic `Procfile` + git-push flow | **Heroku** — worker dyno (Eco $5/mo or Basic $7/mo; **no free tier** since Nov 2022). |
| Want per-second billing and a CLI-first workflow | **Fly.io** — Machines; no permanent free tier (trial credit only), cheapest always-on ≈ $2–5/mo. |
| Want full control / already own a box / Oracle-free-tier VPS | **VPS + systemd** — `Restart=always` unit file included below. |

Full comparison at the [end of this doc](#15-cost-comparison-table).

---

## 3. Pre-flight checklist (do this first, on your machine)

The fastest way to waste a deploy is to debug code on the cloud. Prove the bot
works locally *before* you touch a host:

```bash
# 1. In the repo root, create + activate a venv and install deps
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Copy the template and put in your real DISCORD_TOKEN (and any optional vars)
cp .env.example .env
#    edit .env now — see README Phase 2 for how to get the token

# 3. The full offline test suite must print OK
python test_checkers.py             # 28 offline tests (+ 2 live, skipped)
python test_bot.py                  # 28 pipeline tests

# 4. Live smoke test of the real endpoints from your machine
python checkers.py Notch            # Minecraft + guns.lol, Discord skipped

# 5. Run the bot for a minute; you should see the startup banner,
#    then react in Discord when you type a username in the watched channel
python bot.py
```

**Only when all of that passes, continue.** Also:

- ✅ Put the project in a **private** GitHub repo (`.env` is already
  git-ignored — `git status` must never show it; the tracked `.env.example`
  contains no secrets).
- ✅ Have your Discord bot token handy (Discord Developer Portal → your app →
  Bot → Reset Token).
- ✅ Have the channel ID if you want to watch a single channel
  (Discord → Settings → Advanced → Developer Mode → right-click channel →
  Copy Channel ID).

> ⚠️ **Never** paste `DISCORD_TOKEN` into a chat, issue, or log. Every host
> below has a "secret" / "environment variable" UI — that's the only place it
> belongs.

---

## 4. The environment variables you will set

The bot reads everything from env vars; the host's env panel replaces the
local `.env` file. The full annotated list is in [`.env.example`](.env.example)
— copy the variable names from there into your host's settings.

**Minimum (the bot will not run without it):**

| Variable | Example | Notes |
| -------- | ------- | ----- |
| `DISCORD_TOKEN` | `MTk4NjIyMj…` | Required. Single line, no quotes/spaces. |

**Commonly set next:**

| Variable | Blank = | Set it to |
| -------- | ------- | --------- |
| `TARGET_CHANNEL_ID` | watch every channel | the channel ID to watch (recommended) |
| `LOG_CHANNEL_ID` | no hit logging | a private channel ID for "name found free" posts |
| `DISCORD_CHECK_MODE` | `off` (safe default) | `account` for the opt-in Account API, or `probe` for an authorized checker |
| `DISCORD_ACCOUNT_API_URL` | Discord first-party eligibility route | optional HTTP(S) override with the same JSON contract |
| `DISCORD_ACCOUNT_API_TOKEN` | no credential | optional authorized API/OAuth credential; never a personal client token |
| `PROXY_URL` | direct connection | `http://user:pass@host:port` if you need one |

**Optional tuning** (`CHECK_TIMEOUT`, `RESPONSE_BUDGET_SECONDS`,
`REACTION_TIMEOUT`, `USER_MAX_CHECKS`, `USER_WINDOW_SECONDS`,
`RESULT_CACHE_TTL`) — safe defaults are built in; skip them unless you know why
you're changing them. `DISCORD_ACCOUNT_API_*` matters only in `account` mode;
`DISCORD_PROBE_*` only matters if you enable `probe`.

**Host-specific notes:**

- Values are stored as plain strings — don't wrap them in quotes.
- `DISCORD_TOKEN` and any account/probe/proxy credentials should go in the host's
  **secret** store where available (Render "Secret Files" are for files, not
  vars — just use the normal Environment panel for all of these; Railway and
  Heroku treat all vars as secrets by default).
- Multi-line values are never needed here.

---

## 5. Recommended: Render (Background Worker)

> ⭐ Complete step-by-step Render guide (incl. every token & how to get it):
> **[render.md](render.md)**.

The repo's default production path: git-connected auto-deploys and a process
type that matches the bot exactly (no web listener required). **Note:** Render's
free instances apply only to Web Services / Postgres / Key Value / static
sites — a **Background Worker requires a paid instance type** (~$7/mo Starter),
and there is no supported free path on Render for this bot. See
[render.md](render.md) for a full step-by-step guide (including every token and
how to acquire it), and §2/§15 for free alternatives.

### 5.1 Create the service

1. Sign up at [render.com](https://render.com) (GitHub login works).
2. **New + → Background Worker**.
3. Connect your private GitHub repo and select it.
4. Fill the form:
   - **Name**: `multi-sniper`
   - **Region**: closest to your Discord server (e.g. `Oregon (US West)` or `Frankfurt (EU Central)`)
   - **Runtime**: `Python 3` (latest stable is fine)
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
     (The repo's `Procfile` already declares `worker: python bot.py`, so you
     can also leave this field on its Procfile default — Render reads it.)
   - **Instance Type**: `Starter` (paid, ~$7/mo per service) — Background
     Workers have **no free instance type** as of Aug 2026
5. Click **Create Background Worker**.

### 5.2 Set environment variables

1. Open the service → **Environment** tab → **Add Environment Variable**.
2. Add at minimum:
   - `DISCORD_TOKEN` → your bot token
   - `TARGET_CHANNEL_ID` → the channel ID (optional but recommended)
3. Add any optional vars from [§4](#4-the-environment-variables-you-will-set).
4. **Save Changes** — then Render redeploys automatically.

### 5.3 Deploy & verify

1. The first deploy builds and starts automatically. Open the **Events** tab
   to watch progress, then the **Logs** tab.
2. You should see the startup banner:
   ```
   ==========================================================
   🟢 MULTI-SNIPER ONLINE as Multi-Sniper
   🔒 Watching channel : 123456789012345678
   🕹️ Platforms        : Minecraft | guns.lol | Discord (mode: off)
   ...
   ==========================================================
   ```
3. Type a bare username in the watched Discord channel → you get reactions.
4. Each check logs one line (e.g. `Minecraft available HTTP 404 (zxqw… )`).

### 5.4 Instance type & pricing (important)

- **Background Workers have no free instance type** — you must pick a paid
  instance (Starter, ~$7/mo per service) to run this bot on Render. Free
  instances exist only for Web Services, Postgres, Key Value, and static
  sites; "upgrading" your *workspace* plan does not add a free worker.
- A **paid** worker never spins down on inactivity (there's no inbound
  traffic anyway) and keeps the Discord connection alive 24/7, which is
  exactly what this bot needs.
- **Why a free Web Service won't work:** free web services spin down after
  15 minutes without inbound traffic and take ~1 minute to wake; plus Render
  expects a web service to answer an HTTP health check, which `bot.py` never
  does.
- Paid instances are billed per service per month; verify current prices on
  [Render's pricing page](https://render.com/pricing) — terms have changed
  before and may again.
- Want it $0 anyway? Use the VPS option in [§9](#9-any-vps-with-systemd)
  (e.g. Oracle Cloud Always-Free) or see the full alternatives in
  [render.md](render.md) §14.

### 5.5 Redeploys

- Push to the connected repo → Render auto-deploys (this is on by default;
  see **Settings → Deploy hooks / auto-deploy**).
- Manual redeploy: **Manual Deploy → Deploy latest commit**.

---

## 6. Railway

Git-connected deploys with a clean dashboard. No free tier until recently —
as of writing there is a $0 **Free** plan with a small monthly usage
allowance, a **Trial** with a one-time $5 credit, and a $5/mo **Hobby** plan
(1 vCPU / 0.5 GB is far more than this bot needs). Verify at
[railway.com/pricing](https://railway.app/pricing).

### 6.1 Create the service

1. Sign up at [railway.app](https://railway.app) with GitHub.
2. **New Project → Deploy from GitHub repo** → pick the private repo
   (authorize Railway to read it).
3. Railway detects Python from `requirements.txt`. Open the new service.

### 6.2 Configure

1. Service → **Settings**:
   - **Start Command**: `python bot.py`
     (Railway's Nixpacks builder can read a `Procfile`, but setting the start
     command explicitly is the reliable path.)
2. Service → **Variables** → add:
   - `DISCORD_TOKEN` → your bot token
   - `TARGET_CHANNEL_ID` → channel ID (optional)
   - any optional vars from [§4](#4-the-environment-variables-you-will-set)
3. Railway redeploys automatically on variable changes; or hit **Deploy** in
   the service's **Deployments** tab.

### 6.3 Verify

- **Deployments** tab → open the latest deployment → **View Logs**.
- Expect the startup banner, then reaction lines as you type names in Discord.

### 6.4 Notes

- Railway bills usage on top of the plan fee; a bot like this idles at
  almost nothing (a few MB of RAM, ~0% CPU), so you'll stay inside the Free
  plan's allowance or a few cents on Hobby.
- Default **Restart Policy** restarts the service if it crashes — leave it.
- Redeploys: push to the connected branch → auto-deploy (default), or
  **Deploy → Deploy latest commit** manually.

---

## 7. Heroku (worker dyno)

Heroku's free tier was retired in **November 2022** — everything below costs
money (Eco $5/mo flat with a 1000-hour/month dyno pool, or Basic $7/mo). The
repo is already Heroku-shaped: `Procfile` says `worker: python bot.py`.

### 7.1 Prepare

```bash
# Install the CLI and log in (one time)
brew install heroku/brew/heroku        # macOS; see devcenter.heroku.com for others
heroku login

# From the repo root: create the app (the name must be unique on Heroku)
heroku create multi-sniper

# Python is auto-detected from requirements.txt, but pin it explicitly:
heroku buildpacks:set heroku/python
```

### 7.2 Set config vars

```bash
heroku config:set DISCORD_TOKEN=your-token-here
heroku config:set TARGET_CHANNEL_ID=123456789012345678     # optional
# ... plus any optional vars from §4
```

### 7.3 Deploy

```bash
git push heroku main
```

Then **scale the worker up** — Heroku only starts process types you tell it
to, and this app has no `web` type:

```bash
heroku ps:scale worker=1
heroku ps                      # should show: worker=1 (Eco)
```

### 7.4 Verify & logs

```bash
heroku logs --tail --ps worker
```

You should see the startup banner, then per-check log lines. Type a username
in Discord to confirm reactions.

### 7.5 Notes

- **Eco dynos** share a 1000-hour/month pool per account — one bot worker
  running 24/7 (~720–744 h) fits; a second service won't. Eco dynos sleep
  after inactivity (that mainly affects web dynos; a worker has no traffic to
  sleep on) and can be cold-restarted by the platform.
- **Basic** ($7/mo) never sleeps and gives a dedicated dyno slot — the
  comfortable choice if you want zero surprises.
- Redeploys: `git push heroku main` again (or connect GitHub in the
  dashboard for auto-deploys).
- If a deploy says `push rejected`, you likely have uncommitted changes or
  the `.env` is being tracked — check `git status` and `.gitignore` first.

---

## 8. Fly.io (Machines)

Fly runs your app in micro-VMs ("Machines") defined by a `Dockerfile` +
`fly.toml`. There is **no permanent free tier** (free allowances were removed
in 2024; new accounts get a trial credit), and a tiny always-on machine costs
on the order of $2–5/month. CLI-first workflow.

### 8.1 One-time setup

```bash
# Install flyctl: https://fly.io/docs/flyctl/install/
fly auth login

# From the repo root — answer prompts:
#   App name : multi-sniper
#   Region   : nearest to you (e.g. ams, fra, ord)
#   ...refuse the generated Postgres/Redis, and when asked about the
#   Dockerfile, accept creating one (or add ours from §10 first).
fly launch --no-deploy
```

### 8.2 Shape `fly.toml` for a worker (no web listener)

`fly launch` generates a web-service config; this bot is a **background
worker**, so replace `fly.toml` with:

```toml
app = "multi-sniper"

[build]
  dockerfile = "Dockerfile"

[processes]
  app = "python bot.py"

[[vm]]
  size = "shared-cpu-1x"
  memory = "256mb"
```

Key points:

- `[processes] app = "python bot.py"` runs the bot as a non-HTTP process;
  the `[services]` block (HTTP routing + health checks) is deliberately
  absent — nothing listens, and Fly should not expect a port.
- 256 MB RAM is plenty for this bot.
- For a worker you want the machine to **keep running** — the generated
  web-service defaults may include scale-to-zero settings
  (`auto_stop_machines = "suspend"`). Remove them or set
  `auto_stop_machines = "off"` so Fly doesn't suspend your bot during quiet
  hours; `fly deploy` will warn you if the machine would stop.

### 8.3 Secrets & deploy

```bash
fly secrets set DISCORD_TOKEN=your-token-here
fly secrets set TARGET_CHANNEL_ID=123456789012345678     # optional

fly deploy
fly logs
```

Expect the startup banner in `fly logs`, then reaction lines as you test in
Discord. Redeploys: `fly deploy` after every push.

### 8.4 Notes

- Machines restart automatically on crash (default `restart` policy) and
  discord.py reconnects on its own.
- Billing is per-second while the machine runs; a permanently-on
  shared-cpu-1x 256 MB machine is the cheapest paid option here.
- Verify current pricing at [fly.io/pricing](https://fly.io/pricing).

---

## 9. Any VPS with systemd

A $4–6/mo VPS (Hetzner, DigitalOcean, Linode) or a free Always-Free VM
(Oracle Cloud, 1 GB ARM tier) works perfectly and gives you total control.

### 9.1 Install & prepare (Debian/Ubuntu)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo useradd -r -m -s /usr/sbin/nologin multisniper

sudo mkdir -p /opt/multisniper
sudo chown multisniper:multisniper /opt/multisniper

# as the app user:
sudo -u multisniper git clone <your-private-repo-url> /opt/multisniper/app
sudo -u multisniper python3 -m venv /opt/multisniper/venv
sudo -u multisniper /opt/multisniper/venv/bin/pip install -r /opt/multisniper/app/requirements.txt
```

### 9.2 Secrets file

Create `/etc/multisniper.env` (root-only), one `KEY=VALUE` per line, no
quotes:

```bash
sudo nano /etc/multisniper.env
# DISCORD_TOKEN=your-token-here
# TARGET_CHANNEL_ID=123456789012345678
sudo chmod 600 /etc/multisniper.env
```

### 9.3 systemd unit

Create `/etc/systemd/system/multisniper.service`:

```ini
[Unit]
Description=Multi-Sniper Discord username bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=multisniper
WorkingDirectory=/opt/multisniper/app
EnvironmentFile=/etc/multisniper.env
ExecStart=/opt/multisniper/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now multisniper
sudo systemctl status multisniper
journalctl -u multisniper -f          # follow the logs (startup banner + checks)
```

### 9.4 Updating

```bash
sudo -u multisniper git -C /opt/multisniper/app pull
sudo systemctl restart multisniper
```

### 9.5 Firewall

The bot needs **no inbound ports** (it never listens). Ensure outbound 443 is
allowed (it is by default on every major VPS). If you lock SSH to your IP,
that's the only inbound rule you need.

### 9.6 Quick tmux alternative (no systemd)

```bash
tmux new -s multisniper
source venv/bin/activate && python bot.py
# detach: Ctrl+B then D — reattach with: tmux attach -t multisniper
```

(Use `tmux new -s multisniper -d '...'` for one-liners.) Fine for testing;
systemd is better for production because it restarts the bot on crash/boot.

---

## 10. Optional: Dockerfile & render.yaml (infra as code)

Not required for Render/Railway/Heroku/VPS (they detect Python directly), but
**required for Fly.io** and handy for any container platform (Google Cloud
Run, ECS, Koyeb, …).

### 10.1 `Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py checkers.py Procfile ./

# Long-running worker: no EXPOSE, no CMD web server.
CMD ["python", "bot.py"]
```

### 10.2 `render.yaml` (optional — declarative Render service)

If you like infrastructure-as-code, this file at the repo root lets Render
provision the worker from the dashboard ("New + → Blueprint" instead of
Background Worker). `sync: false` makes Render prompt you for the secret at
deploy time:

```yaml
services:
  - type: worker
    name: multi-sniper
    runtime: python
    plan: starter           # workers have no free instance type
    buildCommand: pip install -r requirements.txt
    startCommand: python bot.py
    envVars:
      - key: DISCORD_TOKEN
        sync: false          # prompt at deploy; never stored in git
      - key: TARGET_CHANNEL_ID
        value: ""            # optional: blank = watch all channels
```

> The repo intentionally ships **no** `Dockerfile`/`render.yaml` by default —
> keep your repo minimal unless you use a container host. If you add them,
> `.gitignore` does not need changes (they contain no secrets).

---

## 11. Updating the deployed bot

The golden rule before **any** deploy:

```bash
python test_checkers.py && python test_bot.py    # both must print OK
```

| Host | Update command / flow |
| ---- | --------------------- |
| Render | `git push` to the connected branch (auto-deploy) — or dashboard **Manual Deploy** |
| Railway | `git push` (auto-deploy) — or **Deploy → Deploy latest commit** |
| Heroku | `git push heroku main` |
| Fly.io | `fly deploy` |
| VPS | `git pull` + `sudo systemctl restart multisniper` |

After any deploy, check the host's logs for the startup banner — it prints
the active configuration (channel, platforms, cooldown, budget), so a glance
confirms the new build took the settings you think it did.

---

## 12. Monitoring, restarts & state

- **Logs are your dashboard.** Every host above streams the bot's stdout:
  Render/Railway have a Logs tab, Heroku `heroku logs --tail --ps worker`,
  Fly `fly logs`, VPS `journalctl -u multisniper -f`. The startup banner +
  one line per check is the entire monitoring story for this bot.
- **Crash restarts** are handled by the platform: Render/Railway restart the
  service, Heroku restarts the dyno, Fly restarts the machine, systemd has
  `Restart=always`.
- **Discord disconnect/reconnect** is handled by discord.py itself — a
  dropped gateway connection retries with backoff and needs no intervention.
- **State is RAM-only and disposable**: per-user cooldown buckets and the
  result cache reset on restart. That's intended — they are rate-limit
  shields, not data. Do not add a database for them.
- **Uptime monitoring** (optional): a free external uptime ping only works
  if something answers HTTP — this bot never does. If you want an alert when
  the bot is down, watch the platform's health (Render/Railway status pages)
  or set up a `!ping`-style Discord command that a second service checks —
  overkill for most users; the log tab is enough.

---

## 13. Secrets & security on cloud hosts

- **The required secret is `DISCORD_TOKEN`** (plus
  `DISCORD_ACCOUNT_API_TOKEN`, `DISCORD_PROBE_TOKEN`, or proxy credentials if
  you use them).
- `.env` is git-ignored and `.env.example` ships with blank credential
  fields — `git status` should stay clean of secrets. The README, blueprint,
  and this guide contain no real tokens.
- Set secrets **before** the first deploy so the bot never starts with a
  blank token and crash-loops.
- Prefer the host's secret handling: Railway and Heroku treat all config
  vars as secret; on Render use the Environment tab (there's no separate
  "secret vars" toggle for plain env vars); on Fly use `fly secrets set`
  (never `[env]` in `fly.toml` — that file is committed).
- Rotating the token: Discord Developer Portal → Reset Token → update the
  host's env var → redeploy/restart. The old token dies immediately.
- The bot logs redact credentials (`DISCORD_ACCOUNT_API_TOKEN`,
  `DISCORD_PROBE_TOKEN`, URL user-info) — you should still never paste raw logs
  containing anything that looks like a token into public places.
- Keep the GitHub repo **private**. The bot itself needs no repository access
  at runtime — it's just the deployment source.

---

## 14. Troubleshooting per host

General symptoms first:

| Symptom | Cause → Fix |
| ------- | ----------- |
| Logs show `❌ DISCORD_TOKEN missing…` then restart loop | Env var not set / misspelled / has quotes or trailing space. Fix in host's env panel, save, redeploy. |
| Bot starts but never reacts in Discord | Message Content Intent off (Developer Portal → Bot → Privileged Gateway Intents), wrong `TARGET_CHANNEL_ID`, or bot lacks the channel perms. See README Phase 2. |
| Reactions are always ⚠️ | Outbound HTTPS blocked by the host firewall, or platforms blocking the host's IP. Test from your machine with `python checkers.py Notch`. |
| guns.lol always reports blocked/403 | Cloudflare wall on datacenter IPs — expected on some hosts; report is honest (never faked). Optional: private `PROXY_URL`. |
| Minecraft suddenly blocked | Mojang rate limit — raise `RESULT_CACHE_TTL`, lower `USER_MAX_CHECKS`. |
| `Improper token has been passed` | Token copied wrong or contains a line break — re-copy it. |

Per host:

- **Render** — *"Your service is starting…" forever*: open the **Events** tab;
  a failed build shows the pip error there. *"No free instance type" error at
  creation*: Background Workers need a paid instance — pick `Starter`.
  *Auto-deploy not firing*: check **Settings → Auto-Deploy** toggle and that
  the branch matches.
- **Railway** — *Build fails at Nixpacks*: ensure Start Command is
  `python bot.py` and `requirements.txt` is at the repo root. *"Replica
  exited"*: read the deployment logs; usually the missing-token exit above.
  *Usage alerts*: a bot like this stays near zero; if over, check you didn't
  accidentally leave multiple replicas or a database attached.
- **Heroku** — *Deployed but nothing runs*: you forgot `heroku ps:scale
  worker=1` (Heroku starts no process type by default when there's no
  `web`). *`R10` boot errors*: only apply to web dynos; irrelevant here.
  *`push rejected`*: uncommitted changes, or `.env` got committed — fix
  `.gitignore` and remove it with `git rm --cached .env`.
- **Fly.io** — *Machine keeps stopping*: your `fly.toml` still has the
  auto-stop web-service defaults; use the worker config in [§8.2](#82-shape-flytoml-for-a-worker-no-web-listener). *Deploy fails*: the Dockerfile
  must exist (`fly launch` creates one, or add ours in §10).
- **VPS** — *Bot dies and stays dead*: unit missing `Restart=always` or you
  used tmux and the session died. *`Permission denied`*: check
  `EnvironmentFile` perms (`chmod 600`) and that `User=` owns the working dir.
  *Bot starts on boot?*: `systemctl enable multisniper`.

---

## 15. Cost comparison table

*Verified August 2026 — pricing pages change; confirm before committing.*

| Host | Free option | Paid entry | Process type | Deploy flow | Good for |
| ---- | ----------- | ---------- | ------------ | ----------- | -------- |
| **Render** | ❌ no free instance for Background Workers | ~$7/mo Starter (per service) | Background Worker (no port) | GitHub auto-deploy | Most managed; must pay per service |
| **Railway** | Trial $5 credit; Free $0/mo with tiny usage allowance | $5/mo Hobby | Service (no port needed) | GitHub auto-deploy | Modern dashboard, near-free |
| **Heroku** | ❌ (removed Nov 2022) | Eco $5/mo (1000 h pool) / Basic $7/mo | Worker dyno (`Procfile`) | `git push heroku` | Already on Heroku; classic flow |
| **Fly.io** | ❌ (trial credit only) | ~$2–5/mo usage (shared-cpu-1x, 256 MB) | Machine, non-HTTP process | `fly deploy` CLI | Per-second billing, global regions |
| **VPS** | Oracle Cloud Always-Free VM; otherwise from ~$4/mo | $4–6/mo | systemd service | `git pull` + restart | Full control, unlimited hours |

**Bottom line:** run it on **Render's Background Worker (paid, ~$7/mo)** if
you want the simplest managed setup; run it on a **VPS** (Oracle Always-Free
or ~$4–6/mo) if you want $0 and don't mind owning the box; pay a few dollars
on **Railway/Fly** if you prefer their workflows. Every option uses the same
recipe: install `requirements.txt`, start `python bot.py`, set
`DISCORD_TOKEN`, watch the logs for the banner. For the complete Render
walkthrough including every token and how to acquire it, see
[render.md](render.md).
