#!/usr/bin/env python3
"""
init_vault.py — scaffold a fresh agent-memory vault from vault-template/.

Copies the template into $AGENT_MEMORY_VAULT (default ~/agent-memory-vault),
creating the directory structure and seed files. Never overwrites an existing
vault unless --force is given.

Usage:
  export AGENT_MEMORY_VAULT="$HOME/my-vault"
  python3 scripts/init_vault.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "vault-template"

# Empty content dirs that need a .gitkeep / to exist in the vault
CONTENT_DIRS = [
    "00_Inbox", "03_Projects", "04_Knowledge", "05_Daily",
    "06_Decisions", "07_Playbooks", "08_Sources", "09_Archive", "10_Resume",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scaffold a fresh agent-memory vault.")
    ap.add_argument("--vault", default=os.environ.get(
        "AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))
    ap.add_argument("--force", action="store_true",
                    help="Write into an existing vault (won't delete, only fills gaps)")
    args = ap.parse_args(argv)

    vault = Path(args.vault).expanduser()
    if not TEMPLATE.is_dir():
        print(f"[fatal] template missing: {TEMPLATE}", file=sys.stderr)
        return 2
    if vault.exists() and any(vault.iterdir()) and not args.force:
        print(f"[abort] vault already exists and is non-empty: {vault}\n"
              f"        re-run with --force to fill in missing files.", file=sys.stderr)
        return 1

    vault.mkdir(parents=True, exist_ok=True)

    # 1) copy template files (don't clobber existing unless they're identical-by-absence)
    for src in TEMPLATE.rglob("*"):
        rel = src.relative_to(TEMPLATE)
        dst = vault / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            if dst.exists() and not args.force:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # 2) ensure the numbered content dirs exist
    for d in CONTENT_DIRS:
        (vault / d).mkdir(parents=True, exist_ok=True)

    # 3) system runtime dirs
    for d in ("_system/logs", "_system/state", "_system/session-beats"):
        (vault / d).mkdir(parents=True, exist_ok=True)

    print(f"[ok] vault scaffolded at: {vault}")
    print(f"     set AGENT_MEMORY_VAULT={vault} in your shell profile.")
    print(f"     next: python3 scripts/agent_memory_boot.py --agent claude-code --task 'hello'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
