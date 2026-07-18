#!/usr/bin/env python3
"""Always-on controller: long-polls Telegram and starts the GPU builder on
demand. No public webhook is exposed — it only makes outbound calls.

Runs on a tiny Scaleway instance, or on ANY always-on machine you control (a
home server / Raspberry Pi), which keeps the Scaleway API key off the cloud
entirely. Needs `scw` on PATH and these env vars:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BUILDER_ID, SCW_ZONE, GUARDRAIL_BLOCK,
  SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_DEFAULT_PROJECT_ID, SCW_DEFAULT_ZONE
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
BLOCK = [t.strip().lower() for t in os.environ.get("GUARDRAIL_BLOCK", "").split(",") if t.strip()]
API = f"https://api.telegram.org/bot{TOKEN}"

HELP = (
    "👋 Send me a build request and I'll spin up the GPU, build it, and email "
    "you the (encrypted) zip.\n\n"
    "Commands:\n  /status — is the builder idle or busy?\n  /help — this message\n\n"
    "Add web research with a line like:\n  search: <query>\n\n"
    "Example:\n  Build a FastAPI todo app with SQLite and pytest"
)


def reply(text: str) -> None:
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": CHAT, "text": text}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print("reply failed:", e)


def scw(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["scw", "instance", *args], capture_output=True, text=True)


def builder_state() -> str:
    r = scw("server", "get", BUILDER, f"zone={ZONE}", "-o", "json")
    try:
        return json.loads(r.stdout).get("state", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def start_build(task: str) -> None:
    st = builder_state()
    if st != "stopped":
        reply(f"⚙️ Busy (builder is '{st}'). Try again when it's idle.")
        return
    scw("user-data", "set", f"server-id={BUILDER}", "key=build-task", f"content={task}", f"zone={ZONE}")
    scw("user-data", "set", f"server-id={BUILDER}", "key=telegram-chat-id", f"content={CHAT}", f"zone={ZONE}")
    scw("server", "start", BUILDER, f"zone={ZONE}")
    reply("🚀 GPU booting and building… you'll get an email + a ping here.")


def handle(text: str) -> None:
    text = (text or "").strip()
    if not text or text in ("/start", "/help"):
        reply(HELP)
        return
    if text == "/status":
        st = builder_state()
        reply("Builder: " + ("🟢 idle (ready)" if st == "stopped" else f"🟠 {st}"))
        return
    hit = next((t for t in BLOCK if t in text.lower()), None)
    if hit:
        reply(f"⛔ Refused: request matches blocked topic '{hit}'.")
        return
    start_build(text)


def main() -> None:
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
