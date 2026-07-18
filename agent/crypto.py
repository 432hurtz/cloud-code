#!/usr/bin/env python3
"""End-to-end encryption for the build pipeline, using `age`.

Deliverables are encrypted to YOUR public key (RECIPIENT_AGE_PUBKEY) before they
leave the VM, so Gmail, Telegram, and GCP only ever hold ciphertext — only your
local private key can decrypt. Your private key never touches the cloud.

Inbound tasks may optionally be age-encrypted to the VM's own key (generated on
the model disk at setup); if so they're decrypted here before anything else sees
the plaintext.
"""
import os
import subprocess
from pathlib import Path

RECIPIENT = os.environ.get("RECIPIENT_AGE_PUBKEY", "").strip()
VM_IDENTITY = os.environ.get("VM_AGE_IDENTITY_FILE", "/mnt/models/vm_age_key.txt")
ARMOR_MARKER = "-----BEGIN AGE ENCRYPTED FILE-----"


def encrypt_file(path: Path) -> Path:
    """Encrypt `path` to RECIPIENT. Returns the new .age path, or the original
    path unchanged if no recipient key is configured (encryption is opt-in)."""
    if not RECIPIENT:
        return path
    out = path.with_name(path.name + ".age")
    subprocess.run(
        ["age", "--recipient", RECIPIENT, "--output", str(out), str(path)],
        check=True,
    )
    return out


def output_is_encrypted() -> bool:
    return bool(RECIPIENT)


def maybe_decrypt_task(task: str) -> str:
    """If the task is an armored age blob and the VM has an identity key,
    decrypt it. Otherwise return the task unchanged."""
    if ARMOR_MARKER not in task or not Path(VM_IDENTITY).exists():
        return task
    res = subprocess.run(
        ["age", "--decrypt", "--identity", VM_IDENTITY],
        input=task.encode(),
        capture_output=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"failed to decrypt inbound task: {res.stderr.decode()}")
    return res.stdout.decode().strip()
