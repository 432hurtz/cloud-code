#!/usr/bin/env python3
"""Iterative, live-session build agent for the cloud-code GPU builder.

Unlike the one-shot build_and_send.py, this keeps a *session*: it handles a
command, stays warm, and waits for the next one — powering the VM off only after
IDLE_TIMEOUT of silence. Work persists per-project on the instance's root volume
(survives stop/start), and every change is pushed to your self-hosted Forgejo so
you review diffs in its web UI and steer over Telegram.

Commands (sent as the `build-task` instance user-data key by the controller):
  plan: <idea>          think first — model writes a plan, no code changes
  go | build[: <idea>]  execute: Aider edits + auto-commits + pushes to Forgejo
  edit: <change>        apply a change (same as build, phrased as an edit)
  ship                  zip the project, encrypt (age), email it
  new: <name>           start a fresh project
  use: <name>           switch the active project
  find: <question>      semantic search over the project's OWN files (not the web)
  status                report active project + builder state
  <anything else>       treated as an edit on the active project

Memory: each project keeps Aider's chat history and PROJECT_NOTES.md on the
volume; both are fed back as context so the model resumes with full picture.

Config comes from environment (set by run.sh from instance user-data):
  MODEL, IDLE_TIMEOUT_MIN, AGENT_INSTRUCTIONS, ENABLE_RESEARCH, RESEARCH_MAX_RESULTS,
  FORGEJO_URL, FORGEJO_USER, FORGEJO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
  SCW_* (for reading/clearing the command user-data key and self-stop).
"""
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import shlex
from email.message import EmailMessage
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import corpus_index  # noqa: E402
import crypto  # noqa: E402
import guardrail  # noqa: E402
from tools import tor_search  # noqa: E402

MODEL = os.environ["MODEL"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT_MIN", "8")) * 60
RESEARCH_MODE = os.environ.get("ENABLE_RESEARCH", "off").lower()
RESEARCH_MAX = int(os.environ.get("RESEARCH_MAX_RESULTS", "5"))
AUTO_FIX_ROUNDS = int(os.environ.get("AUTO_FIX_ROUNDS", "2"))

FORGEJO_URL = os.environ.get("FORGEJO_URL", "").rstrip("/")   # e.g. http://100.96.252.114:3300
FORGEJO_USER = os.environ.get("FORGEJO_USER", "")
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN", "")

OLLAMA = "http://localhost:11434"
PROJECTS = Path("/opt/projects")
ACTIVE_FILE = PROJECTS / ".active"
BUILD_TIMEOUT_SEC = 60 * 40
TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024


# ── Telegram ────────────────────────────────────────────────────────────────
def _tg_chunks(text: str, limit: int = 4000) -> list:
    """Split a message to fit Telegram's 4096-char hard cap (margin for safety),
    breaking on newline boundaries when possible. The chat/talk shell can emit
    long plans/READMEs; without this an over-limit reply 400'd and vanished."""
    chunks: list = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:  # no sensible newline near the end → hard cut
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
        if len(chunks) >= 8:  # safety cap against a runaway reply flooding the chat
            if text:
                chunks.append(text[: limit - 15] + "\n…(truncated)")
            break
    return chunks or [""]


def tg(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        print(text)
        return
    for chunk in _tg_chunks(text or "(no response)"):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": chunk}, timeout=15,
            )
            if r.status_code != 200:  # e.g. 400 too-long / 429 rate-limit — don't fail silently
                print(f"telegram send failed {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"telegram notify failed: {e}", file=sys.stderr)


corpus_index.set_notifier(tg)


def tg_document(path: Path) -> None:
    if not (TG_TOKEN and TG_CHAT) or path.stat().st_size > TELEGRAM_DOC_LIMIT:
        return
    try:
        with path.open("rb") as fh:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT}, files={"document": fh}, timeout=120,
            )
    except Exception as e:  # noqa: BLE001
        print(f"telegram document failed: {e}", file=sys.stderr)


# ── Instance metadata / command queue ───────────────────────────────────────
_IID: str | None = None


def ud(key: str) -> str:
    """Read an instance user-data key via the scw CLI (already authenticated),
    NOT the local metadata proxy directly. That proxy (169.254.42.42/user_data/*)
    requires a privileged (<1024) source port; plain `requests` can't bind one and
    got a 400 whose body — "invalid argument(s)" — is truthy text, not empty. That
    made build-task polling see a permanent "command" and loop forever re-running
    it every 8s. Going through scw sidesteps the proxy, and any failure (missing
    key, API error, anything) now safely resolves to "" instead of garbage text.
    """
    global _IID
    if _IID is None:
        _IID = instance_id() or ""
    if not _IID:
        return ""
    zone = os.environ.get("SCW_DEFAULT_ZONE", "")
    r = subprocess.run(
        ["scw", "instance", "user-data", "get", f"server-id={_IID}", f"key={key}", f"zone={zone}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def scw(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["scw", "instance", *args], capture_output=True, text=True)


def instance_id() -> str:
    try:
        return requests.get("http://169.254.42.42/conf?format=json", timeout=5).json().get("id", "")
    except Exception:  # noqa: BLE001
        return ""


def clear_command(iid: str, zone: str) -> None:
    """Delete build-task so we don't reprocess it (empty content 400s, so delete)."""
    if iid:
        scw("user-data", "delete", f"server-id={iid}", "key=build-task", f"zone={zone}")


# ── Projects & memory ───────────────────────────────────────────────────────
def slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-").lower() or "default"


def active_project() -> str:
    if ACTIVE_FILE.exists():
        return ACTIVE_FILE.read_text().strip() or "default"
    return "default"


def set_active(name: str) -> None:
    PROJECTS.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(name)


def project_dir(name: str) -> Path:
    return PROJECTS / name


def notes_path(name: str) -> Path:
    return project_dir(name) / "PROJECT_NOTES.md"


def append_note(name: str, line: str) -> None:
    p = notes_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    header = "" if p.exists() else f"# {name} — project notes\n\n"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{header}- [{stamp}] {line}\n")


# ── Forgejo ─────────────────────────────────────────────────────────────────
def forgejo_ready() -> bool:
    return bool(FORGEJO_URL and FORGEJO_USER and FORGEJO_TOKEN)


def forgejo_repo_exists(name: str) -> bool:
    if not forgejo_ready():
        return False
    try:
        h = {"Authorization": f"token {FORGEJO_TOKEN}"}
        r = requests.get(f"{FORGEJO_URL}/api/v1/repos/{FORGEJO_USER}/{name}", headers=h, timeout=15)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def forgejo_ensure_repo(name: str) -> None:
    """Create the private repo if it doesn't exist (idempotent)."""
    if not forgejo_ready():
        return
    h = {"Authorization": f"token {FORGEJO_TOKEN}"}
    r = requests.get(f"{FORGEJO_URL}/api/v1/repos/{FORGEJO_USER}/{name}", headers=h, timeout=15)
    if r.status_code == 200:
        return
    requests.post(
        f"{FORGEJO_URL}/api/v1/user/repos", headers=h, timeout=20,
        json={"name": name, "private": True, "default_branch": "main",
              "description": "cloud-code iterative build"},
    )


def remote_url(name: str) -> str:
    # token in the URL — traffic rides Tailscale (WireGuard), so it stays private.
    base = FORGEJO_URL.split("://", 1)[-1]
    return f"http://{FORGEJO_USER}:{FORGEJO_TOKEN}@{base}/{FORGEJO_USER}/{name}.git"


# A network git op (clone/fetch/push) to an unreachable Forgejo used to hang
# FOREVER — no timeout — which blocked session_agent and defeated the idle
# self-stop, so the GPU kept billing until the 60-min watchdog. Guard every
# network git op two ways: git's own low-speed abort (bails if the transfer
# stalls) AND a hard subprocess timeout (catches a dead TCP connect, which the
# low-speed check can't, since no bytes ever move).
GIT_NET_TIMEOUT = 45
_GIT_NET_OPTS = ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=20"]


def _git_timed_out(desc: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=desc, returncode=124, stdout="",
        stderr=f"timed out after {GIT_NET_TIMEOUT}s (Forgejo unreachable?)")


def git(name: str, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    d = project_dir(name)
    try:
        return subprocess.run(
            ["git", "-C", str(d), *args], capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ, GIT_AUTHOR_NAME="cloud-code", GIT_AUTHOR_EMAIL="builder@local",
                     GIT_COMMITTER_NAME="cloud-code", GIT_COMMITTER_EMAIL="builder@local"),
        )
    except subprocess.TimeoutExpired:
        return _git_timed_out(" ".join(args))


def git_clone(name: str, dest: Path) -> subprocess.CompletedProcess:
    """Clone from Forgejo with the same fail-fast guards as git()."""
    try:
        return subprocess.run(
            ["git", *_GIT_NET_OPTS, "clone", remote_url(name), str(dest)],
            capture_output=True, text=True, timeout=GIT_NET_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _git_timed_out("clone")


def ensure_git(name: str) -> None:
    d = project_dir(name)
    # Forgejo is the source of truth: if this builder doesn't have the project
    # locally but it exists in Forgejo (e.g. after the builder was recreated),
    # clone it back so work survives builder churn.
    if not d.exists() and forgejo_repo_exists(name):
        r = git_clone(name, d)
        if r.returncode != 0:
            tg(f"⚠️ couldn't clone '{name}' from Forgejo: {r.stderr.strip()[:150]}")
            # A timed-out/partial clone can leave a broken dir — clear it so the
            # init path below starts a clean local repo instead of choking on it.
            if d.exists() and not (d / ".git" / "HEAD").exists():
                shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").exists():
        git(name, "init", "-b", "main")
        (d / "README.md").write_text(f"# {name}\n\nBuilt iteratively by cloud-code.\n")
        git(name, "add", "-A")
        git(name, "commit", "-m", "init project")
    if forgejo_ready():
        forgejo_ensure_repo(name)
        git(name, "remote", "remove", "origin")
        git(name, "remote", "add", "origin", remote_url(name))
        # Sync down anything added directly in Forgejo since we last touched
        # this project (e.g. a file uploaded via its web UI) — we only clone
        # once above, so without this, an external edit would be invisible
        # and our next force-push could even discard it. No-op on a repo we
        # just cloned/created (no origin/main to reset to yet).
        git(name, *_GIT_NET_OPTS, "fetch", "origin", "main", timeout=GIT_NET_TIMEOUT)
        if git(name, "rev-parse", "--verify", "origin/main").returncode == 0:
            git(name, "reset", "--hard", "origin/main")
    ensure_index_ignored(name)


def ensure_index_ignored(name: str) -> None:
    """Keep the search index (a growing binary blob) out of both aider's
    context AND `main`'s git history — .gitignore is the one that actually
    matters: without it, every 'build'/'fix' commit's `git add -A` would
    sweep index.db/vectors.f32 into main. The index only ever lives on the
    dedicated orphan backup branch (see corpus_index.backup_to_origin)."""
    d = project_dir(name)
    wanted = [f"{corpus_index.INDEX_DIRNAME}/", ".conversation.json"]
    changed = False
    for fname in (".gitignore", ".aiderignore"):
        p = d / fname
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        add = [w for w in wanted if w not in existing.splitlines()]
        if not add:
            continue
        p.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n")
                     + "\n".join(add) + "\n", encoding="utf-8")
        git(name, "add", fname)
        changed = True
    # Untrack the conversation log if a prior build already committed it.
    if (d / ".conversation.json").exists():
        git(name, "rm", "--cached", "--quiet", ".conversation.json")
    if changed and git(name, "diff", "--cached", "--quiet").returncode != 0:
        git(name, "commit", "-m", "chore: ignore internal index + conversation files")


def push(name: str) -> str | None:
    """Push to Forgejo; return the browsable repo URL on success."""
    if not forgejo_ready():
        return None
    r = git(name, *_GIT_NET_OPTS, "push", "-u", "origin", "main", "--force", timeout=GIT_NET_TIMEOUT)
    if r.returncode != 0:
        tg(f"⚠️ push to Forgejo failed: {r.stderr.strip()[:200]}")
        return None
    return f"{FORGEJO_URL}/{FORGEJO_USER}/{name}"


# ── Model calls ─────────────────────────────────────────────────────────────
def ollama_chat(system: str, user: str, num_ctx: int = 16384) -> str:
    try:
        r = requests.post(
            f"{OLLAMA}/api/chat", timeout=BUILD_TIMEOUT_SEC,
            json={"model": MODEL, "stream": False,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "options": {"num_ctx": num_ctx}},
        )
        return r.json().get("message", {}).get("content", "").strip()
    except Exception as e:  # noqa: BLE001
        return f"(model error: {e})"


def ollama_multiturn(system: str, history: list, num_ctx: int = 32768) -> str:
    try:
        r = requests.post(
            f"{OLLAMA}/api/chat", timeout=BUILD_TIMEOUT_SEC,
            json={"model": MODEL, "stream": False,
                  "messages": [{"role": "system", "content": system}, *history],
                  "options": {"num_ctx": num_ctx}},
        )
        return r.json().get("message", {}).get("content", "").strip()
    except Exception as e:  # noqa: BLE001
        return f"(model error: {e})"


# ── Conversational planning ─────────────────────────────────────────────────
# A real back-and-forth, not a one-shot prompt: the model can ask clarifying
# questions and state opinions rather than immediately handing back a "final"
# plan. History persists per-project; every plain message (no explicit
# command) continues it automatically while it's open. Say 'go' when ready —
# build reads whatever's currently in PLAN.md, updated after every turn.
PLAN_CHAT_SYS = (
    "You are a senior engineer collaborating on a plan with the project owner, "
    "through an ongoing conversation — not a one-shot report. Be genuinely "
    "collaborative:\n"
    "- Ask a clarifying question when something real is ambiguous or "
    "underspecified — don't silently guess on things that matter.\n"
    "- State your own opinion when there's a real tradeoff, and say why.\n"
    "- Explicitly ask the owner's opinion on decisions that are theirs to make.\n"
    "- Do NOT write code — this is planning, not implementation.\n"
    "Always end your reply with a 'Plan so far:' section — a concise, numbered "
    "summary of what's been decided/assumed so far, updated to reflect this "
    "turn. Keep it current even while points are still open."
)


def chat_path(name: str) -> Path:
    return project_dir(name) / ".plan_chat.json"


def load_chat(name: str) -> list:
    p = chat_path(name)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_chat(name: str, history: list) -> None:
    p = chat_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(history, indent=2), encoding="utf-8")


def clear_chat(name: str) -> None:
    p = chat_path(name)
    if p.exists():
        p.unlink()


def chat_active(name: str) -> bool:
    return bool(load_chat(name))


# ── Named-file/folder context ─────────────────────────────────────────────
# Lets "check folder X and file Y.md for background" actually pull in real
# content instead of hoping the model notices them on its own.
_SKIP_DIRS = {".git", ".aider.tags.cache.v4", "__pycache__", "node_modules", ".venv",
              corpus_index.INDEX_DIRNAME}
_SKIP_NAMES = {"PLAN.md", "PROJECT_NOTES.md", "RESEARCH.md", "DIAGNOSTICS.md",
               ".aider.chat.history.md", ".aider.input.history", ".plan_chat.json",
               ".conversation.json"}


def _mentioned(token: str, text: str) -> bool:
    """Whole-token, case-insensitive match — skips very short names (avoids
    'app'/'src'-style false positives) and avoids matching inside a longer
    unrelated filename."""
    if len(token) < 4:
        return False
    return re.search(r"(?<![\w.-])" + re.escape(token) + r"(?![\w.-])", text, re.I) is not None


def find_referenced_paths(d: Path, text: str, max_files: int = 25) -> list:
    """Find files/folders under the project whose name is named in `text`. A
    matched directory pulls in every file under it (bounded)."""
    if not d.exists() or not text.strip():
        return []
    found: list = []
    seen: set = set()

    def add_file(p: Path) -> None:
        if p in seen or len(found) >= max_files:
            return
        seen.add(p)
        found.append(p)

    try:
        entries = sorted(d.rglob("*"))
    except OSError:
        return []
    for p in entries:
        if len(found) >= max_files:
            break
        rel_parts = p.relative_to(d).parts
        if any(part in _SKIP_DIRS for part in rel_parts) or p.name in _SKIP_NAMES:
            continue
        if p.is_dir():
            if _mentioned(p.name, text):
                for f in sorted(p.rglob("*")):
                    if f.is_file() and not any(part in _SKIP_DIRS for part in f.relative_to(d).parts):
                        add_file(f)
        elif p.is_file() and (_mentioned(p.name, text) or _mentioned(p.stem, text)):
            add_file(p)
    return found[:max_files]


def read_bounded(d: Path, paths: list, per_file_cap: int = 6000, total_cap: int = 24000) -> str:
    """Read file contents for model context, capped so a big folder can't blow
    the context window. Skips anything not readable as UTF-8 text."""
    blocks, total = [], 0
    for p in paths:
        if total >= total_cap:
            break
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(text) > per_file_cap:
            text = text[:per_file_cap] + "\n...(truncated)"
        text = text[: total_cap - total]
        blocks.append(f"--- {p.relative_to(d)} ---\n{text}")
        total += len(text)
    return "\n\n".join(blocks)


def aider_edit(name: str, message: str, extra_reads: list | None = None) -> None:
    """Drive Aider against the project repo — it edits files and auto-commits."""
    d = project_dir(name)
    env = dict(os.environ, OLLAMA_API_BASE=OLLAMA)
    # 32K = qwen2.5-coder's native window; ~8GB KV cache on top of the ~34GB q8
    # weights fits the L40S-48G, and lets aider hold much larger files in context.
    env.setdefault("OLLAMA_CONTEXT_LENGTH", "32768")
    env.setdefault("AIDER_ANALYTICS", "false")
    instructions = os.environ.get("AGENT_INSTRUCTIONS", "").strip()
    notes = notes_path(name)
    full = message
    if instructions:
        full = f"[Operating instructions]\n{instructions}\n\n[Task]\n{message}"
    # Nudge the model to WRITE files, not just describe. Vague asks with no filename
    # ("you need to create a script that…") otherwise get a prose reply and aider
    # creates nothing (the vulnscan no-code case). This makes loose phrasing work too.
    full += ("\n\n[Output requirement] Implement this by creating or editing ACTUAL FILES "
             "in the project and outputting their full contents — do NOT merely describe, "
             "explain, or discuss it. If no filename is given, pick a sensible one for the "
             "language (e.g. a .py file for Python).")
    cmd = [
        "aider", "--model", f"ollama_chat/{MODEL}",
        # 'whole' (model rewrites entire files) is far more reliable for local/abliterated
        # models than aider's default SEARCH/REPLACE 'diff' format, which they mangle — a
        # slightly-off diff makes aider silently apply NOTHING (the "no code produced"
        # symptom). Whole-file output is much easier for a weaker model to get right.
        "--edit-format", "whole",
        "--yes-always", "--auto-commits", "--no-check-update", "--analytics-disable",
    ]
    # Feed the standing memory into every build so the plan + history carry over:
    #   PLAN.md          — the current plan (so `go` actually follows it)
    #   PROJECT_NOTES.md — running log of decisions/steps
    #   RESEARCH.md      — this turn's web findings, if any
    # (Aider also keeps its own chat history on the volume across build turns.)
    plan_file = d / "PLAN.md"
    if plan_file.exists():
        cmd += ["--read", str(plan_file)]
    if notes.exists():
        cmd += ["--read", str(notes)]
    research_file = d / "RESEARCH.md"
    if research_file.exists():
        cmd += ["--read", str(research_file)]
    for p in (extra_reads or []):
        cmd += ["--read", str(p)]
    cmd += ["--message", full]
    subprocess.run(cmd, cwd=d, env=env, check=False, timeout=BUILD_TIMEOUT_SEC)


# ── Research (reused) ───────────────────────────────────────────────────────
def do_research(name: str, task: str) -> Path | None:
    if RESEARCH_MODE == "off":
        return None
    explicit = re.findall(r"(?im)^\s*(?:search|research):\s*(.+)$", task)
    queries = [q.strip() for q in explicit if q.strip()]
    if not queries and RESEARCH_MODE == "auto":
        queries = [task.strip().splitlines()[0][:120]]
    if not queries:
        return None
    tg(f"🔎 Researching ({len(queries)}) via Tor…")
    blocks = [tor_search.format_findings(q, tor_search.search(q, RESEARCH_MAX)) for q in queries]
    out = project_dir(name) / "RESEARCH.md"
    out.write_text("# Web research (via Tor)\n\n" + "\n".join(blocks), encoding="utf-8")
    return out


# ── Ship (final delivery, reuses the one-shot email/encrypt path) ────────────
def ship(name: str) -> None:
    d = project_dir(name)
    if not d.exists():
        tg(f"Nothing to ship — project '{name}' doesn't exist yet.")
        return
    zip_base = Path(f"/tmp/{name}-{int(time.time())}")
    zip_path = Path(shutil.make_archive(str(zip_base), "zip", root_dir=str(d)))
    deliver = crypto.encrypt_file(zip_path)
    encrypted = crypto.output_is_encrypted()
    size_mb = deliver.stat().st_size / 1024 / 1024
    lock = " 🔒 encrypted" if encrypted else ""

    # Email is best-effort — some hosts block outbound SMTP by default (seen live
    # on Scaleway). Telegram delivery below must still happen either way, since
    # it's the reliable path; don't let an SMTP failure take it down too.
    emailed = False
    try:
        msg = EmailMessage()
        msg["Subject"] = f"cloud-code: {name} (encrypted)" if encrypted else f"cloud-code: {name}"
        msg["From"] = os.environ["EMAIL_FROM"]
        msg["To"] = os.environ["EMAIL_TO"]
        body = f"Project '{name}' is attached."
        if encrypted:
            body += f"\nDecrypt:  age -d -i ~/.age/key.txt -o {name}.zip {deliver.name}\n"
        msg.set_content(body)
        with deliver.open("rb") as fh:
            msg.add_attachment(fh.read(), maintype="application",
                               subtype="octet-stream" if encrypted else "zip", filename=deliver.name)
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=20) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
        emailed = True
    except Exception as e:  # noqa: BLE001
        print(f"[ship] email delivery failed, falling back to Telegram only: {e}")

    where = f" to {os.environ['EMAIL_TO']}" if emailed else " (email delivery failed — sent via Telegram instead)"
    tg(f"📦 Shipped '{name}' ({size_mb:.1f} MB){lock}{where}.")
    tg_document(deliver)
    for p in (zip_path, deliver):
        try:
            p.unlink()
        except OSError:
            pass


# ── Command handling ────────────────────────────────────────────────────────
def summarize_change(name: str) -> str:
    log = git(name, "log", "-1", "--pretty=%s").stdout.strip()
    stat = git(name, "show", "--stat", "--oneline", "HEAD").stdout.strip().splitlines()
    files = [ln for ln in stat[1:] if "|" in ln]
    return f"{log}\n" + ("\n".join(files[:12]) if files else "(no file changes)")


def run_tests(name: str) -> str:
    """Best-effort: run the project's pytest suite to surface failures for the fixer."""
    d = project_dir(name)
    has_tests = (d / "tests").is_dir() or any(d.glob("test_*.py")) or \
        any(d.glob("*_test.py")) or any(d.rglob("test_*.py"))
    if not has_tests:
        return ""
    try:
        if kali_ready():
            full_cmd = f"cd {shlex.quote(str(d))} && {shlex.join(['python', '-m', 'pytest', '-q'])}"
            r = subprocess.run(
                ["docker", "exec", "kali", "bash", "-lc", full_cmd],
                capture_output=True, text=True, timeout=180
            )
        else:
            r = subprocess.run(["python", "-m", "pytest", "-q"], cwd=str(d),
                               capture_output=True, text=True, timeout=180)
        return (r.stdout + "\n" + r.stderr).strip()[-3000:]
    except Exception as e:  # noqa: BLE001
        return f"(could not run tests: {e})"


# ── Action helpers (one per command; called by the dispatcher) ──────────────
def act_status(arg: str) -> None:
    name = active_project()
    exists = project_dir(name).exists()
    last = git(name, "log", "-1", "--pretty=%h %s").stdout.strip() if exists else "—"
    chat = f"\n💬 Planning conversation open ({len(load_chat(name)) // 2} turns) — say 'go' to build" if chat_active(name) else ""
    idx = corpus_index.stats(project_dir(name)) if exists else None
    index_line = f"\n📚 Search index: {idx['chunks']} chunks across {idx['files']} files" if idx else ""
    tg(f"Active project: '{name}'\nLast commit: {last}{chat}{index_line}")


def act_new(arg: str) -> None:
    name = slug(arg)
    set_active(name)
    ensure_git(name)
    url = push(name)  # so the init commit is visible in Forgejo right away
    tg(f"📁 New project: '{name}' (now active)." + (f"\n\n🔗 {url}" if url else ""))


def act_use(arg: str) -> None:
    name = slug(arg)
    fresh = not project_dir(name).exists()
    set_active(name)
    ensure_git(name)
    if fresh:
        push(name)
    tg(f"📁 {'Started' if fresh else 'Switched to'} project: '{name}'.")


def act_plan(arg: str) -> None:
    name = active_project()
    ensure_git(name)
    d = project_dir(name)
    idea = arg.strip() or "the current project"
    history = load_chat(name)
    continuing = bool(history)
    tg("🧠 Thinking…" if continuing else "🧠 Planning…")

    refs = find_referenced_paths(d, idea)
    if refs:
        tg("📎 Reading: " + ", ".join(str(p.relative_to(d)) for p in refs))
    ref_text = read_bounded(d, refs) if refs else ""

    # Pull in relevant background from an existing search index (see 'find:'),
    # if one's already been built — never trigger a fresh index here, since a
    # first-time index over a big corpus can take hours and would silently
    # stall the planning conversation.
    corpus_text = ""
    query = arg.strip()
    if query and corpus_index.stats(d):
        hits = corpus_index.search(d, query, top_k=6)
        if hits:
            tg("📚 Also pulling in indexed background: " + ", ".join(sorted({h["path"] for h in hits})))
            corpus_text = "\n\n".join(f"--- {h['path']} (part {h['chunk_index']}) ---\n{h['content']}" for h in hits)

    extras = []
    if not continuing:
        # First turn only — subsequent turns already have this via history.
        notes = notes_path(name).read_text(encoding="utf-8") if notes_path(name).exists() else ""
        readme = d / "README.md"
        readme_text = readme.read_text(encoding="utf-8")[:4000] if readme.exists() else ""
        if notes:
            extras.append(f"Project notes so far:\n{notes}")
        if readme_text:
            extras.append(f"README.md:\n{readme_text}")
    if ref_text:
        extras.append(f"Referenced files/folders (named just now):\n{ref_text}")
    if corpus_text:
        extras.append(f"Relevant background found via semantic search of the project's indexed files:\n{corpus_text}")
    user_msg = ("\n\n".join(extras) + f"\n\n{idea}") if extras else idea

    history.append({"role": "user", "content": user_msg})
    reply = ollama_multiturn(PLAN_CHAT_SYS, history)
    history.append({"role": "assistant", "content": reply})
    save_chat(name, history)

    append_note(name, f"PLAN turn: {idea[:120]}")
    (d / "PLAN.md").write_text(f"# Plan for {name}\n\n{reply}\n", encoding="utf-8")
    git(name, "add", "-A"); git(name, "commit", "-m", f"plan: {idea[:60]}")
    url = push(name)
    tg(f"🧠 {reply[:3500]}" + (f"\n\n🔗 {url}" if url else "")
       + "\n\n(keep talking, or say 'go' when you're ready to build)")


def act_research(arg: str) -> None:
    name = active_project()
    ensure_git(name)
    topic = arg.strip()
    if not topic:
        tg("What should I research?")
        return
    tg(f"🔎 Researching '{topic}' via Tor…")
    findings = tor_search.format_findings(topic, tor_search.search(topic, RESEARCH_MAX))
    (project_dir(name) / "RESEARCH.md").write_text(
        "# Web research (via Tor)\n\n" + findings, encoding="utf-8")
    append_note(name, f"RESEARCH: {topic[:120]}")
    git(name, "add", "-A"); git(name, "commit", "-m", f"research: {topic[:60]}")
    url = push(name)
    tg(f"🔎 Research saved (I'll use it in the next build):\n\n{findings[:3000]}"
       + (f"\n\n🔗 {url}" if url else ""))


FIND_SYS = (
    "Answer the user's question using ONLY the excerpts below, pulled from their own "
    "project's files via semantic search. Cite which file(s) you drew from. If the "
    "excerpts don't actually answer the question, say so plainly rather than guessing."
)


def act_find(arg: str) -> None:
    name = active_project()
    ensure_git(name)
    d = project_dir(name)
    query = arg.strip()
    if not query:
        tg("What should I look for in the project's own files?")
        return
    tg(f"📚 Checking the search index for '{name}'…")
    result = corpus_index.index_project(name, d)
    if result["restored"]:
        tg("♻️ Restored the search index from its Forgejo backup.")
    if result["changed"] or result["removed"]:
        tg(f"🗂️ Indexed {result['changed']} changed file(s)"
           + (f", removed {result['removed']}" if result["removed"] else "")
           + f" ({result['chunks']} new chunks).")
    hits = corpus_index.search(d, query, top_k=10)
    if not hits:
        tg(f"📚 Nothing indexed yet (or nothing matched) for '{query}'.")
        return
    context = "\n\n".join(f"--- {h['path']} (part {h['chunk_index']}) ---\n{h['content']}" for h in hits)[:24000]
    answer = ollama_chat(FIND_SYS, f"Question: {query}\n\nExcerpts:\n\n{context}")
    sources = ", ".join(sorted({h["path"] for h in hits}))
    tg(f"📚 {answer[:3500]}\n\n📎 Sources: {sources}")


def act_fix(arg: str) -> None:
    name = active_project()
    ensure_git(name)
    d = project_dir(name)
    desc = arg.strip()
    refs = find_referenced_paths(d, desc) if desc else []
    if refs:
        tg("📎 Reading: " + ", ".join(str(p.relative_to(d)) for p in refs))
    tg(f"🔎 Diagnosing '{name}'…")
    before = capture_diagnostics(name)
    parts = ["There is a bug in this existing project. Diagnose the root cause and fix "
             "it with minimal, targeted changes — don't rewrite working code."]
    if desc:
        parts.append(f"Reported symptom: {desc}")
    if before:
        parts.append("Current test output showing the failure:\n" + before)
        (project_dir(name) / "DIAGNOSTICS.md").write_text(
            "# Latest test output\n\n```\n" + before + "\n```\n", encoding="utf-8")
    else:
        parts.append("No test suite found — inspect the code, find the defect, and correct it.")
    _, warning = guardrail.check(desc or "fix a bug")
    if warning:
        tg(f"⚠️ Guardrail note: {warning}")
    append_note(name, f"FIX: {desc[:120] or '(diagnose + repair)'}")
    aider_edit(name, "\n\n".join(parts), extra_reads=refs)
    git(name, "add", "-A")
    if git(name, "diff", "--cached", "--quiet").returncode != 0:
        git(name, "commit", "-m", f"fix: {desc[:60] or 'diagnose and repair'}")
    after = run_tests(name)
    if not after:
        verdict = "ℹ️ no tests to verify the fix"
    elif "failed" in after.lower() or "error" in after.lower():
        verdict = "⚠️ tests still failing — may need another pass"
    else:
        verdict = "✅ tests pass now"
    url = push(name)
    tg(f"🔧 Fixed '{name}':\n{summarize_change(name)}\n{verdict}" + (f"\n\n🔗 {url}" if url else ""))


def act_ship(arg: str) -> None:
    tg("📦 Packaging…")
    ship(active_project())


NO_CODE_RETRY = (
    "You replied WITHOUT creating or changing any file. Do NOT explain, describe, or "
    "discuss — output the ACTUAL FILE(S) NOW, each with its full path and complete "
    "contents, so they get written to the project. If no filename was given, pick a "
    "sensible one for the language. Code only, no commentary."
)


def act_build(arg: str) -> None:
    name = active_project()
    ensure_git(name)
    d = project_dir(name)
    task = arg.strip() or "continue with the plan"
    _, warning = guardrail.check(task)
    if warning:
        tg(f"⚠️ Guardrail note: {warning}")
    
    tools = ["nmap", "sqlmap", "nikto", "gobuster", "ffuf", "hydra", "enum4linux", "nuclei"]
    tool_help = ""
    for tool in tools:
        if tool in task:
            try:
                help_cmd = f"{tool} --help"
                r = subprocess.run(
                    ["docker", "exec", "kali", "bash", "-lc", help_cmd],
                    capture_output=True, text=True, timeout=20
                )
                tool_help += f"\n{r.stdout.strip()}\n"
            except Exception as e:  # noqa: BLE001
                tg(f"⚠️ Could not get help for {tool}: {e}")
    
    task = tool_help + task if tool_help else task
    
    refs = find_referenced_paths(d, task)
    if refs:
        tg("📎 Reading: " + ", ".join(str(p.relative_to(d)) for p in refs))
    tg(f"🛠️ Working on '{name}'…")
    do_research(name, task)  # honors inline 'search:' lines → writes RESEARCH.md
    append_note(name, f"BUILD: {task[:120]}")
    # Bookkeeping files that change on every build regardless of whether the model
    # wrote real code — excluded when deciding "did aider actually produce code?".
    _META = {"PROJECT_NOTES.md", "RESEARCH.md", ".gitignore", ".aiderignore",
             ".conversation.json", ".plan_chat.json"}

    def _real(files: list) -> bool:
        return any(f.strip() and f.strip() not in _META for f in files)

    # aider runs with --auto-commits, so it commits the code ITSELF. Checking only
    # 'staged but uncommitted' misses aider's own commit and falsely reports "no code"
    # (the urltest2 false-negative). So snapshot HEAD before, and after the whole build
    # diff the entire range — that catches aider's commits AND ours.
    def _changed_since(base: str) -> list:
        """Files touched between `base` and the current HEAD — catches aider's own
        auto-commits AND ours (checking only staged changes misses aider's)."""
        head = git(name, "rev-parse", "HEAD").stdout.strip()
        if base and head and base != head:
            return git(name, "diff", "--name-only", base, head).stdout.splitlines()
        if head and not base:  # brand-new repo: everything in the first commit
            return git(name, "show", "--name-only", "--format=", head).stdout.splitlines()
        return []  # no new commit at all → nothing was produced

    def _commit(msg: str) -> None:
        git(name, "add", "-A")
        if git(name, "diff", "--cached", "--quiet").returncode != 0:
            git(name, "commit", "-m", msg)

    before = git(name, "rev-parse", "HEAD").stdout.strip()
    aider_edit(name, task, extra_reads=refs)
    _commit(f"edit: {task[:60]}")

    # The classic 'prose reply, no file written' failure — the model EXPLAINED the
    # code instead of creating it (the vulnscan/no-code case). One forceful retry
    # demanding actual files usually turns that prose into a real file, which beats
    # giving up and making the owner rephrase.
    if not _real(_changed_since(before)):
        tg("🟡 Got a description but no file — insisting on real code…")
        aider_edit(name, NO_CODE_RETRY + "\n\n[The task]\n" + task, extra_reads=refs)
        _commit(f"edit (retry): {task[:50]}")

    # Auto write→run→fix: verify it actually runs, repair up to AUTO_FIX_ROUNDS times.
    for rnd in range(1, AUTO_FIX_ROUNDS + 1):
        diag = capture_diagnostics(name)
        if not looks_broken(diag):
            break
        tg(f"🔁 Didn't run cleanly — auto-fixing (round {rnd})…")
        aider_edit(name, "The project has an error. Fix it with minimal changes.\n\n" + diag)
        _commit(f"autofix {rnd}")

    produced_code = _real(_changed_since(before))
    url = push(name)
    final = capture_diagnostics(name)
    verdict = "\n⚠️ still failing — try 'fix'" if looks_broken(final) else ("\n✅ runs clean" if final else "")
    clear_chat(name)  # discussion has been actioned; a future 'plan:' starts fresh
    if produced_code:
        tg(f"✅ Updated '{name}':\n{summarize_change(name)}{verdict}" + (f"\n\n🔗 review: {url}" if url else ""))
    else:
        tg(f"🟡 No code produced for '{name}'. The model didn't write or change any project "
           "files (only notes). Try again, more specific, or use 'fix:'.")


# ── Tools the agent can run: install deps, run code, lint ────────────────────
RUN_TIMEOUT = 90
_DANGEROUS = re.compile(r"\brm\s+-rf\s+/|\bmkfs\b|\bdd\s+if=|:\(\)\s*\{|>\s*/dev/sd|\bshutdown\b|\breboot\b", re.I)


def detect_entry(d: Path):
    """Best guess at how to run the project."""
    for cand in ("main.py", "app.py", "run.py", "cli.py", "__main__.py"):
        if (d / cand).exists():
            return ["python", cand]
    if (d / "package.json").exists():
        return ["npm", "start"]
    pys = [p for p in d.glob("*.py") if p.name not in ("setup.py", "conftest.py")]
    if len(pys) == 1:
        return ["python", pys[0].name]
    return None


def kali_ready() -> bool:
    """Check if the Kali container is running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "kali"],
            capture_output=True, text=True
        )
        return result.stdout.strip().lower() == "true"
    except Exception:  # noqa: BLE001
        return False

def run_command(d: Path, cmd: list) -> tuple:
    """Run a command in the project dir with a timeout; return (status, output tail)."""
    try:
        if kali_ready():
            full_cmd = f"cd {shlex.quote(str(d))} && {shlex.join(cmd)}"
            r = subprocess.run(
                ["docker", "exec", "kali", "bash", "-lc", full_cmd],
                capture_output=True, text=True, timeout=RUN_TIMEOUT
            )
        else:
            r = subprocess.run(cmd, cwd=str(d), capture_output=True, text=True, timeout=RUN_TIMEOUT)
        return f"exited {r.returncode}", (r.stdout + "\n" + r.stderr).strip()[-3000:]
    except subprocess.TimeoutExpired as e:
        tail = ""
        for s in (e.stdout, e.stderr):
            if s:
                tail += s.decode() if isinstance(s, bytes) else s
        return (f"still running after {RUN_TIMEOUT}s (killed — likely a long-running server)",
                tail.strip()[-3000:])
    except Exception as e:  # noqa: BLE001
        return "failed to start", str(e)


def act_install(arg: str) -> None:
    name = active_project(); ensure_git(name); d = project_dir(name)
    pkgs = arg.strip()
    if pkgs:
        cmd, what = ["pip", "install", *pkgs.split()], pkgs
    elif (d / "requirements.txt").exists():
        cmd, what = ["pip", "install", "-r", "requirements.txt"], "requirements.txt"
    elif (d / "package.json").exists():
        cmd, what = ["npm", "install"], "package.json deps"
    else:
        tg("Nothing to install — name the packages, or add a requirements.txt / package.json.")
        return
    tg(f"📦 Installing {what}…")
    try:
        if kali_ready():
            full_cmd = f"cd {shlex.quote(str(d))} && {shlex.join(cmd)}"
            r = subprocess.run(
                ["docker", "exec", "kali", "bash", "-lc", full_cmd],
                capture_output=True, text=True, timeout=600
            )
        else:
            r = subprocess.run(cmd, cwd=str(d), capture_output=True, text=True, timeout=600)
        out = (r.stdout + r.stderr).strip()[-1500:]
        tg(f"{'✅' if r.returncode == 0 else '⚠️'} install {what}:\n{out or '(done)'}")
    except subprocess.TimeoutExpired:
        tg(f"⏱️ install {what} timed out.")


def act_run(arg: str) -> None:
    name = active_project(); ensure_git(name); d = project_dir(name)
    if arg.strip():
        if _DANGEROUS.search(arg):
            tg("⛔ That command looks destructive — refusing to run it.")
            return
        cmd, label = arg.strip().split(), arg.strip()
    else:
        cmd = detect_entry(d)
        if not cmd:
            tg("No obvious entry point (main.py / app.py / …). Tell me the command, e.g. 'run: python foo.py'.")
            return
        label = " ".join(cmd)
    tg(f"▶️ Running: {label}")
    status, out = run_command(d, cmd)
    tg(f"▶️ {status}:\n{out or '(no output)'}")


def act_lint(arg: str) -> None:
    name = active_project(); ensure_git(name); d = project_dir(name)
    try:
        have_ruff = subprocess.run(["ruff", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        have_ruff = False
    if not have_ruff:
        subprocess.run(["pip", "install", "--quiet", "ruff"], timeout=300)
    do_fix = "no fix" not in arg.lower() and "check only" not in arg.lower()
    tg("🧹 Linting" + (" (auto-fixing)" if do_fix else "") + "…")
    cmd = ["ruff", "check", "."] + (["--fix"] if do_fix else [])
    r = subprocess.run(cmd, cwd=str(d), capture_output=True, text=True, timeout=180)
    out = (r.stdout + r.stderr).strip()[-2500:]
    if do_fix:
        git(name, "add", "-A")
        if git(name, "diff", "--cached", "--quiet").returncode != 0:
            git(name, "commit", "-m", "lint: ruff --fix"); push(name)
    tg(f"🧹 Lint:\n{out or 'clean ✅'}")


def capture_diagnostics(name: str) -> str:
    """For the fixer: prefer test output; else run the entry point to catch runtime errors."""
    out = run_tests(name)
    if out:
        return out
    entry = detect_entry(project_dir(name))
    if entry:
        status, ro = run_command(project_dir(name), entry)
        if ro:
            return f"$ {' '.join(entry)}  ({status})\n{ro}"
    return ""


def looks_broken(diag: str) -> bool:
    """True if diagnostics show a real failure (not just a server that kept running)."""
    if not diag:
        return False
    low = diag.lower()
    if "still running after" in low:  # a server that started fine and didn't exit
        return False
    return any(k in low for k in ("traceback", "error", "exception", "failed", " failed", "assert"))


# ── Chat = the agentic shell: talk to the bot, and let the talk DO things ─────
# Plain (un-prefixed) messages land here. The model can either reply OR pick a
# tool to run — and because it has the whole conversation in context, "build
# that" / "now run it" resolve against what you were just discussing instead of
# forcing you to restate it. Explicit prefixes (build:/fix:/…) still bypass all
# of this via parse_explicit().

# Tools the chat agent may invoke on its own — a curated subset of DISPATCH.
# ('chat' isn't here: replying IS the non-tool branch.)
AGENT_TOOLS = ["plan", "build", "fix", "run", "install", "lint",
               "research", "find", "ship", "new", "use", "status"]

# Loose synonyms the model tends to emit, mapped onto real tools (mirrors the
# alias table in parse_explicit so prefix- and chat-routing stay consistent).
# ('create'/'make' deliberately absent — too ambiguous between build-functionality
# and new-project; the prompt distinguishes build vs new explicitly instead.)
_TOOL_ALIASES = {"edit": "build", "go": "build",
                 "diagnose": "fix", "debug": "fix", "repair": "fix",
                 "search": "research", "deps": "install", "switch": "use",
                 "continue": "use", "open": "use"}

AGENT_SYS = (
    "You are the owner's assistant inside their self-hosted code builder, talking over "
    "Telegram. You can just TALK, or RUN A TOOL on the active project.\n\n"
    "TO RUN A TOOL: make the FIRST line of your reply begin with '@' and the tool name, "
    "then the details on the same line. Nothing before it. For example:\n"
    "    @build a Flask todo app with a SQLite backend\n"
    "    @run\n"
    "    @fix the login handler crashes on an empty password\n"
    "Use a tool ONLY when the owner clearly wants you to act.\n\n"
    "TO TALK: just reply normally, with NO '@' line — for questions, explanations, "
    "opinions, or small talk, plain prose IS the right answer. Don't wrap it in anything, "
    "and don't force a tool when they're only chatting.\n\n"
    "You have the whole conversation above, so when they say 'build that', 'make it', 'go "
    "ahead', 'now run it', 'fix that', 'ship it' — they mean what you were just discussing. "
    "Don't make them restate it: after @build/@fix/@plan, name the thing and its key details "
    "in a sentence or two — you don't need to transcribe everything, because the whole "
    "conversation is also handed to the tool as background.\n\n"
    "Tools:\n"
    "- @plan <idea>       design/think first, write a plan, no code\n"
    "- @build <what>      create or add functionality (also for edits / 'make it' / 'go')\n"
    "- @fix <symptom>     repair broken or failing existing code\n"
    "- @run [command]     execute the project or a shell command, show output (empty = auto-detect)\n"
    "- @install [pkgs]    install dependencies (empty = requirements.txt / package.json)\n"
    "- @lint              run the linter and auto-fix\n"
    "- @research <topic>  look something up on the WEB\n"
    "- @find <query>      semantic search over the project's OWN files/docs (not the web)\n"
    "- @ship              package and deliver the active project\n"
    "- @new <name>        start a NEW project\n"
    "- @use <name>        switch to an EXISTING project\n"
    "- @status            report the current project + state\n\n"
    "If you're unsure whether they want you to act yet, just talk and ask."
)


def _project_files(d: Path, max_files: int = 40) -> list:
    """Real project files (code/docs), skipping VCS/agent/internal files + dotfiles."""
    out = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(d)
        if any(part in _SKIP_DIRS for part in rel.parts) or p.name in _SKIP_NAMES:
            continue
        if p.name.startswith("."):
            continue
        out.append(p)
        if len(out) >= max_files:
            break
    return out


FORGEJO_EXCLUDE = {"cloud-code"}  # the agent's own source repo — not a user project


def forgejo_project_names() -> list | None:
    """Project names from Forgejo (the source of truth). Returns None if Forgejo is
    unreachable so the caller can fall back to local dirs. Excludes the bot's own repo."""
    if not (FORGEJO_URL and FORGEJO_USER and FORGEJO_TOKEN):
        return None
    try:
        r = requests.get(f"{FORGEJO_URL}/api/v1/users/{FORGEJO_USER}/repos",
                         params={"limit": 100}, auth=(FORGEJO_USER, FORGEJO_TOKEN), timeout=10)
        r.raise_for_status()
        return [repo["name"] for repo in r.json() if repo.get("name") not in FORGEJO_EXCLUDE]
    except Exception:  # noqa: BLE001
        return None


def _blurb(d: Path) -> str:
    """First meaningful line of a project's README/PROJECT_NOTES, as a one-line summary."""
    for fn in ("README.md", "PROJECT_NOTES.md"):
        p = d / fn
        if p.exists():
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                ln = ln.strip().lstrip("#").strip()
                if ln:
                    return " — " + ln[:120]
    return ""


def project_context() -> str:
    """Context for the chat. The project LIST comes from FORGEJO (source of truth), so
    it matches what really exists — repos you deleted drop off, repos you made directly
    in Forgejo show up. For each: the file tree from the local clone (if present); plus
    the full contents of the ACTIVE project so the chat can read the real code."""
    active = ACTIVE_FILE.read_text(encoding="utf-8").strip() if ACTIVE_FILE.exists() else ""
    names = forgejo_project_names()
    note = ""
    if names is None:  # Forgejo down → fall back to local dirs, and say so.
        names = [d.name for d in PROJECTS.glob("*/") if d.is_dir()]
        note = "\n(⚠️ couldn't reach Forgejo — this list is from local disk and may be stale)"
    if active and active not in names and (PROJECTS / active).is_dir():
        names.append(active)  # always show the active project even if Forgejo-less
    sections = []
    for name in sorted(set(names)):
        d = PROJECTS / name
        mark = " (ACTIVE)" if name == active else ""
        if d.is_dir():
            files = _project_files(d)
            tree = "\n".join(f"    {p.relative_to(d)}" for p in files) or "    (no code files yet)"
            sections.append(f"• {name}{mark}{_blurb(d)}\n  files:\n{tree}")
        else:
            sections.append(f"• {name}{mark} — in Forgejo, not synced to this builder yet "
                            f"(say 'use: {name}' to pull it in)")
    body = ("\n".join(sections) or "(no projects yet)") + note
    if active and (PROJECTS / active).is_dir():
        contents = read_bounded(PROJECTS / active, _project_files(PROJECTS / active),
                                per_file_cap=4000, total_cap=16000)
        if contents:
            body += f"\n\n[Full contents of the active project '{active}']\n{contents}"
    return body


# ── Conversation memory: the chat remembers across messages ──────────────────
def conv_path(name: str) -> Path:
    return project_dir(name) / ".conversation.json"


def load_conv(name: str) -> list:
    p = conv_path(name)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_conv(name: str, history: list) -> None:
    # Cap at the last 40 messages so context + the file stay bounded.
    # mkdir the project dir first: chat/talk don't call ensure_git, so on a
    # project whose folder doesn't exist yet (e.g. active project left in a
    # half-set-up state) writing here would otherwise crash with ENOENT.
    p = conv_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(history[-40:], indent=2), encoding="utf-8")


def clear_conv(name: str) -> None:
    p = conv_path(name)
    if p.exists():
        p.unlink()


def recent_context(history: list, max_chars: int = 6000, max_msgs: int = 8) -> str:
    """The tail of the conversation rendered as plain text, so a tool triggered
    mid-chat ('build that') can be handed what 'that' actually refers to."""
    lines = []
    for m in history[-max_msgs:]:
        who = "You" if m.get("role") == "assistant" else "Owner"
        content = str(m.get("content", "")).strip()
        if content:
            lines.append(f"{who}: {content}")
    return "\n\n".join(lines)[-max_chars:]


# An action is a line that starts with @<tool>; everything from there to the end
# of the message is the arg (so multi-line specs work). We scan the whole reply,
# not just the first line, so a stray preamble ("Sure!\n@build …") still fires —
# but only a KNOWN tool name counts, so '@property is a decorator' stays chat.
_ACTION_RE = re.compile(r"(?m)^[ \t]*@([A-Za-z]+)[ \t]*")


def parse_agent(raw: str) -> tuple:
    """Turn the model's reply into a decision. Returns ('tool', name, arg) if the
    reply contains an '@<tool>' directive line for a real tool, else ('reply', text).

    This is the whole fix for the old 'it answered in prose instead of acting'
    problem: prose is no longer a PARSE FAILURE that we shrug at — prose is the
    intended shape of a reply. Only an explicit '@build'/'@run'/… makes it act, so
    there's no ambiguous middle ground to get wrong in either direction."""
    for m in _ACTION_RE.finditer(raw):
        name = _TOOL_ALIASES.get(m.group(1).lower(), m.group(1).lower())
        if name in AGENT_TOOLS:
            arg = raw[m.end():].strip()   # rest of this line + any following lines
            return ("tool", name, arg)
    return ("reply", raw.strip())


def act_chat(arg: str) -> None:
    """The agentic shell: talk to the bot, and let the talk run tools.

    A plain (un-prefixed) message is a memory-backed conversation. Each turn the
    model either REPLIES or CALLS A TOOL (build/fix/run/plan/…). Because it has the
    whole conversation in context, 'build that' / 'now run it' / 'ship it' work — it
    pulls the spec from what you were just discussing instead of making you restate
    it. For build/fix/plan the recent conversation is also threaded in as background
    so even a terse arg resolves to the right thing. Explicit prefixes (build:/fix:/…)
    bypass all of this via parse_explicit()."""
    q = arg.strip()
    name = active_project()
    if q.lower() in ("reset", "/reset", "new chat", "clear chat", "forget"):
        clear_conv(name)
        tg("🧹 Fresh conversation — what's up?")
        return
    if not q:
        tg("💬 Just talk to me — describe what you want, ask about a project, or say "
           "'build that' once we've talked something through.")
        return

    system = AGENT_SYS + "\n\n[Your projects]\n" + project_context()
    history = load_conv(name)
    history.append({"role": "user", "content": q})

    raw = ollama_multiturn(system, history)
    decision = parse_agent(raw)

    # ── Plain reply: just answer, remember it, done. ──
    if decision[0] == "reply":
        answer = decision[1] or "(no response)"
        history.append({"role": "assistant", "content": answer})
        save_conv(name, history)
        tg(answer)
        return

    # ── Tool call: carry the conversation in as context so 'that' resolves. ──
    _, tool, tool_arg = decision
    if tool in ("build", "fix", "plan"):
        ctx = recent_context(history[:-1])  # everything up to (not incl) this ask
        if ctx:
            tool_arg = (f"[Background — our conversation leading up to this request]\n{ctx}"
                        f"\n\n[Task]\n{tool_arg or 'do what we just discussed above'}")
    # Record the action in conversation memory BEFORE running it, so a follow-up
    # ('now run it', 'ship it') has the thread even if the tool itself errors.
    history.append({"role": "assistant", "content": f"(running @{tool}: {tool_arg[:200]})"})
    save_conv(name, history)
    DISPATCH[tool](tool_arg)


# ── Pure talk-only channel (chat:/ask:) — never pulls a tool ──────────────────
TALK_SYS = (
    "You are the owner's assistant inside their self-hosted code builder, chatting over "
    "Telegram. This is a TALK-ONLY channel: you do NOT build, run, edit, fix, or change "
    "anything — you just have a conversation. Answer concisely, chat-length. You're given a "
    "summary of their projects below; use it for questions about a project, and answer "
    "helpfully for anything else. If they clearly want you to actually build/fix/run "
    "something, tell them to send it as a plain message or use a command prefix "
    "(build: / fix: / run: / new: / use:) — do NOT attempt to do it here."
)


def act_talk(arg: str) -> None:
    """PURE talk-only channel (the chat:/ask: prefixes). A memory-backed conversation
    that shares the SAME history as the agentic shell (act_chat) — so you can talk
    something through here and then send a plain 'build that' to act on it — but this
    path NEVER pulls a tool. Nothing gets built, run, or changed from here."""
    q = arg.strip()
    name = active_project()
    if q.lower() in ("reset", "/reset", "new chat", "clear chat", "forget"):
        clear_conv(name)
        tg("🧹 Fresh conversation — what's up?")
        return
    if not q:
        tg("💬 Talk-only mode — ask me anything. (Send a plain message, or use "
           "build:/fix:/run:, when you want me to actually do it.)")
        return
    system = TALK_SYS + "\n\n[Your projects]\n" + project_context()
    history = load_conv(name)
    history.append({"role": "user", "content": q})
    answer = ollama_multiturn(system, history)
    history.append({"role": "assistant", "content": answer})
    save_conv(name, history)
    tg(answer or "(no response)")


DISPATCH = {
    "status": act_status, "new": act_new, "use": act_use, "plan": act_plan,
    "research": act_research, "fix": act_fix, "ship": act_ship, "build": act_build,
    "run": act_run, "install": act_install, "lint": act_lint, "find": act_find,
    # chat:/ask: → the guaranteed talk-only channel. The AGENTIC shell (act_chat)
    # is reached only via a plain, un-prefixed message (see handle()).
    "chat": act_talk, "ask": act_talk,
}


# ── Routing: explicit prefix (fast, exact) OR natural language (model router) ─
def parse_explicit(command: str):
    """Deterministic parse of the classic command syntax. Returns (action, arg) or None."""
    low = command.lower().strip()
    if low in ("status", "/status"):
        return ("status", "")
    if low in ("ship", "ship it", "/ship"):
        return ("ship", "")
    if low in ("go", "/go", "build"):
        return ("build", "")
    if low in ("run", "/run"):
        return ("run", "")
    if low in ("lint", "/lint"):
        return ("lint", "")
    m = re.match(
        r"^(new|use|switch|continue|plan|fix|diagnose|debug|build|edit|go|research|search|find|run|install|deps|lint|chat|ask)\s*:\s*(.*)$",
        command, re.I | re.S)
    if m:
        kw, arg = m.group(1).lower(), m.group(2).strip()
        action = {"switch": "use", "continue": "use", "diagnose": "fix", "debug": "fix",
                  "edit": "build", "go": "build", "search": "research", "deps": "install",
                  "ask": "chat"}.get(kw, kw)
        return (action, arg)
    return None


def handle(command: str) -> None:
    command = command.strip()
    if not command:
        return
    parsed = parse_explicit(command)
    if parsed:
        # Explicit command prefix (build:/fix:/run:/chat:/…) always wins — exact and
        # deterministic. 'chat:'/'ask:' resolve to act_talk, the pure talk-ONLY channel.
        action, arg = parsed
        DISPATCH.get(action, act_talk)(arg)
    elif chat_active(active_project()):
        # An open 'plan:' conversation keeps continuing on plain messages
        # until 'go'/'build' runs (see act_build).
        act_plan(command)
    else:
        # DEFAULT: the agentic shell (act_chat). A plain, un-prefixed message is a
        # memory-backed conversation that can EITHER reply OR pull a tool itself
        # (build/run/fix/…) via the '@tool' contract — so 'build that' acts on what was
        # just discussed. Want pure conversation with no chance of action? Use chat:/ask:.
        act_chat(command)


# ── Session loop ────────────────────────────────────────────────────────────
def main() -> None:
    zone = os.environ.get("SCW_DEFAULT_ZONE", "")
    iid = instance_id()
    PROJECTS.mkdir(parents=True, exist_ok=True)

    first = ud("build-task")
    if first:
        clear_command(iid, zone)
        try:
            handle(first)
        except Exception as e:  # noqa: BLE001
            tg(f"❌ Error: {e}")

    # Stay warm: poll for the next command until IDLE_TIMEOUT of silence.
    idle_start = time.time()
    while time.time() - idle_start < IDLE_TIMEOUT:
        time.sleep(8)
        cmd = ud("build-task")
        if cmd:
            clear_command(iid, zone)
            idle_start = time.time()
            try:
                handle(cmd)
            except Exception as e:  # noqa: BLE001
                tg(f"❌ Error: {e}")
    tg("💤 Idle — powering the builder off (your work is saved; just message me to resume).")


if __name__ == "__main__":
    main()
