#!/usr/bin/env python3
"""
ingest_stop_hook.py — Stop hook：外部源「自动入库」守门人（三 agent 共享）

目的：实现"全自动入库"，但**绝不产生半成品**。
做法：本 hook 自己**不做摘要**（廉价自动摘要正是半成品的根源），它只检测
"本轮处理过外部源、却没跑 ingest"，命中就用 exit 2 把控制权交还给 agent，
让 agent 用它**在场的高质量分析**去跑 ingest.py。摘要永远来自真 agent，不来自脚本。

防打扰：
  - 只在出现**强外部源信号**（WebFetch / defuddle / douyin 扒取 / 深研）时触发。
  - 每个 session **最多拦一次**（state flag），拦过就放行，绝不死循环。
  - 任何异常一律 exit 0，不阻塞主对话。

stdin schema (Claude Code Stop hook): {"transcript_path": "...", "session_id": "...", ...}
退出码：0 = 放行；2 = 拦截并把 stderr 文本回灌给 agent（Claude Code Stop hook 约定）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))
STATE_DIR = VAULT / "_system" / "state" / "ingest_reminded"
MAX_TRANSCRIPT_LINES = 5000

# 强外部源信号：工具名 / Bash 命令子串 / 技能名
SOURCE_TOOL_NAMES = {"WebFetch"}
SOURCE_TOOL_SUBSTR = ("douyin",)                       # mcp__douyin__* 等
SOURCE_CMD_SUBSTR = ("fetch.sh", "defuddle", "yt-dlp")  # douyin 扒取 / 网页清洗
SOURCE_SKILLS = ("douyin-analyze", "defuddle", "deep-research")
INGEST_MARKER = "ingest.py"                            # 已入库的标志


def log(msg: str) -> None:
    print(f"[ingest-hook] {msg}", file=sys.stderr)


def parse_agent() -> str:
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--agent" and i + 1 < len(argv):
            return argv[i + 1]
    return "claude-code"


def read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read(4 * 1024 * 1024)
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def scan_transcript(path: str) -> dict | None:
    """扫 transcript，判断：有无外部源信号 / 有无 ingest / 分析是否够分量。"""
    p = Path(path)
    if not p.exists():
        return None
    saw_source = False
    saw_ingest = False
    tool_count = 0
    assistant_chars = 0
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
                msg = entry.get("message") or entry
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    # 用户消息纯文本里出现 douyin 分享/技能调用也算
                    if isinstance(content, str) and any(s in content for s in SOURCE_SKILLS):
                        saw_source = True
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        assistant_chars += len(block.get("text") or "")
                    elif btype == "tool_use":
                        tool_count += 1
                        name = block.get("name") or ""
                        inp = block.get("input") or {}
                        if name in SOURCE_TOOL_NAMES or any(s in name for s in SOURCE_TOOL_SUBSTR):
                            saw_source = True
                        if name == "Bash":
                            cmd = inp.get("command") or ""
                            if any(s in cmd for s in SOURCE_CMD_SUBSTR):
                                saw_source = True
                            if INGEST_MARKER in cmd:
                                saw_ingest = True
                        if name == "Skill":
                            sk = (inp.get("skill") or inp.get("command") or "")
                            if any(s in str(sk) for s in SOURCE_SKILLS):
                                saw_source = True
    except Exception as e:
        log(f"scan error: {e}")
        return None
    return {
        "saw_source": saw_source,
        "saw_ingest": saw_ingest,
        "tool_count": tool_count,
        "assistant_chars": assistant_chars,
    }


def already_reminded(session_id: str) -> bool:
    flag = STATE_DIR / (re.sub(r"[^A-Za-z0-9_\-]", "-", session_id or "unknown")[:48] + ".flag")
    return flag.exists()


def mark_reminded(session_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    flag = STATE_DIR / (re.sub(r"[^A-Za-z0-9_\-]", "-", session_id or "unknown")[:48] + ".flag")
    try:
        flag.write_text("reminded\n", encoding="utf-8")
    except Exception:
        pass


REMINDER = """\
[入库守门人] 本轮处理过外部源（文章/抖音/网页/仓库），但还没入库。

若该内容值得长期保留，请用你本轮的高质量分析跑入库管道（摘要要用你自己的分析，别让脚本凑）：

  python3 "{vault}/_system/scripts/ingest.py" --agent {agent} \\
    --title "<主题短语>" --summary "<你的分析摘要>" \\
    --body-file <要点正文文件> --url "<源链接>" --source-type <article|video|repo|transcript>

它会自动：写 source 页 + 双向交叉引用 + 质量闸门 + 重建索引。

如果这条外部源不值得留（只是顺手查一下），直接说明"无需入库"即可正常结束，本会话不会再提醒。\
"""


def main() -> int:
    agent = parse_agent()
    payload = read_stdin_json()
    transcript_path = payload.get("transcript_path") or os.environ.get("CLAUDE_TRANSCRIPT_PATH")
    session_id = payload.get("session_id") or payload.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID") or ""

    if not transcript_path:
        return 0
    sig = scan_transcript(transcript_path)
    if not sig:
        return 0

    # 放行条件：没碰外部源 / 已经 ingest / 分析不够分量 / 本会话已提醒过
    if not sig["saw_source"]:
        return 0
    if sig["saw_ingest"]:
        return 0
    if sig["assistant_chars"] < 400 and sig["tool_count"] < 8:
        return 0  # 只是顺手查一下，没做实质分析，不打扰
    if already_reminded(session_id):
        return 0

    # 拦截一次：把控制权交还 agent
    mark_reminded(session_id)
    print(REMINDER.format(vault=VAULT, agent=agent), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"unexpected: {e}")
        sys.exit(0)
