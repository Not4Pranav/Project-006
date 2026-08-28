# Free 24/7 hosting guide

How to keep Multi-Sniper online around the clock **without paying anything**. Every option here is genuinely free — no trial credits that expire, no card charges — with the catches stated up front.

New to the bot? Do [SETUP.md](SETUP.md) first and get it running on your own machine. This guide only covers making it permanent.

> Free-tier terms change. Everything below was verified in 2026, but check the provider's current pricing page before you commit.

---

## Pick your option

| Option | Truly free? | Effort | Reliability | Best for |
|---|---|---|---|---|
| **[A. Oracle Cloud Always Free](#option-a-oracle-cloud-always-free-recommended)** | Yes, indefinitely | Medium | ★★★★★ | The best free option overall — a real VPS |
| **[B. Render free web service](#option-b-render-free-tier--keepalive)** | Yes, with a keepalive ping | Low | ★★★☆☆ | Fastest to set up, no Linux knowledge |
| **[C. Koyeb / Fly.io style hosts](#option-c-other-free-paas-hosts)** | Usually | Low | ★★★☆☆ | Alternatives if Render is full |
| **[D. Free Discord bot hosts](#option-d-dedicated-free-discord-bot-hosts)** | Yes, with queues/ads | Very low | ★★☆☆☆ | Zero-config, accepts the limits |
| **[E. Your own hardware](#option-e-a-spare-machine-or-raspberry-pi)** | Yes (electricity aside) | Low | ★★★★☆ | You have a Pi or an old laptop |

**Short answer:** if you can spend 30 minutes, use **Option A**. If you want it running in 10 minutes, use **Option B**.

### The one thing that makes free hosting work

Most free tiers only host **web services** — something that binds an HTTP port — and they shut down anything that looks idle. This bot has a built-in solution:

```env
PORT=8080          # or KEEPALIVE_PORT=8080
```

Set that, and the bot starts a tiny HTTP health endpoint alongside the Discord client. Free hosts see a live web service; an external pinger keeps it awake. The endpoint exposes no secrets and no controls — just status:

```json
{"status":"ok","bot":"Multi-Sniper#1234","uptime_seconds":86400.0,
 "checks_served":1423,"cached_names":312,"proxies_alive":0}
```

You do **not** need this on Option A or E.

---

## Option A: Oracle Cloud Always Free (recommended)

A real always-on Linux VPS, free forever, no ping tricks. As of 2026 the Always Free tier includes Arm-based Ampere A1 capacity (new accounts typically get 2 OCPU / 12 GB; older ones 4 / 24) **plus** two tiny AMD micro instances, 200 GB of block storage and 10 TB/month of outbound traffic. This bot idles at well under 200 MB, so even the smallest shape is overkill.

**Catches, honestly:** signup asks for a card (used for identity verification, not charged while you stay in Always Free), instance capacity in popular regions can be scarce, and Oracle may reclaim instances that look permanently idle. A running Discord bot generates enough activity to be fine.

### A1. Create the instance

1. Sign up at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) and pick your **home region carefully** — it cannot be changed later. Choose one near you with capacity.
2. Console → **Compute → Instances → Create instance**.
3. **Image:** Ubuntu 22.04 or 24.04 (Minimal is fine).
4. **Shape:** `VM.Standard.A1.Flex` with 1 OCPU / 6 GB is plenty. If Arm capacity is unavailable, use `VM.Standard.E2.1.Micro` (AMD, 1 GB) — still enough.
5. **SSH keys:** *Generate a key pair* and **download the private key** before creating. You cannot retrieve it later.
6. Create, then copy the instance's **public IP**.

> "Out of capacity" errors are common on Arm. Try a different availability domain, try again a few hours later, or use the AMD micro shape.

### A2. Connect and install

```bash
chmod 600 ~/Downloads/ssh-key.key
ssh -i ~/Downloads/ssh-key.key ubuntu@YOUR_PUBLIC_IP

sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
python3 --version        # needs >= 3.10
```

### A3. Deploy the bot

```bash
sudo useradd -r -m -s /bin/bash sniper
sudo -u sniper -H bash -c '
  cd ~ &&
  git clone https://github.com/Not4Pranav/Project-006.git &&
  cd Project-006 &&
  python3 -m venv .venv &&
  .venv/bin/pip install --upgrade pip &&
  .venv/bin/pip install -r requirements.txt
'
```

Create the config as that user:

```bash
sudo -u sniper -H nano /home/sniper/Project-006/.env
```

```env
DISCORD_TOKEN=your-bot-token
TARGET_CHANNEL_ID=123456789012345678
RESPONSE_MODE=reply
```

Lock it down and verify:

```bash
sudo chmod 600 /home/sniper/Project-006/.env
cd /home/sniper/Project-006
sudo -u sniper .venv/bin/python test_checkers.py
sudo -u sniper .venv/bin/python test_bot.py
```

### A4. Run it forever with systemd

```bash
sudo nano /etc/systemd/system/multi-sniper.service
```

```ini
[Unit]
Description=Multi-Sniper Discord username checker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sniper
WorkingDirectory=/home/sniper/Project-006
EnvironmentFile=/home/sniper/Project-006/.env
ExecStart=/home/sniper/Project-006/.venv/bin/python bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Proxy verification probes up to 1,000 proxies at once (in the background
# after login), and each in-flight probe holds a file descriptor. systemd's
# default is often 1,024.
LimitNOFILE=16384

# Hardening: the bot needs no privileges beyond outbound HTTPS.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/sniper/Project-006

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now multi-sniper
sudo systemctl status multi-sniper
journalctl -u multi-sniper -f      # live logs, Ctrl+C to detach
```

It now survives crashes **and reboots**. Done — no keepalive, no pinging.

### A5. Keep it healthy

```bash
# Automatic security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades

# Update the bot later
sudo -u sniper -H bash -c 'cd ~/Project-006 && git pull && .venv/bin/pip install -r requirements.txt'
sudo systemctl restart multi-sniper
```

No inbound firewall rules are needed: the bot makes only outbound connections.

---

## Option B: Render free tier + keepalive

Render's free tier hosts **web services only** — background workers require a paid plan — and free services **spin down after 15 minutes without traffic**, taking ~50 s to wake. The keepalive server plus an external pinger solves both problems.

### B1. Deploy

1. Push your fork to GitHub (never commit `.env`).
2. [render.com](https://render.com) → **New → Web Service** → connect the repo.
3. Configure:

   | Field | Value |
   |---|---|
   | Environment | Python 3 |
   | Build command | `pip install -r requirements.txt` |
   | Start command | `python bot.py` |
   | Instance type | **Free** |

4. **Environment variables** (Render's dashboard, not a file):

   ```
   DISCORD_TOKEN      = your-bot-token
   TARGET_CHANNEL_ID  = 123456789012345678
   RESPONSE_MODE      = reply
   PORT               = 10000
   PYTHON_VERSION     = 3.12.0
   ```

   `PORT` is what makes Render's health check pass. Render also injects `PORT` automatically — the bot honours either.

5. Deploy and watch the logs for `Keepalive HTTP server listening on 0.0.0.0:10000` followed by the startup banner.

### B2. Stop it from sleeping

Free services sleep after 15 minutes of no HTTP traffic. Ping the health endpoint from outside:

1. Copy your service URL (`https://your-app.onrender.com`).
2. Sign up at [UptimeRobot](https://uptimerobot.com) (free, 50 monitors).
3. **New monitor** → HTTP(s) → URL = `https://your-app.onrender.com/health` → interval **5 minutes**.

That also gives you free downtime alerts by email.

**Watch out for:** 750 free instance-hours/month across the workspace (one service fits), and free bandwidth caps. A username bot uses a negligible amount of both.

---

## Option C: Other free PaaS hosts

Same recipe as Render — set `PORT`, deploy, ping the health URL.

| Host | Notes |
|---|---|
| **Koyeb** | Free instance, no card for the starter plan; keeps a web service warm. Use `python bot.py` as the run command. |
| **Fly.io** | Generous small-VM allowance historically; requires a card and a `fly.toml`. Verify current free allowances first. |
| **Replit** | Works, but "Always On" is a paid add-on; without it the repl sleeps even with pings. |
| **Railway** | No ongoing free tier any more — one-time trial credit only. Skip unless you'll pay ~$5/month. |
| **Heroku** | No free dynos since 2022. Skip. |

Avoid anything that only offers *serverless functions* — the bot needs a persistent gateway connection, not a request handler.

---

## Option D: Dedicated free Discord bot hosts

Services such as **bot-hosting.net**, **Sparked Host's free plan**, and similar panels give you a Python container aimed specifically at Discord bots.

**How:** upload the repo (or link GitHub), set the startup file to `bot.py`, add your environment variables in the panel, install `requirements.txt`, start.

**Be realistic about the trade-offs:**

- Node capacity is oversubscribed; expect occasional restarts and lag.
- Some require earning "coins" by watching ads or daily check-ins.
- RAM is typically 256–512 MB. Fine for default mode, **not** enough for `DISCORD_CHECK_MODE=dnsrobot` (Chromium needs ~300 MB+).
- You are trusting an unknown operator with a bot token — use a dedicated bot application you can rotate.

Good for testing; Option A is better for anything you care about.

---

## Option E: A spare machine or Raspberry Pi

The most reliable free option if you have hardware and stable internet. Any Raspberry Pi (3 or newer), an old laptop, or a NAS works — the bot is I/O-bound, not CPU-bound.

Follow [SETUP.md](SETUP.md), then use the **same systemd unit as [A4](#a4-run-it-forever-with-systemd)** (adjust `User` and paths).

On a laptop, stop it sleeping with the lid closed:

```bash
sudo sed -i 's/^#HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind
```

Consider a UPS or just accept that a power cut = downtime. `Restart=always` plus `systemctl enable` handles everything else.

---

## Configuration that matters for cloud hosting

```env
# Required
DISCORD_TOKEN=your-bot-token
TARGET_CHANNEL_ID=123456789012345678

# Answer style (default: an emoji-coded reply listing each platform's status)
RESPONSE_MODE=reply

# Only for PaaS hosts that require an HTTP port (Options B, C)
PORT=8080

# Free tiers are small: keep the Chromium mode off. "instantusername" checks
# Discord over plain HTTP and needs no browser, so it fits everywhere.
DISCORD_CHECK_MODE=instantusername

# Second opinion when a platform blocks your host's IP. Shared cloud IPs get
# rate-limited far more often than home ones, so leave this on.
INSTANTUSERNAME_FALLBACK=true

# Busy channel? These control how many outbound connections the bot may hold.
# On a 1 GB free instance, 200 is comfortable; drop to 50 on the smallest tiers.
HTTP_POOL_LIMIT=200
HTTP_POOL_LIMIT_PER_HOST=40

# Lower memory / fewer outbound requests on a tiny instance
# ENABLE_EXTRA_PLATFORMS=false

# On free cloud IPs Reddit and X wall automated traffic and almost always
# report Unknown — skip them so every row is a real verdict. Checks run in
# parallel, so this does not speed the others up; it just removes noise.
DISABLED_PLATFORMS=Reddit,Twitter/X

# Proxy pool size. The default aims for 1,000 working proxies, which means
# ~100,000 probes and about 4 minutes of background CPU at every boot -
# too much for the smallest free tiers, and on a host that sleeps (Render
# free) it is repeated on every cold start. Scale it to the instance:
#   512 MB / 0.1 CPU (Render, Replit):  200 / 200
#   1 GB shared (Koyeb, Fly):           500 / 500
#   Oracle Ampere or any real VPS:      leave the defaults
PROXY_MIN_POOL=200
PROXY_VERIFY_CONCURRENCY=200
```

The pool itself is cheap — a thousand proxies is a few hundred kilobytes of
strings. What costs CPU is *finding* them, because a free list is ~99 % dead
and each dead entry has to time out. That cost is paid in the background,
after the bot is already answering messages, so it never delays a reply; it
only competes for CPU on an instance that has very little.

On a VPS, also raise the open-file limit if you keep the default width — 1,000
concurrent probes need 1,000 descriptors (`LimitNOFILE=16384` in the unit file
above, or `ulimit -n 8192` before launching by hand). The bot raises its own
soft limit where it is allowed to, and narrows the probe width with a warning
where it is not, so a low limit slows the search down rather than breaking it.

If you use proxies, upload `proxies.txt` alongside `.env` (same secrecy rules — it holds credentials, and it is gitignored). On PaaS hosts with no persistent filesystem, put the list in the `PROXY_URLS` environment variable instead; the same formats are accepted.

**Never** commit `.env` or `proxies.txt`. On PaaS, use the provider's environment-variable UI; on a VPS, `chmod 600 .env`. If a token ever leaks, reset it in the Discord Developer Portal immediately.

---

## Monitoring and staying alive

| Need | Free tool |
|---|---|
| Is it up? | UptimeRobot on `/health` (Options B–D) |
| Is it up? (VPS) | `systemctl status multi-sniper` |
| Logs | `journalctl -u multi-sniper -n 200` or the host's log tab |
| Auto-restart on crash | `Restart=always` (systemd) or the host's default restart policy |
| Auto-restart on reboot | `systemctl enable multi-sniper` |

Quick health probe from anywhere:

```bash
curl -s https://your-app.onrender.com/health
```

`"status":"ok"` means the Discord gateway is connected. `"starting"` means the process is alive but not yet logged in. The payload also reports `checks_served`, `cached_names` and `checks_in_flight`, which is the quickest way to see whether a busy channel is backing up.

---

## Cost traps to avoid

- **Oracle:** stay inside Always Free shapes. Creating an extra block volume or a load balancer can start billing. Check **Cost Analysis** after your first week.
- **Render:** free Postgres expires after 30 days — you don't need a database at all, so don't add one.
- **Railway:** the trial credit runs out and then it charges. Not a free option.
- **Any host:** delete test services you no longer use; several free tiers are per-workspace, not per-service.
- **Set a billing alert** wherever the provider supports one, even at $1.

---

## Cloud troubleshooting

| Symptom | Fix |
|---|---|
| Host kills the service as "unhealthy" | `PORT` is not set, so nothing is listening. Set `PORT` and redeploy. |
| Render logs "no open ports detected" | Same cause — set `PORT`. |
| Bot online, then dies after ~15 min | Free service slept. Add the UptimeRobot ping (B2). |
| `ModuleNotFoundError` on deploy | Build command missing or wrong: `pip install -r requirements.txt`. |
| `DISCORD_TOKEN missing` | Environment variable not set in the host's dashboard (a committed `.env` will not exist there). |
| Boot is busy for a few minutes, then settles | Normal: that is the background proxy search probing ~100,000 entries to reach `PROXY_MIN_POOL`. Lower it (see above) if the instance is small. |
| `Only N of the requested 1000 proxies are working` | The list is too stale to supply that many. Point `PROXY_LIST_URL` at a fresher one, lower `PROXY_MIN_POOL`, or add paid proxies to `proxies.txt` — those are never dropped. |
| Killed with exit code 137 | Out of memory — first disable browser modes: set `GUNSLOL_CHECK_MODE=page` (or `DISCORD_CHECK_MODE=off`/`instantusername`). If still OOM, set `ENABLE_EXTRA_PLATFORMS=false`. |
| guns.lol always shows Cloudflare challenge | Switch to `GUNSLOL_CHECK_MODE=browser` to render the page in Chromium and defeat the challenge. Needs ~300 MB extra RAM and Chromium installed. |
| Instagram always shows Unknown (or false TAKEN on datacenter IPs) | The bot now uses Instagram's `web_profile_info` JSON endpoint first (no login, public `X-IG-App-ID` header). 404 → AVAILABLE, 200 with user JSON → TAKEN. If that endpoint also fails, the page fallback is strict: a datacenter landing page with no profile markers is reported as **Unknown**, never guessed as taken. This is by design — a false \"taken\" hides a potentially free name. |
| Works locally, all ⚠️ in the cloud | The host's IP is rate-limited by Instagram/X. Add `PROXY_URLS`, or disable extra platforms. |
| Oracle "Out of capacity" | Try another availability domain, another region, or the AMD micro shape. |
| Bot restarts every few minutes | Check logs for a crash loop; run `python test_bot.py` on the host to confirm the install. |

---

## Recommended setup, in one line

**Oracle Cloud Always Free + systemd + UptimeRobot alerting** — a real always-on machine, no sleep behaviour, no pings required, and $0/month indefinitely. Everything else on this page is a compromise against that.
