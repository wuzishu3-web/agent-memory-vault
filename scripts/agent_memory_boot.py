#!/usr/bin/env python3
"""Print the mandatory shared-memory startup checklist for your AI coding agents."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
from collections import OrderedDict
from pathlib import Path


VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))

# Canonical agent → 02_Agents file. Multiple aliases (Chinese, pinyin, short)
# resolve to the same file so callers can pass whichever name they prefer.
AGENT_FILES = {
    # Canonical agent name → its profile under 02_Agents/.
    # Add your own agents here; keys are matched case-insensitively.
    "codex": "02_Agents/Codex.md",
    "claude-code": "02_Agents/Claude Code.md",
    "claude_code": "02_Agents/Claude Code.md",
    "claudecode": "02_Agents/Claude Code.md",
    "hermes": "02_Agents/Hermes.md",
}


def exists_marker(path: Path) -> str:
    return "OK" if path.exists() else "MISSING"


def latest_files(root: Path, limit: int = 5) -> list[Path]:
    if not root.exists():
        return []
    files = [p for p in root.rglob("*.md") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


# 排除这些路径（系统脚本/索引/日志/历史归档/示例），避免噪声盖过项目笔记
SEARCH_EXCLUDE_DIRS = {
    "_system",     # 含 log.md / hot.md / scripts / agent-rules（太通用）
    "09_Archive",  # 历史归档
    "08_Sources",  # 抓回来的原文（噪声）
    ".obsidian",
    ".git",
    "external",
}


def extract_keywords(task: str) -> list[str]:
    """从 task 文本里提关键词。简单切分：按非字母汉字数字字符 + 过滤短词。"""
    if not task or task == "未填写":
        return []
    # 把空格 / 标点 / 引号都当分隔符
    tokens = re.split(r"[\s，。、；：/\\()（）\[\]【】<>《》\"'`!?！？.,:;]+", task)
    seen = OrderedDict()
    for t in tokens:
        t = t.strip()
        # 中文词 ≥ 2 字；英文词 ≥ 3 字才有效
        is_cjk = any("一" <= c <= "鿿" for c in t)
        if is_cjk and len(t) < 2:
            continue
        if not is_cjk and len(t) < 3:
            continue
        if t.lower() in {"the", "and", "for", "with", "from", "into"}:
            continue
        seen[t] = None
    return list(seen.keys())[:8]


def search_vault(keywords: list[str], limit: int = 5) -> list[tuple[Path, str]]:
    """对每个 keyword grep vault 中 .md 文件，返回 top-N 命中（按 mtime 排序）。

    返回 (path, hit_keyword) 列表。
    """
    if not keywords:
        return []
    candidates: dict[Path, str] = {}
    for kw in keywords:
        # 用 grep -rl 拿命中文件清单
        try:
            result = subprocess.run(
                ["grep", "-rli", "--include=*.md", kw, str(VAULT)],
                capture_output=True, text=True, timeout=20,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        for line in result.stdout.splitlines():
            p = Path(line.strip())
            if not p.exists() or not p.is_file():
                continue
            # 过滤排除目录
            try:
                rel = p.relative_to(VAULT)
            except ValueError:
                continue
            top = rel.parts[0] if rel.parts else ""
            if top in SEARCH_EXCLUDE_DIRS:
                continue
            # 已记录则保留更早的命中关键词
            if p not in candidates:
                candidates[p] = kw
    # 按修改时间降序排序，取 top-N
    ranked = sorted(
        candidates.items(),
        key=lambda kv: kv[0].stat().st_mtime,
        reverse=True,
    )
    return ranked[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Show mandatory shared memory boot context.")
    parser.add_argument("--agent", required=True,
                        help="claude-code / codex / hermes "
                             "(case-insensitive; add your own in AGENT_FILES)")
    parser.add_argument("--task", default="未填写", help="Current task summary")
    parser.add_argument("--print-content", action="store_true",
                        help="Also print the full content of each required file")
    args = parser.parse_args()

    agent = args.agent.strip().lower()
    # Match either by lowered ASCII key or by raw (for CJK aliases).
    agent_file = AGENT_FILES.get(agent) or AGENT_FILES.get(args.agent.strip(), "")
    required = [
        "index.md",
        "_system/WRITE_GUIDE.md",  # 写入决策树：什么内容写哪个 type、命名、谁写（写库前必读）
        "_system/AGENT_MEMORY_PROTOCOL.md",
        "01_User/profile.md",
    ]
    if agent_file:
        required.append(agent_file)

    print("Agent Memory Boot")
    print(f"Time: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Vault: {VAULT}")
    print(f"Agent: {args.agent}")
    print(f"Task: {args.task}")

    # Hot cache always comes first. If it exists, dump it inline so the agent
    # gets full recent context in one read.
    hot_path = VAULT / "_system" / "hot.md"
    print()
    print("=== Hot Cache (_system/hot.md) ===")
    if hot_path.exists():
        print(hot_path.read_text(encoding="utf-8").rstrip())
    else:
        print("(hot cache missing — first session, or wiped)")

    # Last 20 log entries for short-term continuity.
    log_path = VAULT / "_system" / "log.md"
    print()
    print("=== 最近 20 条操作 (_system/log.md tail) ===")
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        bullets = [ln for ln in lines if ln.startswith("- ")]
        for ln in bullets[-20:]:
            print(ln)
    else:
        print("(log empty)")

    print()
    print("=== 必须读取的文件 ===")
    for rel in required:
        path = VAULT / rel
        print(f"- [{exists_marker(path)}] {path}")

    print()
    print("最近项目/日报记录：")
    recent_roots = [VAULT / "03_Projects", VAULT / "05_Daily", VAULT / "06_Decisions"]
    seen: set[Path] = set()
    for root in recent_roots:
        for path in latest_files(root, limit=3):
            if path in seen:
                continue
            seen.add(path)
            print(f"- {path}")

    # 新增：按当前 task 关键词主动检索 vault top 5
    print()
    print("=== 任务关键词命中（按修改时间排序 top 5） ===")
    keywords = extract_keywords(args.task)
    if keywords:
        print(f"关键词: {', '.join(keywords)}")
        hits = search_vault(keywords, limit=5)
        if hits:
            for path, kw in hits:
                try:
                    rel = path.relative_to(VAULT)
                except ValueError:
                    rel = path
                mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                print(f"- [{kw}] {rel}  ({mtime})")
            print()
            print("⚠️ 如命中文件涉及具体项目，**主动 ls 对应代码目录 + launchctl list + tail log** 校验真实状态，再做计划。vault 笔记可能是过期快照。")
        else:
            print("(无命中)")
    else:
        print("(--task 未提供有效关键词，跳过)")

    print()
    print("强制流程：")
    print("1. 先读上方 OK 文件，再开始回答或操作。")
    print("2. 历史记忆只作背景，当前用户最新指令优先。")
    print("3. 涉及配置/项目/偏好/验收/长任务恢复点，收尾必须写入 agent_memory.py。")
    print("4. 禁止写入 key、token、密码、Cookie、客户隐私、原始聊天全文。")

    if args.print_content:
        print()
        print("=== 文件内容 ===")
        for rel in required:
            path = VAULT / rel
            print()
            print(f"----- {rel} -----")
            if path.exists():
                print(path.read_text(encoding="utf-8"))
            else:
                print("(MISSING)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
