#!/usr/bin/env python3
"""Overwrite the shared hot cache at _system/hot.md.

The hot cache is a ~500-word snapshot of "where are we right now". Every agent
reads it FIRST on boot. Each session that produces meaningful state changes
MUST overwrite this file at the end.

Usage:
  update_hot.py --agent <agent-name> \
      --last "<one-line summary of last session>" \
      --active "<active task>" [--active "..."] \
      --pending "<unresolved>" [--pending "..."] \
      --changes "<recent change>" [--changes "..."] \
      --dont "<thing to avoid>" [--dont "..."] \
      [--orchestrator <agent>] \
      [--routing <preset>]

The script enforces:
- Overwrite (not append).
- Word count <= 600 (warn but don't fail at 500-600 buffer).
- frontmatter updated timestamp.
- Default orchestrator = none unless overridden.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import sys
import time
from pathlib import Path


VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))
HOT_PATH = VAULT / "_system" / "hot.md"

DEFAULT_ROUTING = """- Architecture / code quality / risk review / design → [[02_Agents/Claude Code|Claude Code]]
- Ops / code changes / config / scripts / execution → [[02_Agents/Codex|Codex]]
- Long-running tasks / digests / learning / vault lint → [[02_Agents/Hermes|Hermes]]
- Orchestration: none by default; the user dispatches or agents self-route by domain"""


def bullet_list(items: list[str], empty: str = "暂无") -> str:
    items = [s.strip() for s in items if s and s.strip()]
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {s}" for s in items)


def render(args: argparse.Namespace) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    routing = args.routing or DEFAULT_ROUTING
    return f"""---
type: hot-cache
updated: {now}
last_session_owner: {args.agent}
active_orchestrator: {args.orchestrator}
schema_version: 1
---

# Hot Cache

> 这是各 agent 上手时**第一个**要读的文件。整页不超过 500 字。
> 当前用户最新指令永远优先于本缓存。
> 写入约定：覆盖（不追加），由 `_system/scripts/update_hot.py` 维护。

## 最近一次会话

{now.split()[0]} by {args.agent}：{args.last}

## 当前活跃任务

{bullet_list(args.active or [])}

## 未结悬念

{bullet_list(args.pending or [])}

## 最近变更

{bullet_list(args.changes or [])}

## 不要做

{bullet_list(args.dont or [], empty="无特殊禁忌；常规：不写密钥/token/隐私/原始聊天")}

## 路由建议

{routing}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Overwrite hot.md with the latest session snapshot.")
    parser.add_argument("--agent", required=True, help="Agent owning this update (claude-code/codex/hermes or alias)")
    parser.add_argument("--last", required=True, help="One-line summary of the most recent session")
    parser.add_argument("--active", action="append", default=[], help="Active task (repeatable)")
    parser.add_argument("--pending", action="append", default=[], help="Unresolved item (repeatable)")
    parser.add_argument("--changes", action="append", default=[], help="Recent change (repeatable)")
    parser.add_argument("--dont", action="append", default=[], help="Thing to avoid (repeatable)")
    parser.add_argument("--orchestrator", default="none",
                        help="Active orchestrator codename (default: none — the user dispatches "
                             "or agents self-route by domain)")
    parser.add_argument("--routing", default="",
                        help="Override the default routing block; empty = default 4-agent routing")
    args = parser.parse_args()

    content = render(args)
    word_count = len(content.split())
    if word_count > 600:
        sys.stderr.write(
            f"warning: hot.md word count {word_count} exceeds soft limit 600 — "
            "consider tightening before next write.\n"
        )

    HOT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Exclusive lock on a sidecar lock file so concurrent agents can't trample
    # each other's overwrites. We hold the lock for the entire read-then-write
    # so the "last writer wins" rule operates on serialized snapshots.
    lock_path = HOT_PATH.with_suffix(".lock")
    deadline = time.monotonic() + 10  # 10s acquisition timeout
    with lock_path.open("a+", encoding="utf-8") as lock_f:
        while True:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    sys.stderr.write(
                        f"error: could not acquire {lock_path} within 10s; "
                        "another agent is updating hot cache. Retry shortly.\n"
                    )
                    return 2
                time.sleep(0.2)
        try:
            # Detect if hot.md changed while we were waiting and warn so the
            # caller can re-merge if their input is now stale.
            existing_mtime = HOT_PATH.stat().st_mtime if HOT_PATH.exists() else 0
            HOT_PATH.write_text(content, encoding="utf-8")
            note = ""
            if existing_mtime:
                staleness = time.time() - existing_mtime
                if staleness < 5:
                    note = (
                        f" (note: previous hot.md was written {staleness:.1f}s ago — "
                        "you may have just overwritten a peer agent's update)"
                    )
            print(f"wrote: {HOT_PATH}  ({word_count} words){note}")
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
