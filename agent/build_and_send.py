#!/usr/bin/env python3
"""Run the coding agent on the requested task, zip the output, email it, and
ping Telegram. Invoked by deploy/startup-script.sh on the GPU VM.

Flow: guardrail check -> (optional) Tor web research -> build with Aider ->
zip -> email -> Telegram notify. All config comes from environment variables
set by the startup script from instance metadata.
"""
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import guardrail  # noqa: E402
from tools import tor_search  # noqa: E402

TASK = os.environ["BUILD_TASK"]
MODEL = os.environ["MODEL"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
RESEARCH_MODE = os.environ.get("ENABLE_RESEARCH", "off").lower()
RESEARCH_MAX = int(os.environ.get("RESEARCH_MAX_RESULTS", "5"))

WORKDIR = Path(f"/tmp/build-{int(time.time())}")
TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024  # bots may send documents up to 50 MB
BUILD_TIMEOUT_SEC = 60 * 40  # soft limit; the VM watchdog is the hard backstop


def tg(text: str) -> None:
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


def research_queries(task: str) -> list[str]:
    """Explicit 'search:'/'research:' lines, plus one auto query in 'auto' mode."""
    if RESEARCH_MODE == "off":
        return []
    explicit = re.findall(r"(?im)^\s*(?:search|research):\s*(.+)$", task)
    queries = [q.strip() for q in explicit if q.strip()]
    if not queries and RESEARCH_MODE == "auto":
        queries = [task.strip().splitlines()[0][:120]]
    return queries


def do_research(task: str) -> Path | None:
    queries = research_queries(task)
    if not queries:
        return None
    tg(f"🔎 Researching ({len(queries)} quer{'y' if len(queries)==1 else 'ies'}) via Tor…")
    blocks = []
    for q in queries:
        blocks.append(tor_search.format_findings(q, tor_search.search(q, RESEARCH_MAX)))
    notes = WORKDIR / "RESEARCH.md"
    notes.write_text("# Web research (via Tor)\n\n" + "\n".join(blocks))
    return notes


def run_agent(research_notes: Path | None) -> None:
    """Drive Aider headlessly against the local Ollama endpoint."""
    env = dict(os.environ, OLLAMA_API_BASE="http://localhost:11434")
    env.setdefault("OLLAMA_CONTEXT_LENGTH", "16384")

    instructions = os.environ.get("AGENT_INSTRUCTIONS", "").strip()
    message = TASK
    if instructions:
        message = (
            "[How to build — follow these operating instructions]\n"
            f"{instructions}\n\n[Task]\n{TASK}"
        )

    cmd = [
        "aider",
        "--model", f"ollama_chat/{MODEL}",
        "--no-git", "--yes-always", "--no-auto-commits", "--no-check-update",
    ]
    if research_notes is not None:
        cmd += ["--read", str(research_notes)]
    cmd += ["--message", message]
    subprocess.run(cmd, cwd=WORKDIR, env=env, check=True, timeout=BUILD_TIMEOUT_SEC)


def zip_output() -> Path:
    zip_base = Path(f"/tmp/{WORKDIR.name}")
    return Path(shutil.make_archive(str(zip_base), "zip", root_dir=WORKDIR))


def email_zip(zip_path: Path) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Your build: {TASK[:60]}"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(f"Task:\n{TASK}\n\nThe built project is attached as a zip.\n")
    with zip_path.open("rb") as fh:
        msg.add_attachment(
            fh.read(), maintype="application", subtype="zip", filename=zip_path.name
        )
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def main() -> None:
    # 1) Guardrail — refuse out-of-scope tasks before spending build time.
    allowed, reason = guardrail.check(TASK)
    if not allowed:
        tg(f"⛔ Refused by guardrail: {reason}")
        print(f"guardrail blocked: {reason}")
        return

    WORKDIR.mkdir(parents=True, exist_ok=True)
    tg(f"🚀 Building: {TASK[:120]}")
    try:
        notes = do_research(TASK)
        run_agent(notes)
        zip_path = zip_output()
        size_mb = zip_path.stat().st_size / 1024 / 1024
        email_zip(zip_path)
        tg(f"✅ Done ({size_mb:.1f} MB). Emailed to {os.environ['EMAIL_TO']}.")
        tg_document(zip_path)
    except subprocess.TimeoutExpired:
        tg(f"⏱️ Build exceeded {BUILD_TIMEOUT_SEC // 60} min and was stopped. Try a smaller task.")
        raise
    except Exception as e:  # noqa: BLE001
        tg(f"❌ Build failed: {e}")
        raise


if __name__ == "__main__":
    main()
