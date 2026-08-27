# 🎯 Multi-Sniper — Discord Username Availability Bot

Post a bare username in a watched Discord channel. The bot checks it across **8 platforms in parallel** and reacts to that same message with one emoji per platform where the name is free.

> **Platforms:** Minecraft 🕹️ · guns.lol 🔫 · Discord 🐈‍⬛ · GitHub 💻 · Steam 🎮 · Reddit 👀 · Instagram 📸 · Twitter/X 🐦

```
you:  vortex

bot:  Minecraft: Available
      guns.lol: Unavailable
      Discord: Unavailable
      GitHub: Available
      Steam: Unavailable
      Reddit: Available
      Instagram: Unknown
      Twitter/X: Unavailable
```

The reply appears **immediately** and fills in live as each platform reports. Prefer emoji reactions on the original message instead? Set `RESPONSE_MODE=react`.

---

## Contents

- [How it answers](#how-it-answers)
- [Quick start](#quick-start)
- [How a lookup works](#how-a-lookup-works)
- [Speed: why answers are instant](#speed-why-answers-are-instant)
- [Busy channels: many members, many usernames](#busy-channels-many-members-many-usernames)
- [Fallback source: instantusername.com](#fallback-source-instantusernamecom)
- [Platform status matrix](#platform-status-matrix)
- [Proxy pool](#proxy-pool)
- [Smart caching](#smart-caching)
- [Discord check modes](#discord-check-modes)
- [Configuration reference](#configuration-reference)
- [Development and testing](#development-and-testing)
- [Troubleshooting](#troubleshooting)
- [Responsible use](#responsible-use)

---

## How it answers

`RESPONSE_MODE` picks the style — `reply` (default), `react`, or `both`.

### Reply mode (default)

One message per lookup, listing every platform:

| Word | Meaning |
|---|---|
| **Available** | The name is free there |
| **Unavailable** | Taken |
| **Invalid** | The name can never be used there (too short, illegal characters) |
| **Unknown** | Blocked, rate-limited, or timed out — the bot refuses to guess |
| **Checking...** | Still waiting; replaced as soon as that platform answers |

Platforms that are switched off are hidden (`REPLY_INCLUDE_SKIPPED=true` shows them as *Not checked*), and the requester is not pinged (`REPLY_MENTION_AUTHOR=true` changes that).

### React mode

| Reaction | Meaning |
|---|---|
| 🕹️ 🔫 🐈‍⬛ 💻 🎮 👀 📸 🐦 | The name is **free** on that platform |
| ❌ | Every platform answered, and none were free |
| ⚠️ | At least one check could not be confirmed — treat the result as unknown |
| ⏳ | You tripped the per-user flood guard; try again immediately |

Either way the bot never responds to bots, webhooks, other channels, or anything that is not a single bare username token.

---

**Hosting it free, 24/7?** See **[CLOUD_SETUP.md](CLOUD_SETUP.md)**.

## Quick start

```bash
# 1. Clone and enter the repo
git clone https://github.com/Not4Pranav/Project-006.git
cd Project-006

# 2. Create a virtualenv (Python 3.10+)
python3 -m venv .venv && source .venv/bin/activate
python --version                  # must be >= 3.10

# 3. Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 4. Configure
cp .env.example .env
#    Then edit .env and set at minimum:
#       DISCORD_TOKEN=your-bot-token
#       TARGET_CHANNEL_ID=123456789012345678

# 5. Verify offline (no token or network needed)
python test_checkers.py           # 138 tests
python test_bot.py                # 63 tests
python test_stress.py             # 17 tests
python test_integration.py        # 13 tests

# 6. Run
python bot.py
```

Discord-side setup (creating the application, the **Message Content Intent**, and the invite URL) is covered step by step in **[SETUP.md](SETUP.md)**. To keep it running 24/7 for free, see **[CLOUD_SETUP.md](CLOUD_SETUP.md)**.

Try it without Discord at all:

```bash
python checkers.py Notch          # one-off report for all 8 platforms
python checkers.py vortex --no-extra
```

---

## How a lookup works

1. **Filter** — bots, webhooks, wrong channels, and non-username text are dropped before any budget is spent.
2. **Flood guard** — a sub-second token bucket per user (default: 5 checks per 0.5 s).
3. **Cache** — a recent definitive answer is returned instantly, with no network calls.
4. **Share duplicate work** — if the same username is already being checked for someone else, this message waits on that one lookup instead of starting a second (see *Busy channels* below).
5. **Parallel fan-out** — all 8 checks start at once under one shared wall-clock deadline, so total latency is the *slowest single platform*, not the sum.
6. **Fallback** — any platform that could not answer (blocked, rate-limited, network error) gets a second opinion from instantusername.com before it is reported as unknown.
7. **Normalise** — every response maps to `available` / `taken` / `invalid` / `blocked` / `skipped` / `error`.
8. **Answer as results land** — the reply is posted instantly and edited as each platform reports (or, in react mode, each emoji is added the moment that platform answers). Only the ❌ / ⚠️ summary has to wait for everyone.
9. **Log hits** — optionally mirror free names into a private channel, always after the user-visible reaction.

Everything after step 1 shares a single response budget (4.5 s by default, hard-clamped below Discord's 5 s interaction feel), so a slow platform can never delay the reaction.

---

## Speed: why answers are instant

**Time-to-first-reaction is the number that matters** — how long before you see *any* answer. Streaming reactions cuts it by an order of magnitude:

| | First reaction | All reactions |
|---|---|---|
| Batched (old) | 1602 ms | 1604 ms |
| **Streaming (default)** | **121 ms** | 1603 ms |

*Measured over 8 platforms with one 1.6 s straggler; total time is still bounded by the slowest platform, but you no longer wait for it to learn the fast ones.*

| Technique | Effect |
|---|---|
| **Streaming answers** | The reply is painted instantly and updated per platform — a fast result is never held hostage by a slow site |
| Parallel fan-out with a shared deadline | 8 platforms cost one platform's latency, not the sum |
| Result cache | Repeat lookups answer in microseconds, zero requests |
| **Connection pre-warming** | TLS to all 8 hosts is established at startup, so the first lookup skips DNS + TCP + TLS |
| **Bounded page reads (96 KB)** | Steam/Instagram/X markers sit at the top of the document; the rest of a multi-MB page is never downloaded |
| **Hedged Minecraft request** | The backup Mojang endpoint starts only if the primary stalls 150 ms — one request when healthy, no doubled latency when not |
| TCP pooling + keep-alive (30 s) | No repeated handshakes between lookups |
| DNS cache (5 min) | No repeated resolution per check |
| Sub-second flood guard (5 / 0.5 s) | Back-to-back checks are not throttled in practice |
| Per-request proxy rotation | The 8 checks spread across 8 IPs instead of queueing behind one |
| One retry on transient errors | A single connection reset does not become a ⚠️ |
| gzip/deflate compression | Smaller bodies for the HTML-scraped platforms |

With streaming on, emojis appear in **completion order** rather than platform order. Set `STREAM_REACTIONS=false` if you prefer the old fixed ordering.

Brotli is deliberately **not** requested: aiohttp cannot decode `br` without the optional `Brotli` package, and advertising it makes real sites return bodies the bot cannot read.

---

## Busy channels: many members, many usernames

Every message is handled independently and answered as a reply **to that member's own message**, so a channel where twenty people paste twenty different usernames at the same second behaves exactly like twenty separate lookups — no shared result list, no cross-talk, no queue.

Three things make that hold up under load:

| Mechanism | What it prevents |
|---|---|
| **Large connection pool** (`HTTP_POOL_LIMIT`, default 200 / 40 per host) | Requests silently queueing on an exhausted pool until they blow their deadline and report *Unknown* |
| **Duplicate coalescing** (`COALESCE_DUPLICATES`, default on) | Twelve members pasting the same name causing twelve identical fan-outs (96 requests) instead of one (8) |
| **Per-user flood guard** | One spammer eating everyone else's budget — the token bucket is per user, never global |

Measured with a local server answering each "platform" in 80 ms, one lookup = 8 requests:

| Members at once | Old pool (25/10) | New pool (200/40) |
|---|---|---|
| 10 | 0.67 s slowest | **0.18 s** |
| 25 | 1.65 s slowest | **0.45 s** |
| 50 | 3.40 s slowest (over budget) | **0.87 s** |
| 100 | 6.04 s slowest, **10 timed out** | **1.75 s, none timed out** |

Duplicate coalescing is transparent: each member still gets their own reply, the lookup just runs once. If the shared lookup fails or runs out of time, the waiting messages fall back to doing the work themselves rather than reporting nothing.

---

## Fallback source: instantusername.com

A platform check can stop working for reasons that have nothing to do with the username — Cloudflare turns on a challenge, an endpoint rate-limits the host, a site changes its markup. Rather than reporting *Unknown*, the bot asks a second source:

```
GET https://api.instantusername.com/check/<service>/<username>
    -> {"available": true, "url": "..."}
```

- It runs **only** when the platform's own check came back blocked or errored, so a healthy lookup never pays for it.
- It runs **inside the same shared deadline** — the fallback can never push an answer past the response budget.
- It only overrides the result when it is itself definitive (`available` / `taken`). A failed fallback leaves the original honest *Unknown* in place.
- The service catalogue is fetched from `/services.json` at startup, so platforms instantusername adds later are picked up without a code change. If that fetch fails, a built-in map is used.

Covered by default: **GitHub, Steam, Reddit, Instagram, Twitter/X** (plus Discord and Minecraft automatically, if instantusername lists them). guns.lol has no equivalent there and is never sent.

Set `INSTANTUSERNAME_FALLBACK=false` to disable the second source entirely — for example if you do not want usernames leaving your host except to the platforms themselves.

---

## Platform status matrix

| Platform | Emoji | Reported FREE | Reported TAKEN | Unknown / blocked |
|---|---|---|---|---|
| Minecraft | 🕹️ | 204 or 404 | 200 with profile JSON | 403 / 405 / 429 |
| guns.lol | 🔫 | 404/410, or an unclaimed-page marker | 200 without that marker | 403 / 429 / 503, Cloudflare challenge |
| Discord | 🐈‍⬛ | mode-dependent | mode-dependent | mode-dependent (see below) |
| GitHub | 💻 | 404 | 200 with a `login` field | 403 / 429 (rate limit) |
| Steam | 🎮 | 404, or "profile could not be found" | 200 with profile content | 403 / 429 / 503 |
| Reddit | 👀 | 404 | 200 with user-about JSON | 403 / 429 / 503 |
| Instagram | 📸 | 404, or "this page isn't available" | 200 profile page | login wall, checkpoint, 401 / 403 / 429 |
| Twitter/X | 🐦 | 404, or "this account doesn't exist" | 200 profile page | rate limit, Arkose challenge, 403 / 429 |

Instagram and X are **best-effort**: both aggressively gate unauthenticated traffic. When they gate the bot, the result is honestly reported as unknown (⚠️) rather than guessed. Page matching folds typographic apostrophes to ASCII, so the real `doesn’t` served by those sites is matched correctly.

Minecraft is checked against `api.mojang.com` first and falls back to `api.minecraftservices.com` if the first endpoint is blocked or errors.

---

## Proxy pool

Proxies are optional. Configure them and every outbound check is routed through the rotation.

### The easy way: `proxies.txt`

Drop your vendor's list into a file called **`proxies.txt`** in the project folder, one proxy per line. It is picked up automatically at startup — no environment variable, no reformatting:

```text
# proxies.txt
gate.example-vendor.com:7000:myuser:mypassword
gate.example-vendor.com:7001:myuser:mypassword
203.0.113.9:8080
```

Check the list before you rely on it — this loads the file, validates every entry, and probes them all concurrently:

```bash
python proxies.py                     # checks proxies.txt
python proxies.py /path/to/list.txt --timeout 8
python proxies.py "https://drive.google.com/file/d/<id>/view" \
    --want 1000 --skip-socks --keep proxies.txt
```

The last form distils a huge public list into a working one: it keeps testing batches until **1,000 proxies answer**, then writes just those to `proxies.txt`. On a free list that means probing ~100,000 entries, so give it several minutes; `--limit N` tests a fixed sample instead, and `--concurrency` (default 500) trades sockets for speed. Doing this once and committing the result to `proxies.txt` gives you an instant boot afterwards, because curated proxies skip the search entirely.

```
3 proxies loaded from proxies.txt

  ✓ http://gate.example-vendor.com:7000     HTTP 200 in 412 ms
  ✓ http://gate.example-vendor.com:7001     HTTP 200 in 388 ms
  ✗ http://203.0.113.9:8080                 could not connect to the proxy

2/3 alive.
```

`proxies.txt` is **gitignored**, so credentials never end up in a commit. Copy `proxies.txt.example` to get started, and set `PROXY_FILE=/path/to/list.txt` to read it from somewhere else (or `PROXY_FILE=` to switch the file off).

Every common vendor format is accepted and normalised for you:

| Written as | Understood as |
|---|---|
| `1.2.3.4:8080` | `http://1.2.3.4:8080` |
| `1.2.3.4:8080:user:pass` | `http://user:pass@1.2.3.4:8080` |
| `user:pass@1.2.3.4:8080` | `http://user:pass@1.2.3.4:8080` |
| `user:pass:1.2.3.4:8080` | `http://user:pass@1.2.3.4:8080` |
| `http://user:pass@1.2.3.4:8080` | unchanged |
| `https://1.2.3.4:3128` | unchanged |

Blank lines and `#` comments are ignored, duplicates are dropped, credentials containing `@ : space` are percent-encoded, and an unreadable line is skipped with a warning instead of taking the bot down. SOCKS proxies are **rejected at startup** with a clear message — silently ignoring them would run the bot with no proxy at all and leak your real IP.

### Big public lists: `PROXY_LIST_URL`

Scraped public lists are far too large to keep in a repo (the one this bot ships with is ~169,000 entries) and go stale within hours, so they are **fetched at startup instead of stored**:

```env
# Default: the shared list. Set blank to switch remote loading off.
PROXY_LIST_URL=https://drive.google.com/file/d/<id>/view
```

Google Drive `…/view` links, GitHub `blob` links and plain raw URLs all work — share links are rewritten to their direct-download form automatically, and an HTML sign-in page is detected and rejected rather than parsed as proxies.

What happens on boot, in order:

| Step | Why |
|---|---|
| Reuse `.proxy-cache.txt` if younger than `PROXY_LIST_TTL` (6 h) | Restarts are instant and work offline |
| Otherwise download, then rewrite the cache | Survives the next restart even if the host goes away |
| Drop entries on SOCKS-only ports (1080, 4145, 9050, …) | aiohttp cannot speak SOCKS — on a scraped list that is ~44% of the file, and every one would be a wasted probe |
| Sample a first batch up to `PROXY_MAX_POOL` (2,000); the rest stays in reserve | Sampling is spread across the whole file, not the first N, so the pool is not all one scraper's block. The cap is a ceiling on memory and health-check time, not a target |
| Probe each one, keep only what answers, **and keep pulling from the reserve until `PROXY_MIN_POOL` (1,000) are working** | Public proxies are mostly dead on arrival — a 1%-alive list needs roughly 100,000 probes to yield 1,000 survivors, so one sample is never enough. The search continues until the floor is met, the list runs out, or `PROXY_VERIFY_MAX_SECONDS` (15 min) expires |

Startup verification is **pass/fail on one probe**, unlike the running rule where a proxy is only benched after 3 consecutive failures. The probe is an **HTTPS** fetch (`PROXY_PROBE_URL`) because every real check is HTTPS — a proxy that cannot `CONNECT` is no use even if it serves plain HTTP happily. If *nothing* answers, the pool is kept anyway and you get a loud warning — an empty pool would silently mean direct, unproxied traffic.

The search runs **in the background**: the bot answers messages while it works, using whatever is already verified. It also runs on **its own connector**, so a thousand doomed proxy connections never queue behind — or evict — the connections live lookups are using.

### Reaching 1,000 working proxies

Rehearsed against 160,000 candidate entries of which exactly **1.0 % were alive**, with a fifth of the dead ones
hanging until the probe timed out rather than refusing instantly — the expensive shape of a real scraped list:

```
Verifying 2000 proxies (1000 at a time), aiming for 1000 working...
Proxy search:  132/1000 working after testing  12,000 (1.1% alive,  26s)
Proxy search:  432/1000 working after testing  42,000 (1.0% alive,  88s)
Proxy search:  701/1000 working after testing  72,000 (1.0% alive, 157s)
Proxy search: 1004/1000 working after testing 100,201 (1.0% alive, 225s)
Proxy pool ready: 1004 working (tested 100,201, dropped 1,979) in 224.7s
```

**1,004 working proxies in 3 min 45 s**, every survivor genuinely alive, 59,799 entries still untouched in reserve.
A lookup's 8 requests then went out over 8 distinct IPs in 1.3 ms.

Three things make that possible, and they are worth knowing about before you tune anything:

- **Width.** 1,000 probes are in flight at once (`PROXY_VERIFY_CONCURRENCY`). The wall-clock cost is dominated by
  dead proxies that hang for the full timeout, so width — not patience — is what finds working ones.
- **File descriptors.** 1,000 concurrent sockets need 1,000 file descriptors, and the usual Linux default is 1,024.
  The bot raises its own soft limit at startup, and if the host refuses it **narrows the probe width to fit and
  says so** rather than dying with "too many open files".
- **Chunking.** Probes are built in chunks instead of one coroutine per entry, so testing 100,000 proxies does not
  allocate 100,000 coroutines up front. Peak memory stays flat whether the list has 1,000 entries or 169,000.

Sizing the pool is a real trade-off, not a "bigger is better":

| Pool | Good for | Costs |
|---|---|---|
| 100–300 | Small servers, tight hosts (Render free: 512 MB, 0.1 CPU) | Each IP is reused often, so per-IP rate limits arrive sooner |
| **1,000** (default) | Busy servers, aggressive rate limits | ~4 min of background probing at boot, wider health sweeps |
| 2,000+ | Very heavy use with a fresh list | Each 30 s health sweep has more to get through; diminishing returns |

On a 512 MB / 0.1 CPU free host, set `PROXY_MIN_POOL=200` and `PROXY_VERIFY_CONCURRENCY=200`: the search is what
costs CPU, not the pool.

Parsing and filtering a list this size stays cheap — measured on the 169,000-entry list (3.2 MB):

```
parsed 168,997 proxies in 0.3 s
SOCKS-port filter: dropped 74,996, kept 94,001
```

Anything you configure locally (`PROXY_URL`, `PROXY_URLS`, `proxies.txt`) is treated as **curated**: it is never
sampled away and never filtered. If startup verification cannot reach one of your own proxies it stays in the pool
anyway and you get a warning naming it — a paid proxy that fails one probe is far more likely to be a slow handshake
than a dead endpoint, and silently discarding what you paid for would be worse than a noisy log line. Only proxies
pulled from a remote list are dropped when they fail to answer.

### Or via environment variables

```env
# Single proxy (backward compatible)
PROXY_URL=http://user:pass@proxy.example:8080

# Pool — comma or newline separated, same formats as the file
PROXY_URLS=proxy1:8080,proxy2:8080,user:pass@proxy3:8080
```

All three sources are merged in that order — `PROXY_URL`, then `PROXY_URLS`, then the file — with duplicates removed.

What the pool does:

- **Per-request round-robin.** The proxy is resolved for each individual request, so the 8 checks in one lookup go out over 8 different proxies.
- **Live health reporting.** A proxy that fails a *real* check is recorded immediately and benched after 3 consecutive failures — no waiting for the next health sweep.
- **Automatic retry on a different proxy.** A transient failure is retried once, and the retry resolves a fresh proxy.
- **Concurrent health sweeps** every 30 s: all proxies are probed at the same time, so N proxies cost one timeout instead of N.
- **Recovery** after a 60 s cooldown, then the proxy rejoins the rotation.

### When every proxy is down

By default the pool **keeps using proxies** (it resets the failure counters and retries them) instead of going direct. That is deliberate: if you configured proxies to keep your real IP away from these platforms, a silent direct fallback would leak exactly what you were hiding.

If you treat proxies purely as a speed optimisation, opt in:

```env
PROXY_ALLOW_DIRECT_FALLBACK=true
```

Proxy credentials are redacted from every log line and from the startup banner.

---

## Smart caching

A cache hit is the fastest possible answer: no sockets, no proxies, microseconds.

| Result | Default TTL | Why |
|---|---|---|
| Taken names | 600 s (`RESULT_CACHE_TTL` × 2) | Claimed names rarely free up quickly |
| Available names | 120 s (`RESULT_CACHE_TTL` × 0.4) | A free name may get sniped by someone else |

Only **complete, definitive** answers are cached. A lookup where any platform returned `blocked` or `error` is never cached, so a transient outage cannot pin a wrong answer for ten minutes.

The cache is pruned on write — stale entries first, then oldest — and capped by `CACHE_MAX_ENTRIES` (default 5000), so a busy server cannot grow it without bound.

---

## Discord check modes

Discord has no public username-availability API, so this check is **off by default**. Set `DISCORD_CHECK_MODE` to enable one:

| Mode | How it works | Needs |
|---|---|---|
| `off` *(default)* | Skipped; reported as `skipped` | — |
| `dnsrobot` | Loads `dnsrobot.net/username-checker` in a headless Chromium context and reads the rendered result | `python -m playwright install chromium` |
| `account` / `account_api` | POSTs `{"username": "..."}` to Discord's username-eligibility route | Optionally an authorised credential |
| `probe` | GETs your own authorised checker URL template (`200` = taken, `404` = free) | `DISCORD_PROBE_URL` |

The bot never claims a name, and never sends the Discord bot token to any of these endpoints. Any credential you configure is sent **only** to the endpoint it belongs to.

---

## Configuration reference

Every value has a safe default except `DISCORD_TOKEN`. Out-of-range or malformed values are clamped rather than crashing the bot.

### Core

| Setting | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token from the Discord Developer Portal |
| `TARGET_CHANNEL_ID` | *(blank = all)* | The single channel to watch |
| `LOG_CHANNEL_ID` | *(blank = off)* | Private channel that receives free-name hits |
| `ENABLE_EXTRA_PLATFORMS` | `true` | Include GitHub, Steam, Reddit, Instagram, Twitter/X |
| `RESPONSE_MODE` | `reply` | `reply` (text list), `react` (emoji), or `both` |
| `REPLY_EDIT_INTERVAL` | `0.7` | Minimum seconds between live edits of the reply |
| `REPLY_INCLUDE_SKIPPED` | `false` | Show disabled platforms as *Not checked* |
| `REPLY_MENTION_AUTHOR` | `false` | Ping the requester in the reply |
| `STREAM_REACTIONS` | `true` | Answer per platform as it reports (fastest); `false` batches |
| `PORT` / `KEEPALIVE_PORT` | *(blank)* | Serve a health endpoint so free hosts keep the bot alive |
| `PREWARM_CONNECTIONS` | `true` | Open TLS to all platform hosts at startup |
| `INSTANTUSERNAME_FALLBACK` | `true` | Ask instantusername.com when a platform's own check fails |
| `COALESCE_DUPLICATES` | `true` | Members asking the same name at once share one lookup |

### Latency and throttling

| Setting | Default | Range | Description |
|---|---|---|---|
| `RESPONSE_BUDGET_SECONDS` | `4.5` | 0.5 – 4.8 | Total budget for checks + reactions |
| `CHECK_TIMEOUT` | `3` | 0.05 – budget | Per-request outbound timeout |
| `REACTION_TIMEOUT` | `0.75` | 0.05 – budget−0.05 | Cap per Discord reaction call |
| `USER_MAX_CHECKS` | `5` | 1 – 10000 | Checks allowed per user per window |
| `USER_WINDOW_SECONDS` | `0.5` | ≥ 0.01 | Flood-guard window — sub-second so checks feel instant |
| `HTTP_POOL_LIMIT` | `200` | 8 – 5000 | Total outbound connections — raise it for very busy channels |
| `HTTP_POOL_LIMIT_PER_HOST` | `40` | 2 – 1000 | Outbound connections per platform host |

### Caching

| Setting | Default | Description |
|---|---|---|
| `RESULT_CACHE_TTL` | `300` | Base TTL; the two below default to multiples of it |
| `CACHE_TTL_TAKEN` | `600` | TTL for taken names (0 disables) |
| `CACHE_TTL_AVAILABLE` | `120` | TTL for available names (0 disables) |
| `CACHE_MAX_ENTRIES` | `5000` | Hard ceiling on cached usernames |

### Proxies

| Setting | Default | Description |
|---|---|---|
| `PROXY_URL` | *(blank)* | Single proxy; also joins the pool if one is configured |
| `PROXY_URLS` | *(blank)* | Comma/newline separated pool |
| `PROXY_FILE` | `proxies.txt` | Proxy list file loaded automatically; blank disables it |
| `PROXY_LIST_URL` | *(shared list)* | Remote list downloaded at startup; blank disables it |
| `PROXY_CACHE_FILE` | `.proxy-cache.txt` | Where the downloaded list is cached |
| `PROXY_LIST_TTL` | `21600` | Seconds before the remote list is downloaded again |
| `PROXY_LIST_TIMEOUT` | `20` | Download timeout in seconds |
| `PROXY_MAX_POOL` | `2000` | Most proxies allowed in the rotation |
| `PROXY_MIN_POOL` | `1000` | Keep searching the list until this many work |
| `PROXY_VERIFY_ON_START` | `true` | Probe once at boot and keep only what answers |
| `PROXY_VERIFY_CONCURRENCY` | `1000` | Proxies probed at the same time (own connector, auto-narrowed if the host's open-file limit is lower) |
| `PROXY_VERIFY_TIMEOUT` | `5` | Seconds allowed per verification probe |
| `PROXY_VERIFY_MAX_SECONDS` | `900` | Wall-clock ceiling on the background search |
| `PROXY_PROBE_URL` | `https://api.mojang.com` | What a proxy must be able to fetch to count |
| `PROXY_HEALTH_CONCURRENCY` | `200` | Parallelism of the periodic 30 s sweep |
| `PROXY_SKIP_SOCKS_PORTS` | `true` | Ignore entries on well-known SOCKS ports |
| `PROXY_ALLOW_DIRECT_FALLBACK` | `false` | Go direct when every proxy is down (leaks the host IP) |

### Discord check

| Setting | Default | Description |
|---|---|---|
| `DISCORD_CHECK_MODE` | `off` | `off` / `dnsrobot` / `account` / `account_api` / `probe` |
| `DISCORD_ACCOUNT_API_URL` | Discord eligibility route | Endpoint for `account` mode |
| `DISCORD_ACCOUNT_API_TOKEN` | *(blank)* | Credential sent only to that endpoint |
| `DISCORD_ACCOUNT_API_TOKEN_HEADER` | `Authorization` | Header carrying that credential |
| `DISCORD_ACCOUNT_API_TOKEN_SCHEME` | `Bearer` | Prefix; blank sends the raw token |
| `DISCORD_PROBE_URL` | *(blank)* | Template containing `{username}` |
| `DISCORD_PROBE_TOKEN` | *(blank)* | Credential sent only to the probe |
| `DISCORD_PROBE_TOKEN_HEADER` | `Authorization` | Header carrying the probe token |
| `DISCORD_PROBE_TOKEN_SCHEME` | `Bearer` | Prefix; blank sends the raw token |

---

## Development and testing

```bash
python test_checkers.py       # 138 offline tests (interpreters, request layer, proxies, fallback)
python test_bot.py            # 63 pipeline tests (filters, budget, cache, reply, reactions)
python test_stress.py         # 17 stress tests (fuzzing, busy channels, coalescing, leaks)
python test_integration.py    # 13 integration tests (real sockets: proxies, boot, CLI)
LIVE=1 python test_checkers.py   # additionally hit the real Mojang / guns.lol endpoints

# all four suites, 231 tests, no network and no Discord token required
for f in test_checkers test_bot test_stress test_integration; do python $f.py || break; done

python -m pyflakes *.py       # lint
python checkers.py Notch      # manual CLI report
```

| File | Responsibility |
|---|---|
| `bot.py` | Discord client, message pipeline, budget, cache, reactions |
| `checkers.py` | Per-platform checkers, pure interpreters, request layer, CLI |
| `proxies.py` | `ProxyPool` rotation and health, `ProxyProvider` handed to checkers |
| `test_checkers.py` / `test_bot.py` / `test_stress.py` | Offline test suites (no network required) |
| `test_integration.py` | End-to-end tests against real local sockets (proxy servers, list host, boot) |

`test_integration.py` is the only suite that opens sockets. It starts real HTTP forward proxies,
a real proxy-list host and a stand-in for `api.instantusername.com` on loopback ports, then boots an
actual `SniperBot` through `setup_hook()` and feeds it a message — so the wiring between the modules
is covered, not just each module in isolation. It still needs no internet and no Discord token.

The status interpreters (`interpret_minecraft`, `interpret_github`, …) are pure functions of `(status, body)`, which is why the suites can cover every platform without touching the network.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Bot ignores every message | **Message Content Intent** is off in the Developer Portal, or `TARGET_CHANNEL_ID` points at another channel |
| Bot stays silent in reply mode | Missing the **Send Messages** permission in that channel — check the log line |
| Bot answers but adds no reaction | Missing the **Add Reactions** permission (react mode only) |
| Always ⚠️ on Instagram / X | Those sites are gating the host IP; configure `PROXY_URLS` |
| ⚠️ on every platform | Outbound HTTPS is blocked, or every proxy is down (check the startup banner and `Proxy … benched` logs) |
| Discord shows `skipped` | Expected: `DISCORD_CHECK_MODE=off` is the default |
| `DNS Robot browser unavailable` | Run `python -m playwright install chromium` |
| ⏳ on ordinary use | Raise `USER_MAX_CHECKS` or lower `USER_WINDOW_SECONDS` |

---

## Responsible use

This tool reports *availability*; it does not register, reserve, or claim anything. Keep the flood guard on, respect each platform's terms of service and rate limits, and keep tokens and proxy credentials in `.env` (git-ignored) or your host's secret manager — never in the repository.
