#!/usr/bin/env python3
# Conservative Stop hook for Claude Code:
# 读 stdin JSON (transcript_path / session_id) → 抽信号 → 命中阈值才写
# _system/session-beats/<YYYY-MM-DD>/<session-id>.md (append, file-locked, redacted)
# 失败永远 exit 0，不阻塞主对话。
#
# stdin schema (Claude Code Stop hook):
#   {"transcript_path": "...", "session_id": "...", ...}

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))
BEATS_DIR = VAULT / "_system" / "session-beats"

MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_STDIN_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPT_LINES = 5000
USER_MSG_CAP = 200
BASH_CMD_CAP = 160
TOP_TOOLS = 10

# 敏感词脱敏：覆盖 OpenAI/Anthropic/GitHub/ReadGZH/通用 Bearer/x-api-key 等
REDACT_PATTERNS = [
    re.compile(r"sk_live_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9_\-\.=]{20,}"),
    re.compile(r"(?i)x-api-key:\s*[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)authorization:\s*[A-Za-z0-9_\-\.=\s]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+PRIVATE KEY-----"),
]

def log(msg: str) -> None:
    print(f"[stop-beat] {msg}", file=sys.stderr)

def redact(text: str) -> str:
    if not text:
        return text
    for pat in REDACT_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text

def parse_args() -> str:
    agent = "claude-code"
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--agent" and i + 1 < len(argv):
            agent = argv[i + 1]
    return agent

def read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read(MAX_STDIN_BYTES)
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"stdin parse failed: {e}")
        return {}

def extract_signals(transcript_path: str) -> dict | None:
    user_msgs: list[str] = []
    tool_uses: list[str] = []
    files_modified: set[str] = set()
    bash_cmds: list[str] = []

    p = Path(transcript_path)
    if not p.exists():
        log(f"transcript not found: {transcript_path}")
        return None

    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= MAX_TRANSCRIPT_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue

                role = (
                    entry.get("role")
                    or entry.get("type")
                    or (entry.get("message") or {}).get("role")
                )

                # user message
                if role == "user":
                    content = entry.get("content")
                    if content is None:
                        content = (entry.get("message") or {}).get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(
                            (c.get("text", "") if isinstance(c, dict) else "")
                            for c in content
                        )
                    text = text.strip()
                    # 跳过 system-reminder / tool_result 等假"用户消息"
                    if text and not text.startswith("<") and not text.startswith("[{"):
                        user_msgs.append(redact(text[:USER_MSG_CAP]))

                # tool_use blocks（assistant message 里的 content[]）
                msg = entry.get("message") or entry
                msg_content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(msg_content, list):
                    for block in msg_content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tname = block.get("name") or ""
                            if tname:
                                tool_uses.append(tname)
                            inp = block.get("input") or {}
                            if tname in MUTATING_TOOLS:
                                fp = inp.get("file_path")
                                if fp:
                                    files_modified.add(fp)
                            if tname == "Bash":
                                cmd = inp.get("command") or ""
                                if cmd:
                                    bash_cmds.append(redact(cmd[:BASH_CMD_CAP]))
    except Exception as e:
        log(f"transcript read error: {e}")
        return None

    return {
        "user_msgs": user_msgs,
        "tool_uses": tool_uses,
        "files_modified": sorted(files_modified),
        "bash_cmds": bash_cmds,
    }

def should_write(sig: dict) -> bool:
    if not sig:
        return False
    if sig["files_modified"]:
        return True
    if len(sig["user_msgs"]) >= 3:
        return True
    if len(sig["tool_uses"]) >= 10:
        return True
    return False

def render_beat(sig: dict, agent: str, session_id: str, transcript_path: str, now: dt.datetime) -> str:
    tc = Counter(sig["tool_uses"])
    tool_summary = ", ".join(f"{n}×{c}" for n, c in tc.most_common(TOP_TOOLS))
    sid_short = (session_id or "unknown")[:8]
    parts: list[str] = []
    parts.append(f"## {now.strftime('%H:%M:%S')} · {agent} · {sid_short}")
    parts.append("")
    if sig["user_msgs"]:
        parts.append("**用户消息**（最近 5 条，前 200 字）：")
        for m in sig["user_msgs"][-5:]:
            parts.append(f"- {m}")
        parts.append("")
    if sig["files_modified"]:
        parts.append(f"**修改文件**（共 {len(sig['files_modified'])}）：")
        for f in sig["files_modified"][:20]:
            parts.append(f"- `{f}`")
        parts.append("")
    if sig["bash_cmds"]:
        parts.append(f"**Bash 命令**（最后 5 条）：")
        for c in sig["bash_cmds"][-5:]:
            parts.append(f"- `{c}`")
        parts.append("")
    parts.append(f"**工具统计**：{tool_summary}")
    parts.append("")
    parts.append(f"_transcript: `{transcript_path}`_")
    parts.append("")
    return "\n".join(parts)

def write_beat(beat_md: str, session_id: str, now: dt.datetime) -> Path:
    day_dir = BEATS_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    sid = re.sub(r"[^A-Za-z0-9_\-]", "-", session_id or "unknown")[:32]
    fp = day_dir / f"{sid}.md"
    is_new = not fp.exists()

    with fp.open("a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        if is_new:
            f.write(
                "---\n"
                "type: session-beats\n"
                "status: 待复盘\n"
                f"date: {now.strftime('%Y-%m-%d')}\n"
                f"session_id: {session_id}\n"
                "---\n\n"
                f"# Session Beats — {sid}\n\n"
                "> 由 Stop hook 自动追加。不写到长期分区，等后续复盘时升级。\n\n"
            )
        f.write(beat_md)
        f.write("\n")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    return fp

def main() -> int:
    agent = parse_args()
    payload = read_stdin_json()
    transcript_path = payload.get("transcript_path") or os.environ.get("CLAUDE_TRANSCRIPT_PATH")
    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or os.environ.get("CLAUDE_SESSION_ID")
        or ""
    )

    if not transcript_path:
        log("no transcript_path in stdin/env; nothing to do")
        return 0

    sig = extract_signals(transcript_path)
    if not sig:
        return 0
    if not should_write(sig):
        log(
            f"below threshold (msgs={len(sig['user_msgs'])}, "
            f"tools={len(sig['tool_uses'])}, edits={len(sig['files_modified'])}); skip"
        )
        return 0

    now = dt.datetime.now()
    beat_md = render_beat(sig, agent, session_id, transcript_path, now)
    fp = write_beat(beat_md, session_id, now)
    log(f"wrote beat to {fp}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"unexpected error: {e}")
        sys.exit(0)
