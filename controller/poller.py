#!/usr/bin/env python3
"""Always-on controller: long-polls Telegram and starts the GPU builder on
demand. No public webhook is exposed — it only makes outbound calls.

Runs on a tiny Scaleway instance, or on ANY always-on machine you control (a
home server / Raspberry Pi), which keeps the Scaleway API key off the cloud
entirely. Needs `scw` on PATH and these env vars:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BUILDER_ID, SCW_ZONE,
  SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_DEFAULT_PROJECT_ID, SCW_DEFAULT_ZONE

Topic-safety guardrails live on the builder (agent/guardrail.py), judged by the
model against GUARDRAIL_POLICY — no keyword pre-check here, so a request always
at least reaches the GPU and gets a real policy judgment rather than a substring
match refusing something the policy would actually allow.
"""
import base64
import json
import os
import subprocess
import time

import requests


def _resolve_token() -> str:
    """Prefer the plaintext token; else fetch it from Scaleway Secret Manager."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    sid = os.environ.get("TELEGRAM_TOKEN_SECRET_ID", "").strip()
    if sid:
        out = subprocess.run(
            ["scw", "secret", "version", "access", sid, "revision=latest_enabled", "-o", "json"],
            capture_output=True, text=True,
        )
        return base64.b64decode(json.loads(out.stdout)["data"]).decode().strip()
    raise SystemExit("no TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN_SECRET_ID set")


TOKEN = _resolve_token()
CHAT = os.environ["TELEGRAM_CHAT_ID"]
BUILDER = os.environ["BUILDER_ID"]
ZONE = os.environ["SCW_ZONE"]
API = f"https://api.telegram.org/bot{TOKEN}"

HELP = """🤖 cloud-code — your private AI code builder

I run an open-weight coder on a GPU that wakes on demand, works with you step by
step, and powers off when idle. Nothing leaves the loop unencrypted: the model
runs locally, code goes to your own Forgejo, deliverables are age-encrypted.

Just talk to me in plain English — "make a plan for a todo app", "research basic
circuits", "start a project called blog", "fix the failing tests" — and I'll route
it. The exact commands below always work too if you want to be precise.

━━ BUILD (iterative) ━━
• plan: <idea>   — start (or continue) a planning conversation, no code yet
• go  (or build) — build from the plan as it currently stands
• build: <idea>  — build something specific right away
• edit: <change> — adjust what's there
• <any message>  — treated as an edit to the active project
→ after each step I push to your Forgejo so you can review the diffs.
→ name a file or folder already in the project (e.g. "check instructions.md
  and the information folder") and I'll actually read it in, not just guess.

━━ TALK THROUGH A PLAN ━━
Once you've said 'plan: <idea>', just keep replying normally — no prefix
needed. I'll ask questions when something's unclear, give my opinion on real
tradeoffs, and ask yours. Say 'go' whenever you're ready to build; that ends
the conversation and starts fresh next time you plan.

━━ FIX EXISTING CODE (diagnostic mode) ━━
• fix: <what's broken>  — I run the project's tests, find the bug,
  repair it with minimal changes, then re-run tests to confirm
• diagnose / debug: <…> — same thing, different word

━━ RUN & CHECK ━━
• run  (or run: <cmd>)   — execute the project (or your command), show output
• install <pkgs>         — install dependencies (pip/npm)
• lint                   — run ruff and auto-fix style/errors

━━ PROJECTS (each keeps its own memory) ━━
• new: <name>    — start a fresh project
• use: <name>    — switch to an existing one
• status         — active project + last change

━━ DELIVER ━━
• ship           — zip + encrypt (age) + email you the whole project

━━ RESEARCH (the web, via Tor) ━━
• add a line to any build:  search: <query>
  → I look it up via Tor first and use it as context

━━ SEARCH YOUR OWN FILES/DOCS ━━
• find: <question>  — semantic search over the project's OWN files, not the
  web — good for digging through a big pile of docs/logs already sitting
  in its Forgejo repo
  e.g. "find anything about invoices", "search the project for files
  relating to books" (no prefix needed, I'll route it)
→ first time on a project this chunks + embeds everything (can take a
  while for a big corpus — I'll post progress); every time after that,
  only changed files get re-indexed. The index is backed up to Forgejo too,
  so a builder rebuild won't force a redo.
→ once an index exists, plan: automatically pulls in relevant background
  from it too — no need to say find: first during planning.

━━ SYSTEM ━━
• /status — is the builder idle or busy?
• /flush  — clear the queue: drop backlogged messages + cancel a queued command
• /help   — this message

━━ HOW IT WORKS ━━
• First boot is slow (~15–20 min — it loads the model); after that it's quick.
• I stay warm between messages, then power off after a few idle minutes.
  Your work is saved on the volume, so just message me to resume.
• Only your chat can trigger me.

━━ EXAMPLE SESSION ━━
new: todo
plan: a FastAPI todo app with SQLite + pytest
go
edit: add JWT auth
ship"""


def reply(text: str) -> None:
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": CHAT, "text": text}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print("reply failed:", e)


def scw(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["scw", "instance", *args], capture_output=True, text=True)


IDLE_STATES = {"stopped", "stopped in place"}  # Scaleway uses both depending on volume type


def builder_state() -> str:
    r = scw("server", "get", BUILDER, f"zone={ZONE}", "-o", "json")
    try:
        return json.loads(r.stdout).get("state", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _clean_err(r: subprocess.CompletedProcess) -> str:
    """The meaningful error text from a failed scw call (stderr first, trimmed)."""
    raw = (r.stderr or r.stdout or "unknown error").strip()
    # scw prints a blank line + "Hint:" block; keep the first substantive lines.
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    return " ".join(lines[:3])[:400] or "unknown error"


def _ok(r: subprocess.CompletedProcess, what: str) -> bool:
    """Report a failed scw call to Telegram with its real error; return success."""
    if r.returncode == 0:
        return True
    reply(f"⛔ {what} failed:\n{_clean_err(r)}")
    return False


def start_build(task: str) -> None:
    st = builder_state()
    if st == "unknown":
        reply("⛔ Can't reach Scaleway to check the builder (API key or network). "
              "Your command was NOT sent — check the controller.")
        return
    if st in IDLE_STATES:
        # Cold start: queue the command, then boot the builder into a live session.
        if not _ok(scw("user-data", "set", f"server-id={BUILDER}", "key=build-task",
                       f"content={task}", f"zone={ZONE}"), "Queuing your command"):
            return
        scw("user-data", "set", f"server-id={BUILDER}", "key=telegram-chat-id",
            f"content={CHAT}", f"zone={ZONE}")
        if not _ok(scw("server", "start", BUILDER, f"zone={ZONE}"), "Booting the GPU builder"):
            reply("↳ Your command is queued — resend once a GPU frees up "
                  "(GPU stock can be temporarily sold out).")
            return
        reply("🚀 GPU booting into a session… (first boot is slow — it loads the model)")
    elif st == "running":
        # Warm: the live session polls user-data, so just hand it the next command.
        if _ok(scw("user-data", "set", f"server-id={BUILDER}", "key=build-task",
                   f"content={task}", f"zone={ZONE}"), "Sending to the live session"):
            reply("➕ Sent to the live session.")
    else:
        reply(f"⏳ Builder is '{st}' — give it a moment and resend.")


def clear_queued_task() -> None:
    """Delete any build-task waiting on the builder so a stale/queued command
    won't run on its next boot (empty content 400s, so delete the key)."""
    scw("user-data", "delete", f"server-id={BUILDER}", "key=build-task", f"zone={ZONE}")


def handle(text: str) -> None:
    text = (text or "").strip()
    if not text or text in ("/start", "/help"):
        reply(HELP)
        return
    if text == "/status":
        st = builder_state()
        reply("Builder: " + ("🟢 idle (ready)" if st in IDLE_STATES else f"🟠 {st}"))
        return
    if text in ("/flush", "/clear"):
        # Type this from Telegram to wipe the queue: drop any Telegram backlog AND
        # cancel a build command already queued on the builder but not yet run.
        drop_pending()
        clear_queued_task()
        reply("🧹 Flushed — dropped the Telegram backlog and cancelled any queued command.")
        return
    start_build(text)


def drop_pending() -> None:
    """Discard Telegram's backlog at startup so a controller restart doesn't replay
    hours of queued messages — each of which would cold-boot the GPU builder. Without
    this, the loop below starts with offset=None and the first getUpdates pulls every
    retained update (up to 24h). deleteWebhook(drop_pending_updates=true) is the only
    'clear the queue' op Telegram offers for a polling bot."""
    try:
        r = requests.get(f"{API}/deleteWebhook", params={"drop_pending_updates": "true"}, timeout=15)
        print("dropped pending updates:", r.json().get("ok"))
    except Exception as e:  # noqa: BLE001
        print("drop_pending failed:", e)


def main() -> None:
    drop_pending()  # start clean — ignore anything queued while we were down
    reply("🟢 Controller online (running on the cloud VM). Send a build request or /status.")
    offset = None
    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates", params={"timeout": 50, "offset": offset}, timeout=60
            )
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                if str(msg.get("chat", {}).get("id")) != CHAT:
                    continue  # only the owner may trigger builds
                handle(msg.get("text"))
        except Exception as e:  # noqa: BLE001
            print("poll error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
