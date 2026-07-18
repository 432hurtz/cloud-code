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
    """Ask the local model to judge the task against POLICY (your rulebook).
    Fail-CLOSED: if the judge can't be reached after retries, refuse rather than
    risk letting a disallowed request through."""
    if not POLICY:
        return True, ""
    system = (
        "You are a strict admissions gate for an autonomous code builder. "
        "You are given a POLICY written by the owner and a TASK request. Decide, "
        "using ONLY the POLICY as your rulebook, whether the task is allowed. "
        "Judge the request's actual intent and purpose, not just its wording. "
        "If the task conflicts with the POLICY, or is ambiguous about whether it "
        "does, BLOCK it. Respond with ONLY compact JSON: "
        '{"decision":"ALLOW"|"BLOCK","reason":"<short reason citing the policy>"}.'
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
            if str(verdict.get("decision", "")).upper() == "BLOCK":
                return False, verdict.get("reason", "conflicts with your policy")
            return True, ""
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            print(f"[guardrail] policy check attempt {attempt + 1} failed: {e}")
    # Fail-closed: could not get a verdict → refuse.
    return False, f"guardrail check could not run (fail-closed): {last_err}"


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
