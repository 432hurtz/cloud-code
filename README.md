# cloud-code — self-hosted, call-only AI code builder on Scaleway (EU)

Text a **Telegram bot** a build request → a **Scaleway GPU instance** wakes up, runs an
open-weight coding model **locally** (nothing leaves the box), **builds the project**,
**encrypts and emails you the zip**, pings you back on Telegram, and **powers itself off**.
You pay only for the minutes it actually builds.

```
Telegram message ──▶ controller (tiny always-on instance, long-poll) ──▶ starts GPU builder
                                                                            │
                       emails you the ENCRYPTED zip  ◀─────────────────────┤ builds locally
                       Telegram "done" ping ◀──────────────────────────────┤ (Ollama, no egress)
                                                                            ▼
                                                             builder powers itself off
```

## Why this shape

| Choice | Reason |
|---|---|
| **Scaleway (fr-par-2)** | EU/GDPR jurisdiction, no ad-tech; hourly GPU with billing paused while off. |
| **Call-only (scale-to-zero)** | GPU bills only while building. Powered off = compute not billed. |
| **L40S 48GB** | Runs the 32B coder at q8 comfortably. (`L4-1-24G` + a q4 model is the cheap option.) |
| **Local model (Ollama)** | Your code and the model's reasoning never touch any third-party AI service. |
| **age end-to-end encryption** | Deliverables are encrypted to *your* key — Gmail/Telegram/host see only ciphertext. |
| **Controller long-poll** | No public webhook; only outbound calls. Can run on your own machine instead. |

### Rough cost

| Item | Bills when | ~Cost |
|---|---|---|
| L40S GPU compute | only while building | ~€1.4/hr → **a few €/week** at moderate use |
| Root volume (~80 GB block SSD) | always | ~€2/mo |
| Controller (PLAY2-NANO) | always | ~€2–7/mo (or free on your own hardware) |

---

## Layout

```
deploy/
  create-builder.sh          One-time: create the (stopped) GPU builder
  builder-cloud-init.yaml    First-boot provisioning + the per-boot build service
  create-controller.sh       One-time: create the always-on Telegram controller
  controller-cloud-init.yaml First-boot provisioning for the controller
  create-security-group.sh   Optional: egress-locked security group
  put-secrets.sh             Optional: move secrets into Scaleway Secret Manager
agent/                        Host-agnostic build logic (shipped to the builder)
  build_and_send.py           Runs the agent, researches, encrypts, emails, shreds
  crypto.py  guardrail.py  tools/tor_search.py  requirements.txt
controller/
  poller.py                  Long-polls Telegram → starts the builder
  requirements.txt
.env.example                 All the config/secrets you fill in
```

---

## Setup (about 30 minutes, once)

### 0. Prerequisites
- A **Scaleway account** and the [`scw` CLI](https://github.com/scaleway/scaleway-cli)
  installed. Create an **API key** (Console → IAM → API keys), scoped to Instances.
- **GPU availability:** L40S / L4 live in `fr-par-2` (also `nl-ams-1`, `pl-waw-2`).
- A **Gmail App Password** (Google Account → Security → 2-Step Verification → App
  passwords) so the builder can email you the zip.
- A **Telegram bot**: message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the
  token. Get your numeric chat id: message the bot once, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`.
- An **age keypair** for encryption, generated **on your own machine**:
  `age-keygen -o ~/.age/key.txt` → paste the printed public key into `.env`.

### 1. Configure
```bash
cp .env.example .env
# edit .env — Scaleway keys, model, secrets, your age public key, guardrails
```

### 2. Create the GPU builder (left stopped)
```bash
bash deploy/create-builder.sh          # prints BUILDER_ID — save it
export BUILDER_ID=<the id it printed>   # (or put it in .env)
```

### 3. Create the controller (or run the poller on your own machine)
```bash
bash deploy/create-controller.sh
```
> Privacy option: instead of a Scaleway controller, run `controller/poller.py` on
> your own always-on machine with the same env vars — then the Scaleway API key
> never touches the cloud.

### 4. Use it
Text your bot:
> Build a FastAPI todo app with SQLite and pytest tests

You'll get a "🚀 building…" reply, then (first build is slower — it installs deps and
pulls the model once) an **email with the encrypted zip** and a Telegram "✅ done" ping.
The builder powers itself off. Decrypt locally:
```bash
age -d -i ~/.age/key.txt -o build.zip build-*.zip.age
```

> ⚠️ **First run:** this kit targets Scaleway's documented CLI/cloud-init, but the
> deploy scripts haven't been run end-to-end here — do a dry run and check the two
> host-specific spots noted in `deploy/create-builder.sh` (root-volume sizing) and
> that your chosen GPU type is available in your zone.

---

## Features

### Telegram commands
- **Any text** → a build request.
- **`/status`** → is the builder idle (ready) or busy?
- **`/help`** → usage.

### Web search via Tor
Give a build live internet context, routed through Tor (rotating exit IP, not the
VM's address). Controlled by `ENABLE_RESEARCH` in `.env`:
- `off` — no web access.
- `manual` — only queries you name: put `search: <query>` lines in your task.
- `auto` — `manual` plus one query auto-derived from the task.

Findings are written to `RESEARCH.md` and handed to the coding model as read-only
context before it builds. Example task:
> Build a CLI weather tool.
> search: open-meteo free weather api docs

### Guardrails — topic safety *you* set (in your own words)
You control the builder with **two plain-English prompts**, not just keywords:

- **`GUARDRAIL_POLICY` — what to block.** In `strict` mode the local model reads
  this rulebook and judges every request against it, reasoning about intent
  rather than matching words. Write it like you'd brief a person: what's allowed,
  what's off-limits, and what to do when unsure. This is the real gate.
- **`AGENT_INSTRUCTIONS` — how to act.** Injected into every build as the agent's
  operating instructions — your standards, defaults, and voice (e.g. "always
  include a README and tests", "prefer well-known libraries"). Shapes *how* it
  builds; the policy governs *what* it will touch.

`GUARDRAIL_MODE` in `.env`:
- `strict` *(recommended)* — the model judges each request against your
  `GUARDRAIL_POLICY`, **plus** the fast `GUARDRAIL_BLOCK` term check runs first.
  **Fail-closed:** if the judge can't run, the request is refused, not allowed.
- `keyword` — only the fast `GUARDRAIL_BLOCK` term check (no model reasoning).
- `off` — build anything.

`GUARDRAIL_BLOCK` is an optional list of obvious no-go terms refused **before the
GPU even boots** (costs nothing) — a cheap first pass, not the main control. A
refusal pings you on Telegram with the reason.

### Anti-runaway watchdog (never runs 24/7)
Two layers stop the builder billing if anything hangs:
1. The build itself has a soft timeout (~40 min) → Telegram alert, then normal stop.
2. A **hard watchdog** in the per-boot service force-stops the instance after
   `MAX_RUNTIME_MIN` (default 60) **no matter what** — a wedged model server, a
   stuck build, anything — and alerts you when it fires. Combined with the
   always-run self-stop trap, the GPU can't be left running.

---

## Privacy & security notes
- **The model runs entirely local.** Ollama + Aider talk only to `localhost`; your
  code and the model's reasoning never leave the instance.
- **End-to-end encryption.** Set `RECIPIENT_AGE_PUBKEY` and every deliverable is
  encrypted to your key before it's emailed — Gmail, Telegram, and Scaleway only
  ever hold ciphertext. Your private key never touches the cloud.
- **Artifacts are shredded** from the instance disk after each build, and Aider
  telemetry is disabled.
- The controller **only accepts messages from your `TELEGRAM_CHAT_ID`**, and long-polls
  (no public webhook is exposed).
- Secrets ride in instance user-data (readable only by root on the box). Run the
  controller on your own machine to keep the API key off the cloud.

### Optional hardening (two extra steps)
- **Egress lockdown.** `bash deploy/create-security-group.sh` creates a security group
  that default-drops outbound and allows only 80/443, 587 (Gmail), 53 (DNS), 123 (NTP);
  Tor is pinned to 443/80 so it still works. Paste the printed id into
  `SECURITY_GROUP_ID` and the deploy scripts attach it. This stops a compromised
  dependency from opening arbitrary exfil channels — the build has no other egress.
- **Secret Manager.** `bash deploy/put-secrets.sh` stores the Gmail password + bot token
  in Scaleway Secret Manager; paste the printed ids into `SMTP_PASSWORD_SECRET_ID` /
  `TELEGRAM_TOKEN_SECRET_ID` and re-run the create scripts. The instances then fetch
  those values at boot instead of carrying them in user-data. Scope the API key to
  Secret Manager read.

## Tuning power vs. price
- **Cheaper:** set `BUILDER_TYPE=L4-1-24G` and a 4-bit `MODEL`
  (`qwen2.5-coder:32b-instruct-q4_K_M`).
- **Faster/stronger:** keep the L40S and swap `MODEL` for a larger model, or move
  Ollama → **vLLM** in `builder-cloud-init.yaml` for higher throughput.
