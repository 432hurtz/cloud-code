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
import crypto  # noqa: E402
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

    env.setdefault("AIDER_ANALYTICS", "false")  # no telemetry egress
    cmd = [
        "aider",
        "--model", f"ollama_chat/{MODEL}",
        "--no-git", "--yes-always", "--no-auto-commits", "--no-check-update",
        "--analytics-disable",
    ]
    if research_notes is not None:
        cmd += ["--read", str(research_notes)]
    cmd += ["--message", message]
    subprocess.run(cmd, cwd=WORKDIR, env=env, check=True, timeout=BUILD_TIMEOUT_SEC)


def zip_output() -> Path:
    zip_base = Path(f"/tmp/{WORKDIR.name}")
    return Path(shutil.make_archive(str(zip_base), "zip", root_dir=WORKDIR))


def email_zip(attachment: Path, encrypted: bool) -> None:
    msg = EmailMessage()
    # Keep the task OUT of the email subject/body when the payload is encrypted —
    # otherwise the description leaks in cleartext through Gmail.
    if encrypted:
        msg["Subject"] = "Your build (encrypted)"
        body = (
            "Your build is attached, encrypted to your age key.\n"
            "Decrypt locally:  age -d -i ~/.age/key.txt -o build.zip "
            f"{attachment.name}\n"
        )
    else:
        msg["Subject"] = f"Your build: {TASK[:60]}"
        body = f"Task:\n{TASK}\n\nThe built project is attached as a zip.\n"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(body)
    subtype = "octet-stream" if encrypted else "zip"
    with attachment.open("rb") as fh:
        msg.add_attachment(
            fh.read(), maintype="application", subtype=subtype, filename=attachment.name
        )
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def shred_paths(*paths: Path) -> None:
    """Best-effort secure deletion of build artifacts from the VM disk."""
    for p in paths:
        try:
            if p.is_dir():
                subprocess.run(
                    ["find", str(p), "-type", "f", "-exec", "shred", "-u", "{}", "+"],
                    check=False,
                )
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                subprocess.run(["shred", "-u", str(p)], check=False)
        except Exception as e:  # noqa: BLE001
            print(f"shred failed for {p}: {e}", file=sys.stderr)


def main() -> None:
    global TASK
    # 0) Decrypt the task if you sent it age-encrypted (Telegram/GCP saw only ciphertext).
    TASK = crypto.maybe_decrypt_task(TASK)

    # 1) Guardrail — informational only, never blocks (owner is the guardrail).
    _, warning = guardrail.check(TASK)
    if warning:
        tg(f"⚠️ Guardrail note: {warning}")
        print(f"guardrail note: {warning}")

    WORKDIR.mkdir(parents=True, exist_ok=True)
    tg("🚀 Building your request…")  # task text kept off the wire by default
    zip_path = deliver = None
    try:
        notes = do_research(TASK)
        run_agent(notes)
        zip_path = zip_output()
        deliver = crypto.encrypt_file(zip_path)  # → .age if a recipient key is set
        encrypted = crypto.output_is_encrypted()
        size_mb = deliver.stat().st_size / 1024 / 1024
        email_zip(deliver, encrypted)
        lock = " 🔒 encrypted to your key" if encrypted else ""
        tg(f"✅ Done ({size_mb:.1f} MB){lock}. Emailed to {os.environ['EMAIL_TO']}.")
        tg_document(deliver)
    except subprocess.TimeoutExpired:
        tg(f"⏱️ Build exceeded {BUILD_TIMEOUT_SEC // 60} min and was stopped. Try a smaller task.")
        raise
    except Exception as e:  # noqa: BLE001
        tg(f"❌ Build failed: {e}")
        raise
    finally:
        # Wipe build artifacts from the VM disk regardless of outcome.
        shred_paths(*[p for p in (WORKDIR, zip_path, deliver) if p is not None])


if __name__ == "__main__":
    main()
