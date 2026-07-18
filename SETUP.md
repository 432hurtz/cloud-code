# Setup — step by step

End result: you text a Telegram bot, a Scaleway GPU instance wakes up, builds
your project locally, emails you an **encrypted** zip, and powers itself off.

Work through the parts in order. Parts A–F are gather-the-pieces; G onward is deploy.
Budget ~30–45 min the first time. Commands marked **(your machine)** run locally;
everything else the deploy scripts do for you.

---

## A. Scaleway account + CLI

1. Create an account at [console.scaleway.com](https://console.scaleway.com) and enable billing.
2. **GPU access:** GPU instances (L40S/L4) live in zone `fr-par-2` (also `nl-ams-1`,
   `pl-waw-2`). New accounts sometimes need identity verification or a quota bump
   before GPU types are creatable — check **Console → Instances → Create → GPU**.
3. **Install the `scw` CLI** (your machine):
   ```bash
   # macOS
   brew install scw
   # Linux
   curl -s https://raw.githubusercontent.com/scaleway/scaleway-cli/master/scripts/get.sh | sh
   ```
4. Also install **`jq`** and **`age`** locally (the scripts and decryption need them):
   ```bash
   brew install jq age          # macOS
   sudo apt install -y jq age   # Debian/Ubuntu
   ```

## B. Scaleway API key

Console → **IAM → API keys → Generate API key**.
- For least privilege: first create an IAM **Application** with a policy granting
  **InstancesFullAccess** (and **SecretManagerReadOnly** if you'll use Part F),
  then generate the key *for that application*. A personal key also works to start.
- Copy the **Access Key** (`SCWxxxx…`) and **Secret Key** (shown once).
- Get your **Project ID**: Console → project dropdown → *Settings*, or after
  `scw init`, run `scw config get default-project-id`.

Authenticate the CLI (your machine):
```bash
scw init
# paste access key, secret key, choose default project + zone (fr-par-2)
```

## C. Gmail app password

Delivery goes over Gmail SMTP (but the zip is encrypted, so Gmail only holds ciphertext).
1. Enable **2-Step Verification** on the Google account.
2. [myaccount.google.com](https://myaccount.google.com) → Security → 2-Step Verification →
   **App passwords** → generate one → copy the 16-character password.
3. You'll use: host `smtp.gmail.com`, port `587`, user = your full email, pass = that app password.

## D. Telegram bot + your chat id

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts →
   copy the **token** (looks like `123456:ABC-DEF…`).
2. **Message your new bot once** (send it any text) — a bot can't see you until you do.
3. Find your numeric **chat id** (your machine):
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[-1].message.chat.id'
   ```
   That number is `TELEGRAM_CHAT_ID` (only this chat may trigger builds).

## E. Your encryption keypair (stays on your machine)

```bash
mkdir -p ~/.age && age-keygen -o ~/.age/key.txt && chmod 600 ~/.age/key.txt
```
It prints `Public key: age1…`. Copy that **public** key into `RECIPIENT_AGE_PUBKEY`.
The private key in `~/.age/key.txt` never leaves your machine — it's the only thing
that can decrypt your builds.

## F. Fill in config

```bash
git clone <your repo> && cd cloud-code
cp .env.example .env
$EDITOR .env
```
Fill in: `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_DEFAULT_PROJECT_ID`, `SCW_ZONE`;
`SMTP_*` (from C); `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (from D);
`RECIPIENT_AGE_PUBKEY` (from E). Review `GUARDRAIL_POLICY` and `AGENT_INSTRUCTIONS`
— those are your rules and your build style, in your own words. Leave the optional
hardening ids blank for now.

---

## G. (Optional) Hardening — do this BEFORE creating the builder

Both are opt-in; skip to H if you want the simplest path first.

**Egress lockdown:**
```bash
bash deploy/create-security-group.sh
# → prints: SECURITY_GROUP_ID=...   Paste it into .env.
```

**Secret Manager** (moves Gmail password + bot token out of user-data):
```bash
bash deploy/put-secrets.sh
# → prints SMTP_PASSWORD_SECRET_ID=... and TELEGRAM_TOKEN_SECRET_ID=...
# Paste both into .env.
```
(If you do these, do them first so the create scripts pick up the ids.)

## H. Create the GPU builder (left stopped)

```bash
bash deploy/create-builder.sh
# → prints: export BUILDER_ID=<id>
export BUILDER_ID=<id>        # and/or paste it into .env
```
This creates the instance stopped — no GPU billing yet. First build is when it
actually boots.

## I. Create the controller (the trigger)

```bash
bash deploy/create-controller.sh      # reads BUILDER_ID from env or .env
```
This tiny always-on instance long-polls Telegram and starts the builder on demand.

> **Privacy alternative:** skip this script and run the poller on your own
> always-on machine instead — then the Scaleway API key never sits in the cloud:
> ```bash
> pip install requests
> set -a; source .env; set +a
> export SCW_DEFAULT_ZONE="$SCW_ZONE"     # scw must be authed (Part B)
> python3 controller/poller.py
> ```

## J. Use it

Text your bot:
> Build a FastAPI todo app with SQLite and pytest tests

- `/status` → is the builder idle or busy?
- Add web research with a line like `search: open-meteo api docs`.

**The first build is slow** (~10–20 min: the instance installs deps and pulls the
~34 GB model once). Every later build is fast. You'll get a Telegram "🚀 building…"
then an **email with an encrypted zip** + a "✅ done" ping, and the builder stops itself.

Decrypt locally:
```bash
age -d -i ~/.age/key.txt -o build.zip build-*.zip.age && unzip build.zip
```

---

## Troubleshooting

Check builder state / find its IP:
```bash
scw instance server get "$BUILDER_ID" zone="$SCW_ZONE"
```
SSH in to read logs (if you kept the inbound SSH rule):
```bash
scw instance server ssh "$BUILDER_ID" zone="$SCW_ZONE"
# on the box:
tail -f /var/log/cloud-init-output.log     # first-boot provisioning
journalctl -u code-builder -e              # per-boot build run
tail -f /var/log/ollama.log                # model server
```
Controller not responding? Check it's up and the token/chat id are right:
```bash
scw instance server ssh "<controller id>" zone="$SCW_ZONE"
journalctl -u code-controller -e
```
Fully tear down when done (deletes the instance and its volumes):
```bash
scw instance server delete "$BUILDER_ID" zone="$SCW_ZONE" with-volumes=all
scw instance server delete "<controller id>" zone="$SCW_ZONE" with-volumes=all
```

## Known spots to verify on first run

I built this against Scaleway's documented CLI/cloud-init but couldn't run the
deploys end-to-end, so sanity-check these once:
- **Root-volume sizing** in `deploy/create-builder.sh` (`root-volume=b_ssd:80GB`) —
  if the CLI rejects it, create with the default root volume and resize it in the
  console, then re-run.
- **GPU type availability** — confirm `L40S-1-48G` exists in your zone, else use
  `L4-1-24G` + a q4 `MODEL`.
- **`scw secret` / `security-group` flags** — verify against your installed `scw`
  version if you use the optional hardening.
