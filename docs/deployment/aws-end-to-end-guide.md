# Deploying the Trading Agent on AWS — End-to-End Guide

*A complete walkthrough of how we put our bot on a real cloud server. Written so anyone — even someone who has never touched a server before — can follow along and understand WHY we did each step.*

---

## Table of Contents

1. [The Problem (Why we needed this)](#1-the-problem)
2. [What is "the Cloud" anyway?](#2-what-is-the-cloud)
3. [Why AWS, and why these specific services?](#3-why-aws)
4. [The full architecture (one big picture)](#4-the-full-architecture)
5. [Step-by-step deployment journey](#5-step-by-step-deployment-journey)
6. [How everything talks to everything else](#6-how-everything-talks-to-everything-else)
7. [Cost breakdown](#7-cost-breakdown)
8. [Daily operations](#8-daily-operations)
9. [Glossary](#9-glossary)

---

## 1. The Problem

We built an **automated trading bot** that:
- Talks to the Indian stock market via Upstox API
- Watches NIFTY, BANKNIFTY, FINNIFTY, SENSEX, BANKEX every second
- Decides when to buy/sell options based on hundreds of math rules
- Manages risk so we don't lose more than we can afford

So far we've been running this bot **on Aniket's laptop**.

**The problem:** the bot needs to run from 9:15 AM to 3:30 PM Indian market time — every single trading day. If the laptop:

- Closes the lid → bot dies
- Loses Wi-Fi → bot dies
- Battery runs out → bot dies
- Restarts for Windows update → bot dies

If the bot dies while it has an open trade, **nobody is watching that money**. The stock could fall 5% and we'd have no idea until we open the laptop again. This is unacceptable for real trading.

**The solution:** put the bot on a computer that NEVER turns off. A computer in a data center, with backup power, backup internet, and someone to watch over it.

That computer is what we call a **cloud server**, and we rented one from Amazon Web Services (AWS).

---

## 2. What is "the Cloud"?

The "cloud" is a marketing word. In reality, it just means **someone else's computer** that you rent over the internet.

Imagine Amazon owns 50 giant warehouses around the world. Each warehouse has **thousands of computers** stacked floor-to-ceiling. These warehouses have:

- Diesel generators for power outages
- Two separate internet connections from different providers
- Security guards 24/7
- Air conditioning to prevent overheating
- Engineers on-call

When you "rent a cloud server", you're saying: *"Amazon, please reserve one of those computers for me. I'll pay you by the hour."*

The computer Amazon reserves for you is called a **virtual machine** (VM). It's not a whole physical PC — Amazon splits one big physical machine into many "virtual" ones, like apartments inside a building. That's how they can sell millions of "servers" and still make a profit.

**Real-world analogy:**

| Real world | Cloud world |
|---|---|
| Building a house | Buying your own server (expensive, slow, hard) |
| Renting an apartment | Renting a VM from AWS (cheap, instant, flexible) |
| Apartment building | A physical Amazon server |
| Individual apartments | Virtual machines |
| Building's electricity, water, internet | AWS's power, cooling, network |

---

## 3. Why AWS?

There are several cloud providers — Amazon Web Services (AWS), Google Cloud (GCP), Microsoft Azure, Oracle Cloud, DigitalOcean, etc. Why did we pick AWS?

### Reasons:

1. **Largest market share** — most tutorials, documentation, Stack Overflow answers exist for AWS. Easier to learn.
2. **Mumbai region** — AWS has a data center in Mumbai (called `ap-south-1`). Our trading API (Upstox) is in Mumbai. The closer the two are, the faster they can talk to each other. **Speed matters for trading.**
3. **Free tier** — new AWS accounts get $200 in credits. We pay nothing for ~6 months.
4. **Many services in one place** — compute, database, network, security, all from one company. Easier to manage.

### Why we DIDN'T pick the alternatives:

| Alternative | Why we didn't pick it |
|---|---|
| **Google Cloud** | Free tier is in US only (250ms latency to Mumbai = slow) |
| **Oracle Cloud** | More generous free tier (24 GB RAM forever free!) BUT they were "out of capacity" for ARM servers in Mumbai when we tried. We kept a backup plan there. |
| **Render / Heroku** | Server "sleeps" if no traffic for 15 min — terrible for a bot that must be alive 24/7 |
| **Build our own server** | Costs thousands, maintenance nightmare, single point of failure |

### Which AWS services we use:

We used 6 AWS services. Here's what each one does, in plain English:

| AWS Service | What it does | Why we need it |
|---|---|---|
| **EC2** | Rents you a virtual computer | This is where our bot actually runs |
| **VPC** | A private network for your stuff | Isolates our server from other AWS customers |
| **Security Groups** | A firewall | Controls who's allowed to talk to our server |
| **IAM** | Identity & permissions | Lets our bot use other AWS services securely |
| **Billing & Budgets** | Cost tracking | Alerts us if we accidentally spend money |
| **Bedrock** *(optional)* | Access to AI models via AWS | Cheaper than going direct to Anthropic |

---

## 4. The Full Architecture

Here's the complete picture of what we built. Don't worry if it looks complex — we'll walk through every box.

```
                              YOU (anywhere, any device)
                                       │
                                       │  https://...trycloudflare.com/dashboard
                                       │  (login: aniket / password)
                                       ▼
                          ┌──────────────────────────┐
                          │   Cloudflare Tunnel      │  ← free service that gives us
                          │   (public HTTPS URL)     │     a public address with SSL
                          └────────────┬─────────────┘
                                       │
                                       │  encrypted tunnel
                                       ▼
       ╔═══════════════════════════════════════════════════════════╗
       ║   AWS EC2 Instance (a Linux computer in Mumbai)           ║
       ║   IP: 13.233.87.243                                        ║
       ║   2 vCPU, 1 GB RAM, 30 GB disk, Ubuntu 24.04              ║
       ║                                                             ║
       ║   ┌───────────────────────────────────────────────────┐   ║
       ║   │  Docker (manages our containers)                   │   ║
       ║   │                                                     │   ║
       ║   │  ┌─────────────┐  ┌─────────────┐  ┌────────────┐  │   ║
       ║   │  │  Postgres   │  │   Redis     │  │    App     │  │   ║
       ║   │  │  (database) │  │   (cache)   │  │  (FastAPI) │  │   ║
       ║   │  │             │  │             │  │            │  │   ║
       ║   │  │  Stores:    │  │  Holds:     │  │  Spawns:   │  │   ║
       ║   │  │  - ticks    │  │  - kill     │  │  - market  │  │   ║
       ║   │  │  - trades   │  │    switch   │  │    worker  │  │   ║
       ║   │  │  - tokens   │  │  - tick     │  │  - regime  │  │   ║
       ║   │  │  - decisions│  │    pubsub   │  │    worker  │  │   ║
       ║   │  │             │  │             │  │  - strategy│  │   ║
       ║   │  │             │  │             │  │    worker  │  │   ║
       ║   │  └─────────────┘  └─────────────┘  └─────┬──────┘  │   ║
       ║   │                                          │         │   ║
       ║   └──────────────────────────────────────────┼─────────┘   ║
       ║                                              │             ║
       ╚══════════════════════════════════════════════│═════════════╝
                                                      │
                                                      │  Outbound API calls
                                                      ▼
                          ┌───────────────────┐  ┌──────────────────┐
                          │   Upstox API      │  │  Anthropic API   │
                          │   (market data,   │  │  (Claude AI)     │
                          │   place orders)   │  │                  │
                          └───────────────────┘  └──────────────────┘
```

**Reading this diagram top-to-bottom:**

1. **You** open a browser on any device (laptop, phone)
2. The browser hits **Cloudflare Tunnel** — a public URL with proper HTTPS encryption
3. Cloudflare forwards the request through a private tunnel to **AWS EC2**
4. On EC2, **Docker** is running 3 containers:
   - **Postgres** — the database (like a giant Excel sheet of every trade, tick, decision)
   - **Redis** — fast in-memory storage for things like "is the kill switch on?" and live tick broadcasting
   - **App** — the FastAPI web server that ALSO spawns 3 worker processes inside itself
5. The App container's workers talk to:
   - **Upstox** — to receive market ticks (NIFTY price every second)
   - **Anthropic** — to get Claude's opinion on trade ideas

---

## 5. Step-by-Step Deployment Journey

This is the actual sequence we followed. Each step explains *what we did* and *why*.

### Step 1: Create an AWS account

**What:** Signed up at https://aws.amazon.com with a personal Gmail and Indian address.

**Why:**
- AWS needs to know who they're invoicing (legal requirement)
- The address determines tax setup (GST in India)
- Used a personal email (not work email) to keep AWS billing separate from anything work-related

**Gotcha:** AWS asked for a credit/debit card to "verify identity". They charge ₹2 (refunded later). This is anti-fraud — they want to confirm you're a real person.

### Step 2: Pick the "Free Plan"

**What:** AWS now offers two account types — Free (6 months, $200 credit, limited features) and Paid (full features, pay-as-you-go).

**Why we picked Free:**
- We get $200 worth of usage absolutely free for 6 months
- Hard cap — we literally CANNOT accidentally spend more than $200
- After 6 months, account auto-closes if not upgraded (no surprise bills)

This is one of those rare "fail-safe" options in cloud computing.

### Step 3: Set the region to Mumbai

**What:** Changed the AWS region dropdown (top-right of console) from "Sydney" (default) to "Asia Pacific (Mumbai) — ap-south-1".

**Why region matters:**
- The Indian stock market and Upstox API servers are in Mumbai
- If we used Sydney: every API call would travel India → Sydney → India (round-trip ~300 ms)
- With Mumbai: round-trip is ~5-15 ms
- For trading where prices change every millisecond, this is the difference between "good fill" and "lost money"

**Real-world analogy:** It's like the difference between having your warehouse next door vs. having to drive to another city every time you need an item.

### Step 4: Set up a "Zero Spend Budget"

**What:** Created an automatic alert that emails us if AWS ever tries to charge **even $0.01**.

**Why:**
- AWS bills are complex. Easy to accidentally turn on a paid service.
- Free Tier limits are confusing — exceeding them costs real money.
- This is a safety net: if anything ever goes wrong, we know immediately.

This was a **5-minute setup that could save thousands of rupees** if anything misconfigured.

### Step 5: Launch an EC2 Instance (the actual server)

**What:** Created a virtual computer with these specs:

| Setting | Value | Why |
|---|---|---|
| **Operating System** | Ubuntu 24.04 LTS | Free, widely-supported Linux. "LTS" = Long-Term Support (5 years of security patches). |
| **Instance type** | t3.micro | 2 vCPU + 1 GB RAM. Smallest "burstable" option. Within Free Tier. |
| **Storage** | 30 GB | Enough for Docker images + database + logs for months |
| **SSH Key** | RSA, .pem file | Like a special password file. ONLY way to log into the server. |
| **Network** | Default VPC + public IP | Public IP = an internet address others can reach |
| **Firewall** | SSH only, from MY IP | Block everyone from connecting except Aniket's home internet |

**Why these specific choices:**

- **t3.micro is tiny (1 GB RAM)**, but that's all the Free Tier allows. With Docker tuning + 2 GB swap space, it's enough.
- **Ubuntu 24.04**, not Amazon Linux, because our code was tested on Ubuntu. Same OS = no surprises.
- **SSH from "My IP" only**, not "Anywhere", because if anyone could connect, bots from China/Russia would try to break in within minutes. Restricting by IP is a huge security win.

**The SSH key concept:**

Instead of using a password to log in (which is guessable), AWS uses a "key pair" — two huge random numbers that are mathematically linked. The **private key** stays on Aniket's laptop. The **public key** is on the server. Logging in works like this:

1. Server says: "Encrypt this random message with your private key"
2. Laptop encrypts it
3. Server decrypts the message using the public key
4. If it decrypts cleanly → you're authenticated
5. If not → access denied

A password can be guessed (Brute force in seconds). A 4096-bit RSA key would take **millions of years** to crack.

**Critical rule:** if you lose your .pem file, you lose access to the server **forever**. There's no "forgot password" link.

### Step 6: SSH into the server

**What:** From Aniket's Windows machine, we connected to the server using:

```powershell
ssh -i "C:\Users\91996\Downloads\aws-trading-key.pem" ubuntu@13.233.87.243
```

This is like remote-controlling the cloud server through a text-only terminal.

**Why it's text-only:** servers don't have monitors, keyboards, or mice. They're just computers in a rack. Engineers control them entirely through text commands. It's faster, scriptable, and uses way less internet bandwidth than a full graphical desktop.

### Step 7: Install Docker on the server

**What:** Installed **Docker** with two commands:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
```

**Why Docker?**

Docker is one of the most important inventions in modern software. Without Docker:
- You'd install Postgres on the server, configure it
- Then install Redis, configure it
- Then install Python, install all our libraries, configure paths
- Pray nothing conflicts

With Docker, you write a **recipe file** (`docker-compose.yml`) that says "give me Postgres v16, Redis v7, and my app — with these exact versions and these settings." Docker downloads and runs each one in an isolated **container**. Containers can't interfere with each other.

**Analogy:** Docker is like microwave-meal trays. Each meal (container) has its own compartments (filesystem, libraries, network). You can heat up an Italian meal and an Indian meal at the same time, on the same microwave, and they don't mix.

### Step 8: Add 2 GB of "swap space"

**What:**
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

**Why:**

Our server only has 1 GB of RAM. RAM is fast, expensive memory. Disk is slow, cheap storage. Swap is a clever trick — it pretends part of the slow disk is RAM. When real RAM runs out, the OS moves rarely-used data to swap. Apps think they have more RAM than they actually do.

**Why 2 GB?** Postgres + Redis + our app + 3 workers + OS = roughly 1.5 GB at peak. With 1 GB RAM + 2 GB swap = 3 GB total. Plenty of buffer.

**Trade-off:** swap is ~100x slower than RAM. If your app constantly hits swap, performance tanks. But for occasional spikes, it's a lifesaver — prevents the "out of memory" crashes.

### Step 9: Clone the code from GitHub

**What:**
```bash
git clone https://github.com/Aniket242424/Prototype.git Trading_Agent
cd Trading_Agent
git checkout phase-4
```

**Why GitHub?**

GitHub is where our code "lives" online. The server needs the code to run, but we don't have a USB drive to plug into the cloud server. So we:
1. Write code on laptop
2. Push (upload) to GitHub
3. Pull (download) on the server with `git clone`

It also means **every line of our code has a history**. We can roll back if we ever break something.

### Step 10: Copy the secret `.env` file

**What:** Used `scp` (secure copy) to transfer the `.env` file from laptop to server:

```powershell
scp -i aws-trading-key.pem .env ubuntu@13.233.87.243:~/Trading_Agent/.env
```

**Why this file is special:**

`.env` contains **secrets** — Upstox API keys, Anthropic API keys, encryption keys, database passwords. These can NEVER be in GitHub (because GitHub is public, anyone could steal them).

So `.env` is kept on disk, copied directly machine-to-machine, never uploaded anywhere.

**Rule we follow:** if it would let an attacker steal our money, it goes in `.env` and NEVER in code.

### Step 11: Modify `docker-compose.yml` for low memory

**What:** Edited the Docker recipe file to tune Postgres for our tiny 1 GB server:

```yaml
postgres:
  image: postgres:16-alpine
  command: postgres -c shared_buffers=64MB -c max_connections=20
```

**Why:**

By default, Postgres tries to grab 25% of system RAM (would be 256 MB on our 1 GB server). It also allows 100 simultaneous connections. We don't need that — our app uses at most 5 connections.

**Lesson:** software defaults are designed for big servers. On tiny servers, you must tune.

### Step 12: Build & start everything

**What:**
```bash
docker compose up -d --build
```

This single command:
1. Pulls Postgres and Redis images from Docker Hub (their official package repository)
2. Builds our app's Docker image from the `Dockerfile`
3. Starts all 3 containers
4. Connects them via a private Docker network so they can find each other by name

**What happened over the next 5 minutes:**
- Postgres started, created the trading_agent database, ran our migrations (creating ~20 tables)
- Redis started, ready to cache things
- Our app started uvicorn, ran the supervisor, spawned 3 worker subprocesses (market_data, regime, strategy)
- All 3 workers connected to Postgres and Redis successfully

### Step 13: Authenticate with Upstox (OAuth flow)

**What:** Started a one-time login flow:
1. Aniket opened `https://api.upstox.com/v2/login/authorization/dialog?client_id=...` in browser
2. Logged in with Upstox credentials
3. Upstox redirected back to `http://localhost:8000/auth/upstox/callback?code=XXX`
4. Our server captured the code, exchanged it with Upstox for a real **access token**
5. Token is encrypted (with Fernet) and stored in Postgres

**Why this dance?**

We don't want our app to know Aniket's Upstox password. If our app gets hacked, the attacker shouldn't be able to log in to Upstox. So:

- Aniket logs into Upstox directly (Upstox never tells us the password)
- Upstox gives our app a special **token** that says "this app can act on Aniket's behalf"
- The token expires every 24 hours, forcing daily re-auth (Upstox safety rule)

This is **OAuth 2.0** — the same standard used by "Log in with Google / Facebook" buttons everywhere.

### Step 14: SSH Tunnel for local-only callbacks

**What:**
```powershell
ssh -L 8000:localhost:8000 ubuntu@13.233.87.243 -N
```

**Why this is needed:**

The OAuth callback URL registered with Upstox is `http://localhost:8000/auth/upstox/callback`. But our server's localhost is on AWS, not on Aniket's laptop. How can the browser (on the laptop) redirect to "localhost" and have it reach AWS?

**Answer:** SSH Tunnel. The `-L 8000:localhost:8000` says "any traffic to port 8000 on my laptop, secretly forward through SSH to the server's port 8000."

So:
- Browser hits `localhost:8000` on the laptop
- SSH tunnel forwards to AWS EC2's port 8000
- Our app (running on AWS) receives the OAuth code
- Saves the token in AWS Postgres

It's like a secret pipe — invisible to Upstox, fully encrypted, ours.

### Step 15: Add password protection to the dashboard

**What:** Updated our FastAPI app to require a username/password for `/dashboard` and `/control` endpoints. Added a new file `auth_basic.py`.

**Why:**
Once we put the dashboard on a public URL (next step), anyone in the world could hit:
- `POST /control/kill-switch/trip` → instantly kill our bot
- `POST /control/workers/stop` → stop the workers
- `GET /dashboard/api/status` → see our positions and money

So we added HTTP Basic Auth. Browser pops up a username/password box. Wrong creds → 401 Unauthorized.

**Important detail:** we used `secrets.compare_digest` instead of `==` to compare passwords. Why? `==` exits early when it finds a wrong character, leaking timing info that attackers can use to guess the password one letter at a time. `compare_digest` always takes the same time regardless. This is a real attack — Google's actual login system uses `compare_digest` for this reason.

### Step 16: Set up Cloudflare Tunnel for a public URL

**What:** Installed Cloudflare's `cloudflared` tool on the EC2 server and ran:

```bash
cloudflared tunnel --url http://localhost:8000
```

It printed: `https://gathered-zones-atlas-ind.trycloudflare.com`

**Why Cloudflare Tunnel?**

We could have opened port 8000 to the world in the AWS firewall. But that would:
- Expose us directly to internet scanners
- Have no HTTPS (no `https://` lock icon)
- Need a domain name

Cloudflare Tunnel solves all 3 problems:
- It's a "reverse tunnel" — our server reaches OUT to Cloudflare, no inbound port opens
- Cloudflare provides automatic HTTPS via their certificates
- They give us a free `*.trycloudflare.com` URL

**Analogy:** Imagine you have a private office in a secure building. You want clients to visit, but you don't want strangers walking off the street into the building. Solution: a reception desk in a downtown plaza that takes appointments and escorts clients to your office. Cloudflare Tunnel is that reception desk.

### Step 17: (Optional) Set up AWS Bedrock for AI

**What:** We ALSO wired up AWS Bedrock as an alternative way to call Claude AI.

**Why optional:**

The AI advisor (Claude) reviews each potential trade before placing it. We can talk to Claude two ways:
- **Anthropic Direct** — call `api.anthropic.com` with an Anthropic API key (paid via Anthropic)
- **AWS Bedrock** — call Claude through AWS, billed via AWS credit

Both work identically code-wise. Our advisor.py supports both via a single setting: `ADVISOR_BACKEND=anthropic` or `bedrock`.

We tried Bedrock first because the AWS $200 credit could absorb the cost. But Bedrock requires a Marketplace billing setup that AWS Free Plan doesn't satisfy. So we fell back to direct Anthropic (using Aniket's purchased $20 credit).

**The lesson:** designing systems with **fallback options** is a sign of mature engineering. We can flip a single env var and switch backends without touching any other code.

---

## 6. How Everything Talks to Everything Else

A more detailed view of what happens during a typical market-hours minute:

```
Market is open. 09:30 IST. NIFTY is at 23,400.

[09:30:00.000]
  Upstox WebSocket sends a "tick" message: NIFTY = 23,401.50
       ↓
  Our market_data_worker receives the tick
       ↓
  Worker writes the tick to Postgres
       ↓
  Worker publishes the tick to Redis pubsub channel "ticks:NIFTY"

[09:30:00.050]
  Our regime_worker (subscribed to "ticks:NIFTY") receives the tick
       ↓
  Adds it to the rolling 60-candle buffer
       ↓
  Re-computes ADX, EMA, RV, VWAP indicators
       ↓
  Classifies regime: "TREND_UP at confidence 0.85"
       ↓
  Saves regime state to Postgres
       ↓
  Publishes to Redis "opportunity:active" if opportunity score > 0.55

[09:30:01.000]
  Our strategy_worker (signal cycle, runs every 30s):
       ↓
  Reads latest opportunity from Postgres
       ↓
  Asks each of 4 strategies: "Would you trade this?"
  - EMA Crossover Trend: yes (ADX 28, EMA stack aligned, regime TREND_UP)
  - ORB: no (window closed)
  - Vol Expansion: no (RV ratio too low)
  - Gap Continuation: no (no gap today)
       ↓
  Strategy emits StrategySignal
       ↓
  Worker calls Claude AI advisor:
    - Sends prompt to Anthropic API (or Bedrock)
    - Claude returns: {"decision": "CALL", "advisor_score": 0.7, "rationale": "..."}
       ↓
  Worker calls Risk Engine (16 checks):
    - Kill switch? Not tripped ✓
    - Daily loss < 2%? Yes ✓
    - Open positions < 1? Yes ✓
    - Spread < 50 bps? Yes ✓
    - ... 12 more checks ...
    - ALL PASS → APPROVE
       ↓
  Worker calls Execution Engine:
    - Places paper-LIMIT order at NIFTY 23400 CE @ ₹140
    - PaperBroker simulates a fill in ~80ms
       ↓
  Worker records the position in Postgres
       ↓
  Position Manager starts watching every second

[Every second from now]
  Position Manager:
    - Reads latest NIFTY price from Redis
    - Computes unrealized P&L
    - Checks: did spot hit stop? Did spot hit target? Time to trail?
    - If exit needed → calls Execution Engine emergency_exit()
    - At 15:15 IST → forces all positions flat (Indian market closing soon)
```

This whole loop runs continuously, **6 hours a day, every trading day**.

---

## 7. Cost Breakdown

What does it actually cost to run this for a month?

| Service | Free tier | What we use | Real cost |
|---|---|---|---|
| **AWS EC2 t3.micro** | 750 hours/month free | Always-on (730 hours) | ₹0 (under credit) |
| **AWS storage (EBS)** | 30 GB free | 30 GB | ₹0 |
| **AWS data transfer** | 100 GB out free | ~5 GB/month | ₹0 |
| **AWS Free Plan credit** | $200 total | ~₹500/month projected | ₹0 (until credit exhausted) |
| **Cloudflare Tunnel** | Unlimited | ~5 GB/month | ₹0 forever |
| **GitHub (public repo)** | Unlimited | Code repo | ₹0 |
| **Upstox API** | Free for retail | ~50k req/day | ₹0 |
| **Anthropic Claude Sonnet 4.6** | $5 promo + $20 paid | ~₹1.20/call × 90 calls/month | ₹110/month |

**TOTAL: ~₹110/month** (basically just AI advisor calls)

After the AWS Free Plan $200 credit runs out (~6 months), EC2 t3.micro costs about ₹600-800/month at retail price. So ongoing total would be ~₹800/month — still cheaper than a single losing trade.

**If we switched the AI backend to Claude Haiku 4.5:** cost drops to ₹30/month. Could be a future optimization.

---

## 8. Daily Operations

What you do every day (now and going forward):

### Every morning before market open (one-time, ~30 sec)
1. Open https://api.upstox.com/v2/login/authorization/dialog?client_id=...
2. Log in to Upstox → approve
3. Browser auto-redirects → token saved on AWS

(This is the **daily Upstox token refresh** — annoying but currently required. The next session's first task is to automate this away using Upstox's refresh_token mechanism.)

### During market hours
- Open the dashboard: https://gathered-zones-atlas-ind.trycloudflare.com/dashboard
- Login: `aniket` / `LSVPkH6BvxwQn8Ny`
- Watch:
  - Regime panel — what the market looks like right now
  - Opportunity panel — what setups the bot sees
  - Open Positions — any active trades
  - Risk Decisions — what the bot considered and approved/rejected
  - Recent Trades — paper P&L

### If something looks wrong
- Check the **Infrastructure** card — is Postgres/Redis green?
- Check **Authentication** — is the token expired?
- Check **Phase 4 Worker** — is it alive?

If anything is red:
- SSH into the server: `ssh -i aws-trading-key.pem ubuntu@13.233.87.243`
- Check logs: `cd Trading_Agent && docker compose logs -f app`
- Restart workers: hit the "Restart All Workers" button on the dashboard

---

## 9. Glossary

**API** — Application Programming Interface. A way for one program to talk to another over the internet. Upstox has an API; our bot uses it to receive prices and place orders.

**AWS** — Amazon Web Services. The biggest cloud computing provider in the world.

**Bedrock** — AWS's service for accessing AI models (Claude, Llama, etc.) through AWS billing.

**Cloudflare** — Internet infrastructure company. We use them for free public URLs with HTTPS.

**Container** — A lightweight, isolated package that contains an application and all its dependencies. We have 3: Postgres, Redis, App.

**Docker** — Software that runs containers. Lets us package and deploy our app reliably.

**EC2** — AWS's service for renting virtual computers. We rent a t3.micro instance.

**Env file (`.env`)** — A text file storing secret keys (API keys, passwords). Never committed to GitHub.

**FastAPI** — A Python framework for building web servers. Our app is built with FastAPI.

**Free Tier** — AWS's free quota for new accounts. We use this so we pay ₹0.

**Git / GitHub** — Version control. Tracks every change to our code. GitHub hosts the code online.

**HTTP Basic Auth** — Simple username/password protection. Browser shows a popup.

**IAM** — Identity and Access Management. AWS's permission system.

**OAuth** — Industry-standard protocol for "log in with X" flows. We use it for Upstox.

**Postgres (PostgreSQL)** — A reliable database. We use it to store all trades, ticks, decisions.

**Redis** — A fast in-memory database. We use it for the kill switch and live tick broadcasting.

**Region** — A geographic group of AWS data centers. We use `ap-south-1` (Mumbai).

**SSH** — Secure Shell. Encrypted remote access to a server.

**SSH Tunnel** — Forwarding a port through SSH. We use it for the OAuth callback.

**Swap** — Disk space pretending to be RAM. We added 2 GB to compensate for our small 1 GB RAM server.

**VPC** — Virtual Private Cloud. Your isolated network in AWS.

**Worker** — A background process that does one specific job. We have 3: market_data, regime, strategy.

---

## What you learned

If you understood this guide, you now know:

- Why cloud servers exist and how they replace physical computers
- How AWS, the most popular cloud, works
- The pieces that make up a modern web application (database, cache, app, network)
- What Docker does and why it's a big deal
- How SSH and SSH tunneling work
- The basics of OAuth 2.0
- How to deploy a real application to a cloud server end-to-end
- Why cost-control measures (budgets, billing alerts) matter
- The kinds of security trade-offs engineers think about

These concepts apply to nearly every modern internet company — Flipkart, Zerodha, Swiggy, Netflix. They all use these same patterns, just at massive scale.

**You're not just running a trading bot. You're running a production system with the same architectural patterns the world's biggest companies use.**

---

*Last updated: 2026-05-15*
*Trading_Agent project — Phase 4 complete.*
