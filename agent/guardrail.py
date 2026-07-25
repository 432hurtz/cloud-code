#!/usr/bin/env python3
"""Owner-controlled topic-safety guardrail.

One layer, configured from env (set in .env, see GUARDRAIL_*): the local model
reads your plain-English GUARDRAIL_POLICY and can surface a short warning
about a task. It never blocks or refuses anything — you are the guardrail;
this just keeps you informed per your own policy. Set GUARDRAIL_MODE=off to
disable entirely.

You set the policy. This is an informational heads-up you tune, not a filter.
There is deliberately no separate keyword blocklist — a substring match warns
about things your policy wouldn't even flag (e.g. a message merely mentioning
"malware" while asking about detection), so all judgment lives in the policy
the model reads.
"""
import json
import os

import requests

MODE = os.environ.get("GUARDRAIL_MODE", "strict").lower()
POLICY = os.environ.get("GUARDRAIL_POLICY", "")
MODEL = os.environ.get("MODEL", "")
OLLAMA = "http://localhost:11434/api/chat"


def policy_check(task: str) -> tuple[bool, str]:
    """Ask the local model to note anything about the task worth flagging per
    POLICY (your rulebook). This NEVER blocks the task — you're the guardrail,
    the model only keeps you informed. Fail-open: if the judge can't be
    reached after retries (e.g. the GPU is busy loading the model), proceed
    with no warning rather than stall on an outage."""
    if not POLICY:
        return True, ""
    system = (
        "You are an informational assistant for an autonomous code builder "
        "owned and operated by one trusted person, who has final say over "
        "everything it builds. You are given a POLICY written by the owner "
        "and a TASK request. The task ALWAYS proceeds — you do not decide "
        "whether it's allowed, and you must never refuse, block, or lecture. "
        "Your only job is to note, briefly, anything about the task worth "
        "flagging per the POLICY (e.g. risk of damage to the owner's own "
        "machine or code, or a brief legal heads-up if the POLICY asks for "
        "one). If there's nothing worth flagging, return an empty warning. "
        'Respond with ONLY compact JSON: {"warning":"<short note, or empty '
        'string if nothing worth flagging>"}.'
    )
    user = f"POLICY (the owner's rulebook):\n{POLICY}\n\nTASK request:\n{task}"
    last_err = "unknown error"
    for attempt in range(2):
        try:
            resp = requests.post(
                OLLAMA,
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0},
                },
                timeout=120,
            )
            resp.raise_for_status()
            verdict = json.loads(resp.json()["message"]["content"])
            return True, str(verdict.get("warning", "") or "")
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            print(f"[guardrail] policy check attempt {attempt + 1} failed: {e}")
    # Fail-open: could not get a verdict → proceed with no warning, note why in the log.
    print(f"[guardrail] no verdict after retries, proceeding with no warning: {last_err}")
    return True, ""


def check(task: str) -> tuple[bool, str]:
    """Returns (allowed, warning). allowed is always True — kept in the return
    shape so callers just switch from aborting on it to displaying the
    warning alongside normal execution."""
    if MODE == "off" or not POLICY:
        return True, ""
    return policy_check(task)


if __name__ == "__main__":
    import sys

    _, warning = check(" ".join(sys.argv[1:]))
    print(f"⚠️ {warning}" if warning else "no warnings")
