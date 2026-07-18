#!/usr/bin/env python3
"""Run the coding agent on the requested task, zip the output, email it, and
ping Telegram. Invoked by deploy/startup-script.sh on the GPU VM.

All configuration comes from environment variables set by the startup script
from instance metadata.
"""
import os
import shutil
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests

TASK = os.environ["BUILD_TASK"]
MODEL = os.environ["MODEL"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

WORKDIR = Path(f"/tmp/build-{int(time.time())}")
TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024  # bots may send documents up to 50 MB


def tg(text: str) -> None:
    """Best-effort Telegram message; never fatal."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        print(f"telegram notify failed: {e}", file=sys.stderr)


def tg_document(path: Path) -> None:
    if not (TG_TOKEN and TG_CHAT) or path.stat().st_size > TELEGRAM_DOC_LIMIT:
        return
    try:
        with path.open("rb") as fh:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT},
                files={"document": fh},
                timeout=120,
            )
    except Exception as e:  # noqa: BLE001
        print(f"telegram document failed: {e}", file=sys.stderr)


def run_agent() -> None:
    """Drive Aider headlessly against the local Ollama endpoint. Aider creates
    and edits files in WORKDIR based on the task."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OLLAMA_API_BASE="http://localhost:11434")
    cmd = [
        "aider",
        "--model", f"ollama_chat/{MODEL}",
        "--no-git",
        "--yes-always",
        "--no-auto-commits",
        "--no-check-update",
        "--message", TASK,
    ]
    # Give the model plenty of context room.
    env.setdefault("OLLAMA_CONTEXT_LENGTH", "16384")
    subprocess.run(cmd, cwd=WORKDIR, env=env, check=True, timeout=60 * 45)


def zip_output() -> Path:
    zip_base = Path(f"/tmp/{WORKDIR.name}")
    archive = shutil.make_archive(str(zip_base), "zip", root_dir=WORKDIR)
    return Path(archive)


def email_zip(zip_path: Path) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Your build: {TASK[:60]}"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(
        f"Task:\n{TASK}\n\nThe built project is attached as a zip.\n"
    )
    with zip_path.open("rb") as fh:
        msg.add_attachment(
            fh.read(), maintype="application", subtype="zip",
            filename=zip_path.name,
        )
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def main() -> None:
    tg(f"🚀 Building: {TASK[:120]}")
    try:
        run_agent()
        zip_path = zip_output()
        size_mb = zip_path.stat().st_size / 1024 / 1024
        email_zip(zip_path)
        tg(f"✅ Done ({size_mb:.1f} MB). Emailed to {os.environ['EMAIL_TO']}.")
        tg_document(zip_path)  # also drop it in the chat if small enough
    except subprocess.TimeoutExpired:
        tg("⏱️ Build timed out (45 min). Try a smaller task.")
        raise
    except Exception as e:  # noqa: BLE001
        tg(f"❌ Build failed: {e}")
        raise


if __name__ == "__main__":
    main()
