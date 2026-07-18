"""Cloud Function (gen2, HTTP) that receives Telegram webhook updates and, for
an authorized chat, starts the stopped GPU VM with the requested build task.

Env vars (set at deploy time by deploy-trigger.sh):
  PROJECT_ID, ZONE, VM_NAME, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os

import functions_framework
import requests
from google.cloud import compute_v1

PROJECT = os.environ["PROJECT_ID"]
ZONE = os.environ["ZONE"]
VM_NAME = os.environ["VM_NAME"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT = os.environ["TELEGRAM_CHAT_ID"]

HELP = (
    "👋 Send me a build request and I'll spin up the GPU, build it, and email "
    "you the zip.\n\nExample:\n  Build a FastAPI todo app with SQLite and pytest"
)


def reply(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def start_build(chat_id, task):
    client = compute_v1.InstancesClient()
    inst = client.get(project=PROJECT, zone=ZONE, instance=VM_NAME)

    # One build at a time: only start from a stopped/terminated state.
    if inst.status != "TERMINATED":
        reply(chat_id, f"⚙️ Busy (VM state: {inst.status}). Try again shortly.")
        return

    items = {i.key: i.value for i in inst.metadata.items}
    items["build-task"] = task
    items["telegram-chat-id"] = str(chat_id)
    meta = compute_v1.Metadata(
        fingerprint=inst.metadata.fingerprint,
        items=[compute_v1.Items(key=k, value=v) for k, v in items.items()],
    )
    client.set_metadata(
        project=PROJECT, zone=ZONE, instance=VM_NAME, metadata_resource=meta
    )
    client.start(project=PROJECT, zone=ZONE, instance=VM_NAME)
    reply(chat_id, "🚀 GPU booting and building… you'll get an email + a ping here.")


@functions_framework.http
def telegram_webhook(request):
    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return ("ok", 200)

    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    # Only the owner may spend the GPU budget.
    if str(chat_id) != ALLOWED_CHAT:
        if chat_id is not None:
            reply(chat_id, "⛔ Not authorized.")
        return ("ok", 200)

    if not text or text in ("/start", "/help"):
        reply(chat_id, HELP)
        return ("ok", 200)

    start_build(chat_id, text)
    return ("ok", 200)
