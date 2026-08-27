# Multi-Sniper: complete setup and deployment guide

This is the canonical, ordered setup guide for the project. Follow it from top
to bottom for a new installation; use the later sections for deployment,
verification, troubleshooting, and safe updates.

The bot is a long-running Discord **worker**. It does not expose an HTTP server
or listen on a port. It connects outbound to Discord and to the configured
username-checking services.

## 1. Prerequisites

Before cloning the project, have:

- **Python 3.10 or newer.** Check with `python --version` (or
  `python3 --version`). The source uses the `str | None` type-hint syntax and
  therefore does not support Python 3.9.
- **Git** and permission to clone/push the repository.
- A Discord account, a server where you can add a bot, and a text channel the
  bot can watch.
- An outbound network that permits HTTPS (port 443) and Discord's secure
  gateway WebSocket connection.
- A private place for secrets: a local `.env` file during development and the
  deployment provider's environment/secret settings in production.

A database, Redis instance, inbound port, public domain, or cloud API key is not
required for the default bot.

## 2. Get the source and create the Python environment

### 2.1 Clone the repository

Run this from the directory where you keep projects:

```bash
git clone https://github.com/Not4Pranav/Project-006.git
cd Project-006
```

For a private fork, authenticate Git with a credential manager or configure an
SSH key and use the SSH clone URL. Never put a GitHub token in the clone URL or
commit it to a remote configuration. If the repository is already present, enter
its root instead:

```bash
cd /path/to/Project-006
```

Confirm that the files are in the current directory:

```bash
python --version
git status --short
ls bot.py checkers.py requirements.txt .env.example
```

On Windows PowerShell, use `Get-ChildItem` instead of `ls` if necessary.

### 2.2 Create and activate a virtual environment

Use a project-local `.venv`; it is already ignored by Git.

**macOS/Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows Command Prompt:**

```bat
py -3 -m venv .venv
.venv\Scripts\activate
```

**Windows PowerShell:**

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

Every new terminal needs activation again. Verify that the selected Python is
inside `.venv`:

```bash
python --version
python -c "import sys; print(sys.executable)"
```

If PowerShell blocks activation, either allow scripts for your user according
to your organization's policy or run the commands from Command Prompt. You can
also invoke `.venv\Scripts\python.exe` directly without activation.

### 2.3 Install dependencies

Always invoke pip through the active Python so packages cannot land in a
different Python installation:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The dependencies include Playwright's Python package because the opt-in
`dnsrobot` mode uses a real browser. The browser executable is installed in the
next section, not by `pip`.

## 3. Configure the Discord application

Complete this section before trying to run `bot.py`.

### 3.1 Create the application and bot user

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and sign in.
2. Select **New Application**, give it a name such as `Multi-Sniper`, and
   create it.
3. Open **Bot** in the left sidebar.
4. Select **Add Bot** and confirm.
5. Select **Reset Token** / **Generate New Token**, then copy the token into a
   password manager temporarily. Discord shows a token only when it is
   revealed; resetting invalidates the old token.
6. Never put this token in Git, an issue, a screenshot, a chat message, or a
   public log. It will be entered only in `.env` or a deployment secret field.

The copied bot token is the value for `DISCORD_TOKEN`. It is not a personal
Discord client token and it must not be used for any username-check request.

### 3.2 Enable Message Content Intent

The bot reads a message's bare username text, so this intent is required:

1. Stay on the application's **Bot** page.
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
3. Save the change if Discord shows a save button.

Presence Intent and Server Members Intent are not required by this project.

### 3.3 Invite the bot with the minimum useful permissions

1. Open **OAuth2 → URL Generator**.
2. Under **Scopes**, select `bot`.
3. Under **Bot Permissions**, select:
   - **View Channels**;
   - **Read Message History**;
   - **Add Reactions**; and
   - **Send Messages** only if `LOG_CHANNEL_ID` will be used (it is also safe
     to grant it now for simpler setup).
4. Copy the generated URL, open it, select the target server, and authorize.
   You need permission to manage that server for it to appear in the list.
5. In the target channel, verify that the bot's role can see the channel and
   add reactions. If you use hit logging, give it the same access in the
   private log channel.

Do not grant Administrator or Manage Server; the bot does not need either.

### 3.4 Copy channel IDs

1. In Discord, open **User Settings → Advanced** and enable **Developer Mode**.
2. Right-click the channel to watch and select **Copy Channel ID**. This is
   `TARGET_CHANNEL_ID`.
3. If using hit logging, right-click a private log channel and copy its ID for
   `LOG_CHANNEL_ID`.

Channel IDs are identifiers, not passwords. A blank `TARGET_CHANNEL_ID` makes
the bot inspect every channel it can see, so setting it is strongly recommended.

## 4. Create and fill the environment file

### 4.1 Create the private file

From the repository root:

**macOS/Linux/Git Bash:**

```bash
cp .env.example .env
chmod 600 .env 2>/dev/null || true
```

**Windows PowerShell:**

```powershell
Copy-Item .env.example .env
```

Open `.env` in a local editor. Do not edit `.env.example` to hold real secrets.
The `.env` file is ignored by Git; confirm that it is not tracked:

```bash
git status --short
git ls-files --error-unmatch .env >/dev/null 2>&1 \
  && echo "STOP: .env is tracked" \
  || echo "OK: .env is not tracked"
```

### 4.2 Required and common variables

Start with this minimum configuration and replace the placeholders:

```dotenv
DISCORD_TOKEN=paste-the-bot-token-on-one-line
TARGET_CHANNEL_ID=123456789012345678
LOG_CHANNEL_ID=
DISCORD_CHECK_MODE=off
```

`DISCORD_TOKEN` is the only required variable. Keep it on one line. Do not add
quotes, comments, or spaces to the token value.

| Variable | Default | Set it when | Important behavior |
|---|---|---|---|
| `DISCORD_TOKEN` | none | Always | Bot token from Developer Portal. Missing/blank stops startup. |
| `TARGET_CHANNEL_ID` | blank | Recommended | One channel to watch. Blank watches all visible channels. |
| `LOG_CHANNEL_ID` | blank | Optional | Channel for messages about confirmed free results. |
| `DISCORD_CHECK_MODE` | `off` | Only when enabling a Discord mode | Accepted values are `off`, `dnsrobot`, `account`, `account_api`, and `probe`, case-insensitively. Unknown values stop startup. |

### 4.3 Choose the Discord mode

The modes coexist; changing the value only selects the optional Discord checker.
Minecraft and guns.lol continue to run in every mode.

- **`off`** — safe default; Discord is skipped and no Discord availability claim
  is made.
- **`dnsrobot`** — literal DNS Robot mode. For each valid candidate, an
  isolated Chromium page loads
  `https://dnsrobot.net/username-checker?u=<candidate>` and the bot reads the
  rendered Discord card. No DNS Robot credential, account token, or bot token is
  passed to the page. This mode is the one that checks through the DNS Robot
  website and requires the browser installation in Section 5.
- **`account`** — opt-in JSON account-flow check. It uses the configured
  account endpoint and strict JSON parsing. Enable it only for an endpoint and
  use case you are authorized to operate.
- **`account_api`** — compatibility alias for `account`.
- **`probe`** — opt-in GET to an external checker that you explicitly control
  or are authorized to use. It requires a URL template containing
  `{username}`. The contract is `200 = taken`, `404 = available`, and
  `401/403/429 = blocked/unknown`.

For `dnsrobot`, page failures, browser errors, rate limits, challenge pages,
missing results, and ambiguous labels remain `BLOCKED`/`ERROR`; the bot never
turns them into `AVAILABLE`.

### 4.4 Account and probe variables

Leave these blank unless that mode is enabled and the endpoint owner has given
you permission:

```dotenv
# Used only by account/account_api mode. Blank uses the built-in first-party
# eligibility route. A custom endpoint must implement the documented JSON body.
DISCORD_ACCOUNT_API_URL=
DISCORD_ACCOUNT_API_TOKEN=
DISCORD_ACCOUNT_API_TOKEN_HEADER=Authorization
DISCORD_ACCOUNT_API_TOKEN_SCHEME=Bearer

# Used only by probe mode. The placeholder is required.
DISCORD_PROBE_URL=https://checker.example/username/{username}
DISCORD_PROBE_TOKEN=
DISCORD_PROBE_TOKEN_HEADER=Authorization
DISCORD_PROBE_TOKEN_SCHEME=Bearer
```

The account request body is `{"username": "candidate"}` and a strict boolean
`taken`/`available` answer is required. The bot never reuses `DISCORD_TOKEN` as
an account credential. Never paste a personal Discord client token into any
variable. Probe credentials are sent only to `DISCORD_PROBE_URL`.

Use HTTPS for endpoints that receive credentials. A malformed URL, header name,
placeholder, or credential containing a line break is rejected before the bot
connects to Discord.

### 4.5 Optional network and timing variables

The safe defaults normally need no changes:

```dotenv
PROXY_URL=
CHECK_TIMEOUT=3
RESPONSE_BUDGET_SECONDS=4.5
REACTION_TIMEOUT=0.75
USER_MAX_CHECKS=3
USER_WINDOW_SECONDS=60
RESULT_CACHE_TTL=300
```

- `PROXY_URL` accepts an HTTP(S) proxy. It is applied to normal HTTP checks and
  to the DNS Robot browser when that mode is selected. Keep proxy credentials
  private.
- `CHECK_TIMEOUT` is clamped to the response budget.
- `RESPONSE_BUDGET_SECONDS` is clamped to 0.5–4.8 seconds so reactions stay
  below the five-second target.
- `REACTION_TIMEOUT` is the cap for each concurrent Discord reaction call.
- `USER_MAX_CHECKS` and `USER_WINDOW_SECONDS` limit per-user traffic.
- `RESULT_CACHE_TTL` reuses definitive results to reduce upstream requests.

Malformed or non-finite numeric values fall back to safe defaults; they do not
crash the process halfway through a lookup.

## 5. Install Chromium for DNS Robot mode

Skip this section only if `DISCORD_CHECK_MODE=off`, `account`, `account_api`, or
`probe` will be used and you do not plan to switch to `dnsrobot`.

After activating `.venv` and installing requirements, run:

```bash
python -m playwright install chromium
```

On a Linux VPS or CI image where Chromium libraries are not already installed,
run instead:

```bash
python -m playwright install --with-deps chromium
```

The command must be run as the same OS user and with the same virtual
environment that will run `python bot.py`. If the browser is installed for a
different user, the bot may report that the executable is missing.

The browser is long-lived at bot startup, while each lookup receives a fresh
isolated browser context. Account and probe headers are not copied into that
context.

## 6. Run local validation before starting the bot

Run all commands from the repository root with `.venv` active:

```bash
python -m py_compile bot.py checkers.py test_bot.py test_checkers.py
python test_checkers.py
python test_bot.py
```

Expected offline result at the time of this version:

- `test_checkers.py`: 36 tests, 2 optional live-network tests skipped;
- `test_bot.py`: 30 tests;
- 66 total tests, 64 executed offline;
- every executed test ends with `OK`.

The tests use fake HTTP/browser objects and do not need a Discord token. If you
changed the mode to `dnsrobot`, the mocked tests still pass without a browser;
the real browser is exercised by the next smoke test.

### 6.1 Smoke-test the ordinary checkers

This command makes live requests to Minecraft and guns.lol. Discord remains
off unless you provide a mode explicitly:

```bash
python checkers.py Notch
```

A `BLOCKED` result from a platform is an honest network/service outcome, not a
reason to change the code to report `AVAILABLE`.

### 6.2 Smoke-test literal DNS Robot mode

Make sure Chromium is installed, then run:

```bash
python checkers.py vortex --mode dnsrobot --timeout 8
```

The command should show a Discord detail similar to `DNS Robot page: Available`
or `DNS Robot page: Taken`. `ERROR` for a missing browser and `BLOCKED` for a
challenge/rate limit are expected safe outcomes; fix the browser/network issue
rather than treating them as free.

### 6.3 Start the bot locally

1. Put the intended mode and channel IDs in `.env`.
2. Start the process:

   ```bash
   python bot.py
   ```
3. Confirm the console prints `MULTI-SNIPER ONLINE`, the watched channel, the
   selected Discord mode, and (for `dnsrobot`) `DNS Robot browser: ready`.
4. In the watched channel, send one bare candidate such as `vortex`.
5. Confirm the bot reacts to the **same message**. It ignores sentences,
   mentions, bot messages, webhooks, and messages in other channels.
6. With `DISCORD_CHECK_MODE=off`, expect no Discord emoji. With `dnsrobot`, a
   Discord emoji appears only when the DNS Robot page says Available. An
   unavailable/blocked browser produces the uncertainty reaction rather than a
   false free result.
7. Stop with Ctrl+C after verification.

## 7. Deploy as a long-running worker

Do not deploy this project as a web service that expects a listening `PORT`.
Use a background worker, service, machine, or VPS process. Set variables in the
provider's environment UI; do not upload `.env`.

### 7.1 Render Background Worker

1. Push the repository to a private GitHub repository. Before pushing, run the
   tests from Section 6 and check that `git status` does not show `.env`.
2. Sign in to [Render](https://render.com) and authorize its GitHub App for the
   repository.
3. Select **New + → Background Worker**. Do **not** select Web Service.
4. Select the repository and the branch you intend to deploy.
5. Set the runtime to **Python 3** and use these commands:
   - **Build Command** (normal modes):
     `python -m pip install -r requirements.txt`
   - **Build Command** (if `DISCORD_CHECK_MODE=dnsrobot`, or if you want to
     enable it later without another build):
     `python -m pip install -r requirements.txt && python -m playwright install --with-deps chromium`
   - **Start Command**: `python bot.py`
   - **Service type**: Background Worker. No health-check URL or `PORT` is
     needed.
6. Add environment variables in the creation form if Render exposes that
   option, or open the service's **Environment** tab immediately after creation:
   - required: `DISCORD_TOKEN`;
   - recommended: `TARGET_CHANNEL_ID`;
   - selected mode: `DISCORD_CHECK_MODE`; and
   - any account, probe, proxy, or timing variables required by Section 4.
   If the dashboard starts an automatic first build before the variables can be
   entered, let that failed start stop and set the variables; it cannot log in
   without `DISCORD_TOKEN`.
7. Save the environment settings and deploy/redeploy. Render will build and start
   the worker.
8. Open the service **Logs**. Verify the startup banner, the selected mode, and
   a ready DNS Robot browser when applicable.
9. Send a candidate in Discord and verify the same-message reaction. Check the
   logs for one sanitized status line per platform.

Render's worker pricing and available plans change. Check Render's current
pricing before selecting an instance. A free Web Service is not a substitute:
it expects an HTTP listener and may sleep, while this bot needs a continuous
worker.

### 7.2 Railway or another managed worker

1. Create a project/service from the private GitHub repository.
2. Select Python 3.10+ (or let the provider use the repository's Python
   runtime) and set the build command to:
   `python -m pip install -r requirements.txt`.
3. If using `dnsrobot`, append:
   `&& python -m playwright install --with-deps chromium` to the build command.
4. Set the start command to `python bot.py`.
5. Add the same variables from Section 4 in the provider's Variables/Secrets
   panel. Do not upload `.env`.
6. Deploy, read the worker logs, and perform the Discord verification in
   Section 8.

If the provider only offers a web process, use its worker/process setting or
choose a VPS. Do not add a dummy web server just to satisfy a health check.

### 7.3 Ubuntu/Debian VPS with systemd

The following example uses `/opt/multisniper`, a dedicated service account, and
an environment file outside the Git checkout.

1. Provision a VPS with outbound HTTPS and log in over SSH.
2. Install Python, Git, and browser system dependencies:

   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-venv
   ```

3. Create a non-root service account and application directory:

   ```bash
   sudo useradd --system --create-home --home-dir /opt/multisniper multisniper
   sudo -u multisniper mkdir -p /opt/multisniper/app
   ```

   Clone the repository as `multisniper`. For a public repository, use HTTPS:

   ```bash
   sudo -u multisniper git clone https://github.com/Not4Pranav/Project-006.git /opt/multisniper/app
   ```

   For a private repository, configure a **read-only GitHub deploy key** for
   this service account first (GitHub repository → Settings → Deploy keys →
   Add deploy key; leave write access off), verify GitHub's SSH host key, and
   then clone with its SSH URL. Do not put a PAT in the URL, shell history, or
   `/etc/multisniper.env`:

   ```bash
   sudo -u multisniper mkdir -m 700 /opt/multisniper/.ssh
   sudo -u multisniper ssh-keygen -t ed25519 -N '' \
     -f /opt/multisniper/.ssh/id_ed25519 \
     -C multisniper-read-only
   sudo cat /opt/multisniper/.ssh/id_ed25519.pub
   # Add the displayed public key as the repository's read-only deploy key.
   # Verify github.com's fingerprint through a trusted channel, then:
   sudo -u multisniper ssh-keyscan -H github.com >> /opt/multisniper/.ssh/known_hosts
   sudo -u multisniper git clone git@github.com:Not4Pranav/Project-006.git /opt/multisniper/app
   ```

   Replace the owner/repository in the commands with your private fork. Then
   create the virtual environment and install the Python package:

   ```bash
   sudo -u multisniper python3 -m venv /opt/multisniper/venv
   sudo -u multisniper /opt/multisniper/venv/bin/python -m pip install --upgrade pip
   sudo -u multisniper /opt/multisniper/venv/bin/python -m pip install -r /opt/multisniper/app/requirements.txt
   ```

4. If using DNS Robot mode, install system libraries as root and the browser as
   the service user. `--with-deps` cannot be run as a locked-down service user
   because its `apt-get` step needs root:

   ```bash
   sudo /opt/multisniper/venv/bin/python -m playwright install-deps chromium
   sudo -u multisniper /opt/multisniper/venv/bin/python -m playwright install chromium
   ```

5. Create the private environment file and restrict it:

   ```bash
   sudo nano /etc/multisniper.env
   sudo chown root:multisniper /etc/multisniper.env
   sudo chmod 640 /etc/multisniper.env
   ```

   Put `DISCORD_TOKEN`, channel IDs, and the selected variables in this file.
   Do not copy `.env` into the Git checkout.

6. Create `/etc/systemd/system/multisniper.service` with:

   ```ini
   [Unit]
   Description=Multi-Sniper Discord bot
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=multisniper
   Group=multisniper
   WorkingDirectory=/opt/multisniper/app
   EnvironmentFile=/etc/multisniper.env
   ExecStart=/opt/multisniper/venv/bin/python /opt/multisniper/app/bot.py
   Restart=always
   RestartSec=10
   NoNewPrivileges=true
   PrivateTmp=true

   [Install]
   WantedBy=multi-user.target
   ```

7. Enable, start, and inspect the service:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now multisniper
   sudo systemctl status multisniper --no-pager
   sudo journalctl -u multisniper -f
   ```

8. Complete the verification in Section 8 before closing the SSH session.

The systemd service has no `ExecStop` script because the bot closes its HTTP
session and Chromium runtime when systemd sends a normal stop signal.

### 7.4 Other hosts

The same requirements apply to Heroku worker dynos, Fly.io Machines, Docker,
and other VPS providers: install the requirements, install Chromium when
`dnsrobot` is selected, inject environment variables, and run `python bot.py`.
See [CLOUD_SETUP.md](CLOUD_SETUP.md) and [render.md](render.md) for host-specific
notes. Do not commit a provider-generated secret file.

## 8. Production verification checklist

Perform these checks after every first deploy or configuration change:

1. **Build:** the provider reports a successful Python dependency install. If
   using DNS Robot, the build also reports a successful Chromium install.
2. **Startup:** logs show `MULTI-SNIPER ONLINE`, the expected channel scope, and
   the expected `DISCORD_CHECK_MODE`.
3. **Gateway:** the bot appears online in the Discord server. If not, check the
   token and Message Content intent first.
4. **Permissions:** send a bare candidate in `TARGET_CHANNEL_ID`; the bot adds
   a reaction to that exact message. It does not send a normal reply.
5. **Modes:**
   - `off`: logs say Discord is skipped and only confirmed Minecraft/guns.lol
     emojis can appear;
   - `dnsrobot`: logs say `DNS Robot page: Available` or `DNS Robot page: Taken`
     for a successful browser result; a page block remains blocked/error;
   - `account`/`account_api`: only a strict authorized JSON answer is used;
   - `probe`: only the configured URL is requested.
6. **Safety:** make one request with a known-bad/blocked network path if you
   need to exercise the warning path. Confirm that it produces `⚠️` or an
   omitted platform emoji, never a speculative `FREE` result.
7. **Cache/cooldown:** repeat a candidate to confirm the cache, then send enough
   different candidates from one user to observe `⏳`. This confirms upstream
   rate-limit protection is active.
8. **Restart:** restart the service once and confirm it reconnects. The cache
   and cooldown state are intentionally in memory and reset after a restart.

## 9. Troubleshooting

| Symptom | Check and fix |
|---|---|
| `DISCORD_TOKEN missing` | Set the exact variable in `.env`/the provider's Environment panel. Do not put it in a YAML value, source file, or chat. |
| `Improper token has been passed` | Reset/re-copy the bot token from Developer Portal. Keep it on one line and update every deployment copy. |
| Bot is online but never reacts | Enable Message Content Intent, verify the channel ID, ensure the bot can View Channels/Read Message History, and check that messages are a single bare token. |
| Missing `Add Reactions` permission | Update the bot role/channel overrides or re-invite with Add Reactions. |
| `DNS Robot browser is unavailable` | Install the package and executable in the same environment: `python -m pip install -r requirements.txt` then `python -m playwright install chromium`. |
| Chromium starts locally but not on Linux | Install OS libraries as root with `python -m playwright install-deps chromium`, then install the browser as the service user with `python -m playwright install chromium`. Managed build images can use `install --with-deps chromium`. |
| DNS Robot returns `BLOCKED`/`ERROR` | The page may be challenged, rate-limited, redirected, or unable to reach Discord. Inspect logs and network egress; keep the unknown result. Do not add a personal client token. |
| DNS Robot always times out | Increase only the bounded check setting if necessary, verify Chromium can reach `https://dnsrobot.net`, and remember the handler has a sub-five-second reaction budget. |
| DNS Robot says `Taken` unexpectedly | Confirm the page itself in a normal browser and inspect the rendered card. The adapter accepts only the card scoped to Discord; a page/selector change should be treated as unknown, not changed to `Available`. |
| `account` is always blocked/error | The account-flow route may require authorization or may have changed. Verify the documented endpoint/contract and policy; do not substitute a personal client token. |
| `probe` is skipped | Set `DISCORD_CHECK_MODE=probe` and an HTTPS `DISCORD_PROBE_URL` containing the literal `{username}` placeholder. |
| All platform checks show `ERROR`/`BLOCKED` | Test outbound HTTPS from the deployment, inspect the proxy, and run `python checkers.py Notch` locally. Datacenter IPs may be challenged by guns.lol or Discord. |
| Minecraft is suddenly blocked | Reduce request rate, keep the cache/cooldown enabled, and wait for the upstream limit. The fallback does not make a block into an availability answer. |
| Render reports an unhealthy service | The service was likely created as Web Service. Recreate/select Background Worker; this bot has no HTTP health endpoint. |
| Build succeeds but the process exits | Read the first stack trace, verify Python 3.10+, check the environment variable names, and run the exact start command locally. |
| No private hit log | Set `LOG_CHANNEL_ID`, grant the bot Send Messages there, and remember logging occurs only for a confirmed available platform. |

## 10. Safe updates and credential rotation

### 10.1 Update code without exposing secrets

1. Announce a short maintenance window if the bot is used by others.
2. Keep a private copy of the current environment values. Never put that copy
   in the repository.
3. Fetch the intended branch and inspect the changes:

   ```bash
   git fetch origin
   git status --short
   git diff origin/main...HEAD
   ```

4. Activate the environment and update dependencies:

   ```bash
   source .venv/bin/activate                 # Windows uses .venv\Scripts\activate
   python -m pip install -r requirements.txt
   python -m playwright install chromium      # if dnsrobot is enabled
   ```

5. Run the full validation again:

   ```bash
   python -m py_compile bot.py checkers.py test_bot.py test_checkers.py
   python test_checkers.py
   python test_bot.py
   ```

6. On a VPS, deploy the reviewed revision and restart:

   ```bash
   cd /opt/multisniper/app
   sudo -u multisniper git pull --ff-only
   sudo systemctl restart multisniper
   sudo journalctl -u multisniper -n 100 --no-pager
   ```

   On Render/Railway/another Git-connected host, push the reviewed commit to
   the deployment branch and use the provider's deploy/redeploy action.
7. Repeat Section 8. If the new revision is unhealthy, stop/rollback to the
   previous reviewed revision and retain the logs for diagnosis.

### 10.2 Rotate the Discord bot token

1. In Developer Portal, open the application → **Bot** → **Reset Token**.
2. Copy the new token directly into your password manager.
3. Replace `DISCORD_TOKEN` in the local/deployment secret store, not in Git.
4. Save/redeploy or restart the worker.
5. Verify the gateway reconnects, then revoke/delete the old stored copy.

The old token stops working when it is reset. Do not print it to test logs.

### 10.3 Change modes safely

1. Review the mode contract in Section 4.3.
2. If switching to `dnsrobot`, install Chromium in the deployment build before
   changing the environment value.
3. If switching to `account` or `probe`, verify authorization and scope every
   credential to its one endpoint.
4. Change only the environment value, restart/redeploy, and check the startup
   banner.
5. Run one controlled candidate and inspect the sanitized status line.

## 11. Security and operational rules

- Keep the repository private when it contains deployment configuration or
  operational notes. `.env`, `.venv`, caches, and compiled files are ignored.
- `DISCORD_TOKEN` is used only by the Discord client. It is never sent to DNS
  Robot, Minecraft, guns.lol, the account endpoint, or the probe endpoint.
- The DNS Robot browser context receives no account/probe token or custom auth
  header. Proxy credentials are used only to connect to the configured proxy.
- `DISCORD_ACCOUNT_API_TOKEN` is sent only to `DISCORD_ACCOUNT_API_URL`, and
  `DISCORD_PROBE_TOKEN` only to `DISCORD_PROBE_URL`. Never reuse a personal
  Discord client token.
- Use HTTPS for credential-bearing endpoints and keep provider logs/private
  dashboards access-controlled.
- Do not lower cooldowns or remove caching to automate high-volume harvesting.
  This bot only reports hints; it never claims or registers names. Confirm a
  candidate in the platform's own UI and follow each service's terms.
- Treat every `AVAILABLE` result as a time-sensitive hint, not a reservation.
  Any failure, block, malformed response, rate limit, or ambiguous page state
  is intentionally represented as unknown/error.

## 12. Final go-live checklist

- [ ] Python 3.10+ and a clean `.venv` are being used.
- [ ] `requirements.txt` installed successfully.
- [ ] `test_checkers.py` and `test_bot.py` both end with `OK`.
- [ ] Discord application and bot user exist.
- [ ] Message Content Intent is enabled.
- [ ] Bot is invited to the correct server with View Channels, Read Message
      History, and Add Reactions (and Send Messages if logging).
- [ ] `DISCORD_TOKEN` is present only in the private secret store.
- [ ] `TARGET_CHANNEL_ID` is correct.
- [ ] `DISCORD_CHECK_MODE` is deliberately selected; default is `off`.
- [ ] Chromium is installed if and only if `dnsrobot` is enabled/planned.
- [ ] Worker logs show the expected startup banner.
- [ ] A real test message receives the expected same-message reaction.
- [ ] Unknown/block behavior was observed or reviewed and is not treated as
      availability.
- [ ] The update and token-rotation procedure is known to the operator.
