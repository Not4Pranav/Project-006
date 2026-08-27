# 🚀 Multi-Sniper on Render — Complete Setup Guide

> Follow the ordered cross-host checklist in [SETUP.md](SETUP.md) first; this
> document contains Render-specific fields and operational notes.
>
> Everything you need to run this Discord username-sniper bot on
> [Render](https://render.com): account setup, the exact service to create,
> every environment variable, every **token/credential** you need, and
> **step-by-step instructions for acquiring each one**.
>
> Companion docs: [README.md](README.md) (local quick start),
> [CLOUD_SETUP.md](CLOUD_SETUP.md) (all hosts), [`.env.example`](.env.example)
> (full variable reference), [blueprint.md](blueprint.md) (how the bot works).

---

## Table of contents

1. [What this bot needs from Render](#1-what-this-bot-needs-from-render)
2. [The 30-second decision: Background Worker vs Free Web Service](#2-the-30-second-decision-background-worker-vs-free-web-service)
3. [Prerequisites (do this before Render)](#3-prerequisites-do-this-before-render)
4. [Step 1 — Push the repo to GitHub](#4-step-1--push-the-repo-to-github)
5. [Step 2 — Create the Discord application & get the bot token](#5-step-2--create-the-discord-application--get-the-bot-token)
6. [Step 3 — Create the Render account](#6-step-3--create-the-render-account)
7. [Step 4 — Create the Background Worker service](#7-step-4--create-the-background-worker-service)
8. [Step 5 — Add the environment variables](#8-step-5--add-the-environment-variables)
9. [Step 6 — Deploy & verify](#9-step-6--deploy--verify)
10. [✅ Every token / credential you need — and how to acquire it](#10--every-token--credential-you-need--and-how-to-acquire-it)
11. [Optional: Deploy with a Blueprint (render.yaml)](#11-optional-deploy-with-a-blueprint-renderyaml)
12. [Optional: Render CLI, API key, and GitHub token](#12-optional-render-cli-api-key-and-github-token)
13. [Optional: Deploy Hook](#13-optional-deploy-hook)
14. [Can I do this for free on Render? (honest answer)](#14-can-i-do-this-for-free-on-render-honest-answer)
15. [Updating, monitoring & troubleshooting](#15-updating-monitoring--troubleshooting)
16. [Security checklist](#16-security-checklist)
17. [Costs](#17-costs)

---

## 1. What this bot needs from Render

Read this once — it explains every choice below.

| Requirement | What it means |
| ----------- | ------------- |
| **Long-running process** | The bot must stay connected to Discord 24/7. It can never "sleep" on inactivity. |
| **No HTTP port, no web listener** | `bot.py` never accepts connections. It only makes **outbound** HTTPS calls (Discord gateway, Mojang, guns.lol, and optionally DNS Robot through Chromium). |
| **Outbound HTTPS (443)** | Both the Discord WebSocket and the username checks use TLS. No inbound firewall rules needed. |
| **Python 3.10+** | The code uses modern type hints. Render's Python runtime is fine. |
| **Enough memory for the selected mode** | HTTP-only mode is small; `dnsrobot` also keeps headless Chromium running. A 512 MB instance is preferred, while 256 MB can be tight under concurrent browser work. |
| **Environment variables** | All config comes from env vars (Render's Environment tab replaces your local `.env`). |
| **No database, no disk** | State is RAM-only by design (cooldown buckets, result cache). Do **not** add Postgres/Redis/disk. |
| **`DISCORD_TOKEN` before first start** | If it's missing, the bot exits at startup (`❌ DISCORD_TOKEN missing…`) and Render restarts it in a loop. Set env vars **before** the first deploy. |

**One-line summary:** you need a **Background Worker** (a service type that runs
continuously with no incoming traffic), not a Web Service, and you need one
secret: the Discord bot token.

---

## 2. The 30-second decision: Background Worker vs Free Web Service

| | **Background Worker** ✅ recommended | Free Web Service |
| --- | --- | --- |
| Fits this bot? | **Yes** — design for exactly this process type | **No** — Render expects an HTTP listener + health check, and the bot has none |
| Free instance type? | ❌ No free Background Worker path is documented for this setup; verify Render's current plan matrix | Free Web Services may sleep and expect an HTTP listener |
| Always-on Discord connection | Use a plan that does not suspend the worker | No reliable always-on connection for this process |
| Cost | Check [Render pricing](https://render.com/pricing) for the current worker rate | $0 may be available, but it is not a viable worker for this bot |

**Conclusion:** deploy as a **Background Worker on a paid instance type**.
There is no supported free path on Render for this bot today. (Free alternatives
on other hosts are in [CLOUD_SETUP.md](CLOUD_SETUP.md#15-cost-comparison-table), e.g. a free VPS.)

---

## 3. Prerequisites (do this before Render)

```bash
# 1. From the repo root: create a venv and install deps
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt

# 2. Copy the template and put in your real Discord token
cp .env.example .env
#    edit .env — see Section 5 for how to get the token

# 3. Run the offline test suites (must print OK)
python test_checkers.py
python test_bot.py

# 4. Smoke-test the real endpoints from your machine
python checkers.py Notch

# 5. Run the bot locally for a minute and confirm it reacts in Discord
python bot.py
```

Only continue to Render when the bot works locally. You also need:

- ✅ A **private GitHub repo** containing this project (`.env` is git-ignored —
  `git status` must never show it).
- ✅ A **Discord application + bot token** (Section 5).
- ✅ **Render account** (Section 6).

> ⚠️ Never paste `DISCORD_TOKEN` into chat, commits, or logs. It goes into your
> local `.env` and Render's Environment tab — nowhere else.

---

## 4. Step 1 — Push the repo to GitHub

The standard Render flow deploys **straight from GitHub** — no tokens needed
for this step (see Section 10 for the token matrix).

```bash
cd Project-006                # or wherever you cloned the repo
git init                      # if not already a repo
git add .
git commit -m "Multi-Sniper initial commit"
git branch -M main

# Create an empty PRIVATE repo on GitHub first (github.com → New repository → Private),
# then:
git remote add origin https://github.com/<your-username>/multi-sniper.git
git push -u origin main
```

Make sure the repo contains: `bot.py`, `checkers.py`, `requirements.txt`,
`Procfile`, `.env.example` — and **not** `.env`. Check with:

```bash
git status --short        # must not show .env
git ls-files | grep '^\.env$' && echo "DANGER: .env is tracked!" || echo "OK: .env not tracked"
```

---

## 5. Step 2 — Create the Discord application & get the bot token

This is the **one required credential**. Do exactly this:

### 5.1 Create the application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and sign in with your Discord account.
2. Click **New Application** (top right).
3. Name it (e.g. `Multi-Sniper`), accept the terms, click **Create**.
4. You land on the app's *General Information* page. (You can add an icon/description — optional.)

### 5.2 Create the bot user (mandatory)

1. In the left sidebar, click **Bot**.
2. Click **Add Bot** → confirm **Yes, do it!**.
   - ⚠️ **Common mistake:** people skip this and there is *no token to copy*.
     A token only exists after the bot user is created.

### 5.3 Copy the token (this is your `DISCORD_TOKEN`)

1. Still on the **Bot** page, click **Reset Token**.
2. Click **Generate New Token** (it invalidates any old token).
3. **Copy the token immediately** — it is shown **only once**; if you lose it,
   repeat this step.
4. Store it in a password manager, your local `.env`, and (later) Render. Never
   commit it, never paste it in a public place.

> The token looks like a long base64-ish string (often starts with
> `MT...`). Keep it on a **single line** — no quotes, no spaces, no line breaks.

### 5.4 Enable the Message Content intent (critical)

1. On the same **Bot** page, scroll to **Privileged Gateway Intents**.
2. Toggle **MESSAGE CONTENT INTENT** ON.
3. **Save Changes** if a save button appears.

Without this, Discord sends the bot messages with empty content and it will
**never react** — it looks "online" but does nothing. You do **not** need
*Presence Intent* or *Server Members Intent*.

> **100+ servers?** Privileged intents require Discord app **verification**
> once the bot is in more than ~100 guilds. For a personal bot in a few
> servers this is irrelevant today.

### 5.5 Invite the bot to your server

1. Left sidebar → **OAuth2** → **URL Generator**.
2. **Scopes**: tick `bot`.
3. **Bot permissions**: tick
   - `View Channels` (Read Messages / View Channels)
   - `Send Messages`
   - `Add Reactions`
4. Copy the generated URL at the bottom of the page.
5. Open it in a browser → pick your server → **Authorise** → solve the CAPTCHA.
6. The bot appears in your server's member list.

### 5.6 Get the channel IDs (optional but recommended)

1. Discord desktop/web → **User Settings** → **Advanced** → turn **Developer Mode ON**.
2. Right-click the channel you want the bot to watch → **Copy Channel ID** →
   that's your `TARGET_CHANNEL_ID` (a long number like `123456789012345678`).
3. Repeat for a private log channel → `LOG_CHANNEL_ID`.

> Channel IDs are **not secrets** — they're just numbers used to target the
> bot. Still, keep them only in `.env`/Render vars like everything else.

**Checklist:** app exists → bot user exists → token copied → Message Content
intent ON → bot invited to your server → channel IDs copied.

---

## 6. Step 3 — Create the Render account

1. Go to <https://dashboard.render.com/register>.
2. Sign up with **GitHub**, **Google**, or email.
   - GitHub sign-up is convenient because you'll connect a GitHub repo anyway.
3. Verify your email (if applicable).
4. You land in a workspace. The default **Hobby workspace** is free and
   unlimited members-only features aren't needed.
5. (Optional but recommended) Confirm your **account region** and workspace
   name in **Account Settings** → *Profile*.

> You do **not** need a Render API key or CLI token for the dashboard flow —
> see [Section 12](#12-optional-render-cli-api-key-and-github-token) for when
> those are needed.

---

## 7. Step 4 — Create the Background Worker service

1. In the [Render Dashboard](https://dashboard.render.com/), click **New +**
   (top right) → **Background Worker**.
   - ⚠️ Not "Web Service" — a web service expects an HTTP listener and health
     check. This bot is a worker.
2. **Connect GitHub** (first time only):
   - Click **Connect GitHub** → you're taken to GitHub's authorization page →
     **Authorize** the *Render* GitHub App.
   - Choose **All repositories** or **Only select repositories** → select your
     `multi-sniper` repo. (You can change this later in GitHub →
     Settings → Applications.)
3. Select the repository and **branch** (`main`).
4. Fill in the service form:

   | Field | Value |
   | ----- | ----- |
   | **Name** | `multi-sniper` (any name you like) |
   | **Region** | Closest to your Discord server — e.g. *Oregon (US West)*, *Virginia (US East)*, *Frankfurt (EU Central)*, *Singapore (Asia Pacific)* |
   | **Branch** | `main` |
   | **Runtime** | `Python 3` (latest stable is fine) |
   | **Build Command** | `python -m pip install -r requirements.txt` (add `&& python -m playwright install --with-deps chromium` when using `dnsrobot`) |
   | **Start Command** | `python bot.py` (the repo's `Procfile` already declares `worker: python bot.py`, so the Procfile default also works) |
   | **Instance Type** | `Starter` (paid, ~$7/mo per service) — **Free is not offered for Background Workers** |

5. Click **Create Background Worker**. If the form includes environment
   variables, add `DISCORD_TOKEN` there before submitting. Otherwise open the
   service's **Environment** tab immediately after creation. Render clones,
   builds, and starts; an automatic build before variables are entered exits
   safely until you save them and redeploy.

> 💡 Advice: set the required env vars before triggering the first successful
> deploy so the bot does not restart in a loop on a missing `DISCORD_TOKEN`.

---

## 8. Step 5 — Add the environment variables

1. Open the service → **Environment** tab.
2. Click **Add Environment Variable** (one at a time, or use **Add from .env**
   if you have one — but never upload a file containing a real token to a
   public place).
3. **Required:**
   - `DISCORD_TOKEN` → your bot token (from Section 5.3)
4. **Recommended:**
   | Variable | Value | Effect |
   | -------- | ----- | ------ |
   | `TARGET_CHANNEL_ID` | Copy of the channel ID | Bot only reacts in that channel |
   | `LOG_CHANNEL_ID` | ID of a private channel | Posts a mention when a name is found free |
5. **Optional tuning** (safe defaults exist — skip unless you know why):
   `CHECK_TIMEOUT`, `RESPONSE_BUDGET_SECONDS`, `REACTION_TIMEOUT`,
   `USER_MAX_CHECKS`, `USER_WINDOW_SECONDS`, `RESULT_CACHE_TTL`,
   `DISCORD_CHECK_MODE`, `DISCORD_ACCOUNT_API_*`, `DISCORD_PROBE_*`,
   `PROXY_URL`. Set `DISCORD_CHECK_MODE=dnsrobot` to load the DNS Robot page
   in Chromium; it needs no extra secret. Full descriptions live in
   [`.env.example`](.env.example).
6. Click **Save Changes**. Render redeploys automatically.
7. (Optional) Group related vars under **Environment Groups** for reuse across
   services.

**Rules:**
- Plain strings — no quotes, no trailing spaces.
- `DISCORD_TOKEN` is a **secret** — Render's Environment tab has no separate
  "secret" toggle for plain vars; it's hidden from others on your account, but
  it appears in some UIs, so keep the repo private and the account secure.
- Multi-line values are never needed here.

> **Don't set these:** no `PORT`, no `DATABASE_URL`, no Redis vars — the bot
> never listens and uses no database.

---

## 9. Step 6 — Deploy & verify

1. Open the service → **Events** tab → watch the build. A failed build shows
   the exact pip error there.
2. Open the **Logs** tab. You should see the startup banner:

   ```
   ============================================================
   🟢 MULTI-SNIPER ONLINE as Multi-Sniper
   🔒 Watching channel : 123456789012345678
   🕹️ Platforms        : Minecraft | guns.lol | Discord (mode: off)
   ...
   ============================================================
   ```

3. Type a bare username in the watched Discord channel → you get reactions
   (⏳ while checking, then ✅/❌/⚠️).
4. Each check logs one line (e.g. `Minecraft available HTTP 404 (zxqw… )`).

**Verify the config took:** the banner prints the active channel, platforms,
cooldown, and budget — one glance confirms the env vars were applied.

---

## 10. ✅ Every token / credential you need — and how to acquire it

Here's the complete matrix. **Only the first row is strictly required for a
working deploy.** Everything else is optional and only needed for the workflow
listed in the *Uses* column.

### Token matrix (bottom line)

| # | Credential | Required? | Used for | Where to acquire (exact path) | Lifetime / rotation |
|---|------------|-----------|----------|-------------------------------|---------------------|
| 1 | **Discord bot token** (`DISCORD_TOKEN`) | ✅ **Yes** | Bot logs into Discord | Discord Developer Portal → app → **Bot** → **Reset Token** | Until reset. Rotate: Developer Portal → Reset Token → update Render → redeploy |
| 2 | Channel IDs | Optional (recommended) | Targeting the watched/log channel | Discord User Settings → Advanced → **Developer Mode** → right-click channel → **Copy Channel ID** | Never expires; not a secret |
| 3 | **Render API key** (`RENDER_API_KEY`) | ❌ Optional | CLI/API/CI automation | Render Dashboard → **Account Settings** → **API Keys** → **Create API Key** | No periodic expiry; revoke manually when unused |
| 4 | **Render CLI token** | ❌ Optional | Local `render` CLI | `render login` → browser → **Authorize CLI** | Expires periodically; re-run `render login` |
| 5 | **GitHub token (PAT/SSH)** | ❌ Optional | Pushing to a *private* repo from your machine, or scripted deploys | GitHub → Settings → Developer settings → **Personal access tokens** → generate | You choose expiry (30/90 days); rotate manually |
| 6 | **Deploy Hook URL** | ❌ Optional | Trigger a redeploy from CI/curl | Service → Settings → **Deploy Hook** → **Generate** | Contains an embedded secret; anyone with URL can redeploy |
| 7 | **Account API credential** (`DISCORD_ACCOUNT_API_TOKEN`) | ❌ Optional | Only in `DISCORD_CHECK_MODE=account` when the authorized endpoint requires it | Your authorized account API/OAuth provider; never use a personal Discord client token | Provider-specific |
| 8 | **Discord probe token** (`DISCORD_PROBE_TOKEN`) | ❌ Optional | Only if you run your own +2 username checker | You create it in *your own* checker service (or your proxy's API key) | Up to you |
| 9 | **Proxy credentials** (`PROXY_URL`) | ❌ Optional | Route outbound checks via a residential proxy | Your proxy provider's dashboard (e.g. Bright Data, Oxylabs, or self-hosted) | Provider-specific |
| 10 | Render payment method | ❌ Optional | Only if you run a paid worker instance (recommended) | Render Dashboard → **Billing** → add card | Card details are never a token; stored by Render |

### 10.1 Discord bot token — full acquisition walkthrough

Covered in [Section 5](#5-step-2--create-the-discord-application--get-the-bot-token).
TL;DR:

```
discord.com/developers/applications
  → New Application
  → Bot → Add Bot
  → Bot → Reset Token → Copy (shown only once!)
  → Privileged Gateway Intents → MESSAGE CONTENT INTENT → ON
```

- **There is no Discord bot permission that unlocks username availability.**
  The bot's check is `off` by default. `dnsrobot` loads
  `https://dnsrobot.net/username-checker` in isolated Chromium and needs no
  extra secret. Add `python -m playwright install --with-deps chromium` to
  the Render build command for this mode. `account` mode uses the first-party
  account-flow eligibility
  route (or a compatible authorized gateway) and may work without a credential;
  if your authorized provider requires one, store it as
  `DISCORD_ACCOUNT_API_TOKEN`. Never use or request a personal Discord client
  token.
- **Rotating:** Portal → Bot → Reset Token → paste new token into Render's
  Environment tab → Save Changes. The old token dies immediately.

### 10.2 Render API key — when and how

**When you need it:** running Render CLI commands in CI/CD or scripts,
calling the [Render REST API](https://render.com/docs/api), or using
`RENDER_API_KEY` instead of an interactive CLI login. **Not needed** for the
dashboard deploy in Sections 7–9.

**How to acquire (exact steps):**

1. Log in to the [Render Dashboard](https://dashboard.render.com/).
2. Click your avatar → **Account Settings**, or go directly to
   <https://dashboard.render.com/u/settings>.
3. Click **API Keys** (there's also a direct "add API key" shortcut link).
4. Click **Create API Key**.
5. Give it a descriptive name (e.g. `multi-sniper-ci`).
6. Click **Create** → the key (format `rnd_...`) is displayed **in full only
   once** — copy it immediately and store it in your password manager, GitHub
   Actions secrets, or a local `.env` (never committed).
7. (Optional) Set a **workspace scope** if offered. API keys are personal and
   carry your permissions — they aren't fine-grained.
8. **Test it:**

   ```bash
   export RENDER_API_KEY=rnd_xxxxxxxxxxxxxxxx
   curl --request GET \
        --url 'https://api.render.com/v1/services?limit=20' \
        --header 'Accept: application/json' \
        --header "Authorization: Bearer $RENDER_API_KEY"
   ```

   A `200` with a JSON list means it works.

**Security:** API keys don't periodically expire — **revoke them manually** when
unused or if leaked: Account Settings → API Keys → (⋮) → **Revoke**. Keep them
out of git; in CI, put them in **GitHub Actions secrets** (`Settings → Secrets
and variables → Actions → New repository secret`).

### 10.3 Render CLI token — when and how

**When you need it:** you prefer the CLI for deploys/logs/SSH instead of the
dashboard.

**How to acquire:**

1. Install the CLI:

   ```bash
   brew install render                      # macOS
   # or the official install script on Linux/macOS:
   curl -fsSL https://raw.githubusercontent.com/render-oss/cli/refs/heads/main/bin/install.sh | sh
   ```

2. Run `render login`.
3. Your browser opens a confirmation page in the Render Dashboard.
4. Click **Authorize CLI**.
5. The CLI saves the generated token to `~/.render/cli.yaml` (you never see or
   need to copy the token itself). A success message appears in the browser.
6. Back in the terminal, it prompts you to set your active workspace —
   choose the one that holds your service (switch later with
   `render workspace set`).

**Important:** CLI tokens **expire periodically**. If a command fails with an
auth error, just run `render login` again. When `RENDER_API_KEY` is exported,
the API key takes precedence over the CLI token. View/revoke active CLI tokens
in Account Settings → *CLI tokens*.

**Typical commands:**

```bash
render services                          # list services
render logs multi-sniper --tail          # tail the bot's logs
render deploys create multi-sniper --wait --confirm   # redeploy
render blueprints validate render.yaml   # check infra-as-code
```

### 10.4 GitHub tokens — do you actually need one?

| Scenario | Need a token? |
| -------- | ------------- |
| Render dashboard deploys from GitHub | **No** — Render's GitHub App authorization handles it |
| Push to a **private** repo from your machine over HTTPS | Yes: **PAT** (or use SSH keys / `gh auth login`) |
| Scripted deploys in CI (Render CLI) | Store **Render API key** in a GitHub Actions secret (Section 10.2) — not a GitHub PAT |

**If you do need a GitHub PAT (exact steps):**

1. [github.com](https://github.com) → avatar → **Settings**.
2. **Developer settings** (bottom of left sidebar) → **Personal access tokens**.
3. Pick **Fine-grained tokens** (recommended) or **Tokens (classic)**.
4. **Generate new token**:
   - Name: `render-deploy` (or whatever).
   - **Fine-grained:** Owner + repository access → *Only select repositories* →
     your repo; **Permissions** → *Repository permissions* → *Contents*:
     **Read and write** (only needed if the token *pushes*; for pure deploys you
     don't need repo access at all).
   - **Classic:** scope `repo` (full) if pushing; `public_repo` for public only.
   - Expiration: 30/90 days or custom.
5. Click **Generate token** → copy immediately (shown once) → store in a
   password manager / git credential manager.
6. Use it: `git push https://<token>@github.com/<user>/<repo>.git main` — or
   better, let a credential helper handle it:

   ```bash
   gh auth login          # GitHub CLI: prompts for a PAT or device flow, stores it securely
   git push -u origin main
   ```

   Or SSH: `ssh-keygen -t ed25519` → copy `~/.ssh/id_ed25519.pub` →
   GitHub → Settings → SSH and GPG keys → **New SSH key** → paste → then
   `git remote set-url origin git@github.com:<user>/<repo>.git`.

**Security:** PATs are passwords to your GitHub account (scoped). Never commit
them; set short expirations; revoke in Settings → Developer settings whenever
unused.

### 10.5 Deploy Hook — what it is

An alternative deploy trigger: Service → **Settings** → **Deploy Hook** →
**Generate Deploy Hook** → copy the URL (it contains an embedded secret key).
Then:

```bash
curl -X POST "https://api.render.com/deploy/srv-xxx?key=xxx"
```

Anyone with that URL can trigger a redeploy, so treat it like a secret. Git
auto-deploys make this optional.

### 10.6 What you do **not** need

- ❌ No AWS / GCP / Azure keys
- ❌ No Render token for the standard dashboard deploy
- ❌ No Minecraft or guns.lol API key (public endpoints)
- ❌ No separate Discord bot permission or token is required for the default
  Account API route; only add `DISCORD_ACCOUNT_API_TOKEN` for an authorized
  gateway that explicitly requires it
- ❌ No database credentials, no Redis password
- ❌ No SSH keys on Render (and free instances don't support SSH anyway)

---

## 11. Optional: Deploy with a Blueprint (render.yaml)

Prefer infrastructure-as-code? Add a `render.yaml` at the repo root and use
**New + → Blueprint** instead of Background Worker. The `sync: false` on
`DISCORD_TOKEN` makes Render prompt you for the secret at deploy time — it is
never stored in git:

```yaml
services:
  - type: worker
    name: multi-sniper
    runtime: python
    plan: starter            # workers have no free instance type
    buildCommand: python -m pip install -r requirements.txt && python -m playwright install --with-deps chromium
    startCommand: python bot.py
    envVars:
      - key: DISCORD_TOKEN
        sync: false          # prompt at deploy; never stored in git
      - key: TARGET_CHANNEL_ID
        value: "123456789012345678"   # optional; blank = watch all channels
```

Validate before pushing:

```bash
render blueprints validate render.yaml
```

> Blueprints are synced from the repo — if you remove a secret from the YAML,
> don't remove it from Render blindly; check the sync behavior docs first.

---

## 12. Optional: Render CLI, API key, and GitHub token

See Section 10.2–10.4 for acquisition; quick usage here:

```bash
# Render CLI with API key (automation-safe)
export RENDER_API_KEY=rnd_xxx
render services
render logs multi-sniper --tail
render deploys create multi-sniper --wait --confirm

# Render CLI with browser login (local)
render login
render workspace set

# GitHub Actions example (CI redeploy)
# steps: export RENDER_API_KEY from repo secret, then:
```

```yaml
- name: Deploy to Render
  env:
    RENDER_API_KEY: ${{ secrets.RENDER_API_KEY }}
  run: render deploys create <service-id> --wait --confirm
```

---

## 13. Optional: Deploy Hook

Covered in [Section 10.5](#105-deploy-hook--what-it-is). Use it if you want a
webhook-style redeploy (e.g. from another CI) without a Render API key.

---

## 14. Can I do this for free on Render? (honest answer)

**Short answer: not for this bot.** Current Render policy (verify at
[render.com/pricing](https://render.com/pricing) and
[render.com/docs/free](https://render.com/docs/free)):

- Free instances exist for **Web Services**, **Postgres**, **Key Value**,
  **static sites** — **not** Background Workers.
- Free **Web Services** spin down after **15 minutes** without inbound traffic.
  This bot runs an *outbound* Discord connection, which doesn't count as
  inbound traffic — so a free web service would go offline every 15 minutes,
  and Render also expects a web service to answer an HTTP health check, which
  `bot.py` never does.
- Free databases expire after 30 days; free instances can restart anytime,
  lack SSH/one-off jobs, and heavy outbound traffic can trigger suspension.

**Free ($0) alternatives that DO work** (see
[CLOUD_SETUP.md](CLOUD_SETUP.md#15-cost-comparison-table)):

1. **Oracle Cloud Always-Free VM** (ARM, 4 vCPU / 24 GB) + systemd — genuinely
   free 24/7, requires you to manage a box.
2. **Railway Free plan** (small monthly usage allowance) — cheapest managed
   option to test.
3. **Any $4–6/mo VPS** if you want cheap + reliable instead of free + fiddly.

If you want Render specifically with zero surprises, the Starter Background
Worker at ~$7/mo is the correct choice.

---

## 15. Updating, monitoring & troubleshooting

### Updating

```bash
# Tests first — always
python test_checkers.py && python test_bot.py

git add -A && git commit -m "fix: ..." && git push origin main
```

Render auto-deploys on push (default). Manual: **Manual Deploy → Deploy
latest commit**. Rollback: **Manual Deploy → Rollback** (free instances roll
back only two deploys; paid instances more).

### Monitoring

- **Logs tab** is the dashboard: startup banner + one line per check.
- **Events tab** shows builds/deploys/restarts.
- discord.py reconnects automatically after brief network drops; Render
  restarts the process on crash.
- State is RAM-only: restarts clear cooldown/caches by design. No action needed.

### Troubleshooting

| Symptom | Cause → Fix |
| ------- | ----------- |
| Logs show `❌ DISCORD_TOKEN missing…` loop | Env var not set / misspelled / quotes or spaces. Fix in Environment tab → Save. |
| Bot starts but never reacts | Message Content intent off, wrong `TARGET_CHANNEL_ID`, or missing bot permissions → re-invite (Section 5.5). |
| `Improper token has been passed` | Token copied wrong / has a line break → Reset Token and re-copy. |
| Reactions always ⚠️ | Outbound HTTPS blocked or platform blocking datacenter IPs → `python checkers.py Notch` from your machine; add `PROXY_URL`. |
| guns.lol always blocked/403 | Cloudflare on datacenter IPs — expected on Render; use a residential proxy. |
| Deploy fails at build | Events tab shows the pip error — check `requirements.txt` is at repo root. |
| "Service is unhealthy" | You created a **Web Service** instead of a **Background Worker** — recreate as worker. |
| Render emails about bandwidth/build minutes | Free web service limits — not applicable to a paid worker; check Billing page. |

---

## 16. Security checklist

- [ ] Repo is **private**; `.env` is git-ignored; `git status` is clean.
- [ ] `DISCORD_TOKEN` only in local `.env` + Render Environment tab.
- [ ] Render API key (if created) in a password manager, never in git.
- [ ] GitHub PAT (if created) short-lived, scoped, revoked when unused.
- [ ] Deploy hook URL not posted anywhere public.
- [ ] No tokens in the bot's logs (the bot redacts credentials by design —
  still avoid pasting raw logs publicly).
- [ ] Token rotation path known: Discord Developer Portal → Reset Token →
  update Render → Save Changes → redeploy.
- [ ] Render account has 2FA (Account Settings → Security) and Billing alerts
  enabled if you run a paid instance.

---

## 17. Costs

| Item | Cost |
| ---- | ---- |
| Render workspace | $0 (Hobby) |
| Background Worker — Starter instance | ~**$7/month per service** (check [pricing](https://render.com/pricing); prices changed April 2026 — currently a $0–499/mo workspace fee + per-service compute) |
| Discord | $0 |
| Minecraft / guns.lol checks | $0 (public endpoints; free plans of those services) |
| Optional residential proxy | ~$3–25/mo depending on provider |

**Bottom line:** ~$7/mo on Render for a reliable 24/7 deploy, or a few dollars
per month on a VPS / Railway if you want cheaper. A *free* Render deploy isn't
supported for this bot under current Render free-tier rules.

---

*Verified against Render's official docs (render.com/docs/free,
render.com/docs/background-workers, render.com/docs/cli, render.com/docs/api)
on 2026-08-26. Render changes pricing and free-tier rules periodically —
re-check the linked pages before committing.*
