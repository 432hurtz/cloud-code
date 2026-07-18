# cloud-code — self-hosted, call-only AI code builder on GCP

Text a **Telegram bot** a build request → a **Spot A100** GPU VM wakes up, runs an
open-weight coding model, **builds the project**, **emails you a zip**, pings you back
on Telegram, and **shuts itself down**. You pay only for the minutes it actually builds.

```
Telegram message ──▶ Cloud Function (always-on, ~free) ──▶ starts Spot A100 VM
                                                              │
                          emails you the zip  ◀──────────────┤ loads model + builds
                          Telegram "done" ping ◀─────────────┤
                                                              ▼
                                                      VM stops itself
```

## Why this shape

| Choice | Reason |
|---|---|
| **Call-only (scale-to-zero)** | GPU bills only while building. No idle cost. |
| **Spot A100 40GB** | ~$1.10/hr *while running*. Fast, runs a strong 32B coder at 8-bit. |
| **Telegram trigger** | Free, works from your phone, no infra to keep online. |
| **Persistent model disk** | Model weights survive stop/start — no 20GB re-download each boot. |
| **Email delivery** | Zip lands in your inbox; nothing to log into. |

### Rough cost

| Item | Bills when | ~Cost |
|---|---|---|
| Spot A100 compute | only while building | ~$1.10/hr → **~$7–10/week** at moderate use |
| Model disk (~60GB pd-balanced) | always | ~$6/mo |
| Cloud Function trigger | per message | ~$0 at personal volume |

Comfortably under a $40–50/week budget, with a much stronger GPU than a 24/7 setup could afford.

---

## Layout

```
deploy/
  create-vm.sh          One-time: create the (stopped) Spot A100 VM + model disk
  prepare-model-disk.sh One-time: download the model onto the persistent disk
  startup-script.sh     Runs on every VM boot: build → deliver → self-stop
agent/
  build_and_send.py     Runs the coding agent, zips output, emails + Telegram
  requirements.txt
trigger/
  main.py               Cloud Function: Telegram webhook → start VM with your task
  requirements.txt
  deploy-trigger.sh     Deploy the function + register the Telegram webhook
.env.example            All the config/secrets you fill in
```

---

## Setup (about 30 minutes, once)

### 0. Prerequisites
- A GCP project with billing enabled, and the `gcloud` CLI installed & authenticated.
- **Request A100 Spot quota:** *IAM & Admin → Quotas* → filter `NVIDIA A100 GPUs` (Spot)
  in your chosen region → request at least **1**. (This is the one step that can take a
  few hours for Google to approve.)
- A **Gmail App Password** (Google Account → Security → 2-Step Verification → App
  passwords) so the VM can email you the zip.
- A **Telegram bot**: message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the
  token. Then get your numeric chat id: message your new bot once, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`.

### 1. Configure
```bash
cp .env.example .env
# edit .env — fill in project, region, secrets, model, etc.
```

### 2. Create the model disk + download weights (one-time)
```bash
bash deploy/prepare-model-disk.sh
```
This spins up a cheap temporary VM, downloads the model onto a persistent disk, then
deletes the temporary VM (the disk stays).

### 3. Create the GPU VM (left stopped)
```bash
bash deploy/create-vm.sh
```

### 4. Deploy the Telegram trigger
```bash
bash trigger/deploy-trigger.sh
```

### 5. Use it
Text your bot:
> Build a FastAPI todo app with SQLite and pytest tests

You'll get: an instant "🚀 building…" reply, then a few minutes later an **email with the
zip** and a Telegram "✅ done" ping. The VM stops on its own.

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

### Guardrails — topic safety *you* set
You define what the builder will and won't work on. `GUARDRAIL_MODE` in `.env`:
- `off` — build anything.
- `keyword` — refuse tasks containing any `GUARDRAIL_BLOCK` term. Checked in the
  trigger **before the GPU even boots**, so blocked requests cost nothing.
- `strict` — keyword layer **plus** the local model judges each task against your
  plain-English `GUARDRAIL_POLICY` and refuses violations.

Edit `GUARDRAIL_BLOCK` (terms) and `GUARDRAIL_POLICY` (scope description) to taste.
A refusal pings you on Telegram with the reason.

### Anti-runaway watchdog (never runs 24/7)
Two layers stop the VM billing if anything hangs:
1. The build itself has a soft timeout (~40 min) → Telegram alert, then normal stop.
2. A **hard watchdog** in the startup script force-stops the VM after
   `MAX_RUNTIME_MIN` (default 60) **no matter what** — a wedged model server, a
   stuck build, anything — and alerts you when it fires. Combined with the
   always-run self-stop trap, the A100 can't be left running.

---

## Security notes
- The trigger **only accepts messages from your `TELEGRAM_CHAT_ID`** — nobody else can
  spend your GPU budget.
- Secrets are passed to the VM via instance metadata for simplicity. For a hardened setup,
  move them to **Secret Manager** (see comments in `deploy/create-vm.sh`).
- The VM **always self-stops** at the end of `startup-script.sh`, even on failure, so a
  crash can't leave the A100 billing.

## Tuning power vs. price
- **Cheaper:** switch `GPU_TYPE`/machine in `.env` to a **Spot L4** (`g2-standard-8`) and a
  4-bit model — under ~$5/week.
- **Faster/stronger:** keep the A100 and swap `MODEL` for a larger model, or move from
  Ollama to **vLLM** (see comment in `startup-script.sh`) for higher throughput.
