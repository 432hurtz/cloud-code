#!/usr/bin/env python3
"""Owner-controlled topic-safety guardrail.

Two layers, both configured from env (set in .env, see GUARDRAIL_*):
  * keyword  — refuse any task containing a blocked term (fast, no model needed;
               the trigger also runs this BEFORE booting the GPU).
  * strict   — additionally ask the local model to judge the task against your
               plain-English GUARDRAIL_POLICY and refuse violations.

You set the policy. This is a topic/scope control you tune, not a fixed filter.
"""
import json
import os

import requests

MODE = os.environ.get("GUARDRAIL_MODE", "keyword").lower()
POLICY = os.environ.get("GUARDRAIL_POLICY", "")
BLOCK = [t.strip().lower() for t in os.environ.get("GUARDRAIL_BLOCK", "").split(",") if t.strip()]
MODEL = os.environ.get("MODEL", "")
OLLAMA = "http://localhost:11434/api/chat"


def keyword_check(task: str) -> tuple[bool, str]:
    low = task.lower()
    for term in BLOCK:
        if term in low:
            return False, f"matched blocked term '{term}'"
    return True, ""


def policy_check(task: str) -> tuple[bool, str]:
    """Ask the local model to judge the task against POLICY. Fail-closed on a
    clear BLOCK; allow if the judge is unreachable (keyword layer still applies)."""
    if not POLICY:
        return True, ""
    system = (
        "You are a strict policy gate. Given a POLICY and a TASK, decide if the "
        "task is allowed. Respond ONLY with compact JSON: "
        '{"decision":"ALLOW"|"BLOCK","reason":"<short>"}.'
    )
    user = f"POLICY:\n{POLICY}\n\nTASK:\n{task}"
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
        content = resp.json()["message"]["content"]
        verdict = json.loads(content)
    except Exception as e:  # noqa: BLE001
        print(f"[guardrail] policy check unavailable, allowing: {e}")
        return True, ""
    if str(verdict.get("decision", "")).upper() == "BLOCK":
        return False, verdict.get("reason", "policy violation")
    return True, ""


def check(task: str) -> tuple[bool, str]:
    if MODE == "off":
        return True, ""
    ok, reason = keyword_check(task)
    if not ok:
        return False, reason
    if MODE == "strict":
        return policy_check(task)
    return True, ""


if __name__ == "__main__":
    import sys

    allowed, why = check(" ".join(sys.argv[1:]))
    print("ALLOW" if allowed else f"BLOCK: {why}")
