#!/usr/bin/env python3
"""Write a shared Obsidian agent memory note for your AI coding agents."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import re
import sys
from pathlib import Path


VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))

TYPE_DIR = {
    "inbox": "00_Inbox",
    "user": "01_User",
    "agent": "02_Agents",
    "project": "03_Projects",
    "knowledge": "04_Knowledge",
    "daily": "05_Daily",
    "decision": "06_Decisions",
    "playbook": "07_Playbooks",
    "source": "08_Sources",
    "archive": "09_Archive",
    "resume": "10_Resume",
}

INDEX_NAME = {
    "decision": "决策索引.md",
    "playbook": "工作流索引.md",
    "knowledge": "知识索引.md",
    "source": "来源索引.md",
    "resume": "恢复点索引.md",
}

AGENT_FILE = {
    "codex": "02_Agents/Codex.md",
    "claude-code": "02_Agents/Claude Code.md",
    "hermes": "02_Agents/Hermes.md",
}


def slugify(text: str) -> str:
    text = text.strip()
    # Callers often prefix the title with today's date; the script ALSO prepends
    # `{date}-`, which produced filenames like `2026-06-07-2026-06-07-foo.md`.
    # Strip any leading date so the prefix is never doubled.
    text = re.sub(r"^\d{4}-\d{2}-\d{2}[-\s]*", "", text)
    text = re.sub(r"[\\/:*?\"<>|#^\[\]]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = text.strip("-")
    return text[:80] or "untitled"


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    return args.body or ""


def auto_links(args: argparse.Namespace) -> list[str]:
    """Wikilinks that the script injects automatically."""
    links: list[str] = []
    agent_key = args.agent.strip().lower()
    if agent_key in AGENT_FILE:
        stem = Path(AGENT_FILE[agent_key]).stem
        links.append(f"[[02_Agents/{stem}|{stem}]]")
    if args.project and args.type == "project":
        links.append(f"[[03_Projects/{args.project}|{args.project}]]")
    return links


def build_note(args: argparse.Namespace, body: str) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tags = ", ".join(args.tags or [])

    related = list(auto_links(args))
    for link in args.links or []:
        link = link.strip()
        if not link:
            continue
        if not (link.startswith("[[") and link.endswith("]]")):
            link = f"[[{link}]]"
        if link not in related:
            related.append(link)

    related_section = "\n".join(f"- {l}" for l in related) if related else "暂无"

    return f"""---
type: {args.type}
status: {args.status}
source_agent: {args.agent}
created: {now}
confidence: {args.confidence}
tags: [{tags}]
---

# {args.title}

## 摘要

{args.summary or "待补充"}

## 内容

{body.strip() or "待补充"}

## 后续

{args.next or "暂无"}

## 相关

{related_section}
"""


def append_log(agent: str, ntype: str, path: Path, title: str) -> None:
    """Write to both the machine-readable TSV log and the agent-readable markdown log."""
    log_dir = VAULT / "_system" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 1) machine-readable TSV (already in use; tooling can grep/awk this)
    tsv_path = log_dir / "agent_memory_writes.log"
    tsv_line = f"{dt.datetime.now().isoformat()}\t{agent}\t{ntype}\t{path}\n"
    with tsv_path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(tsv_line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # 2) agent-readable markdown log (chronological, with wikilinks)
    md_path = VAULT / "_system" / "log.md"
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rel = path.relative_to(VAULT) if path.is_absolute() else path
    md_line = f"- {stamp} [{agent}] [{ntype}] [[{path.stem}|{title}]] `{rel}`\n"
    if not md_path.exists():
        header = ("# Operation Log\n\n"
                  "> 由 `agent_memory.py` 自动追加。最新条目在底部。\n"
                  "> 跨 agent 共享的时间线，agent 上手时 `tail -30` 拿动态。\n\n")
        md_path.write_text(header, encoding="utf-8")
    with md_path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(md_line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def append_index(ntype: str, title: str, slug_path: Path) -> None:
    """Append a one-line entry to the index file of the given section."""
    index_filename = INDEX_NAME.get(ntype)
    if not index_filename:
        return
    index_path = VAULT / TYPE_DIR[ntype] / index_filename
    index_path.parent.mkdir(parents=True, exist_ok=True)

    stem = slug_path.stem
    entry = f"- [[{stem}|{title}]]"

    if index_path.exists():
        existing = index_path.read_text(encoding="utf-8")
        if stem in existing:
            return
        header_present = existing.lstrip().startswith("#")
        joiner = "" if existing.endswith("\n") else "\n"
        new_content = existing + joiner + entry + "\n"
    else:
        header = {
            "decision": "# 决策索引",
            "playbook": "# 工作流索引",
            "knowledge": "# 知识索引",
            "source": "# 来源索引",
            "resume": "# 恢复点索引",
        }[ntype]
        new_content = f"{header}\n\n{entry}\n"

    with index_path.open("w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(new_content)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a shared Obsidian agent memory note.")
    parser.add_argument("--type", choices=sorted(TYPE_DIR), default="inbox")
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--status", default="已确认")
    parser.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--project", default="")
    parser.add_argument("--tags", nargs="*", default=[])
    parser.add_argument("--links", nargs="*", default=[],
                        help="Extra wikilinks to inject under ## 相关. "
                             "Accepts 'Some Title' or '[[Some Title]]'.")
    parser.add_argument("--next", default="")
    parser.add_argument("--print-path", action="store_true")
    args = parser.parse_args()

    body = read_body(args)
    date = dt.datetime.now().strftime("%Y-%m-%d")
    root = VAULT / TYPE_DIR[args.type]
    if args.project and args.type == "project":
        root = root / args.project
    root.mkdir(parents=True, exist_ok=True)

    path = root / f"{date}-{slugify(args.title)}.md"
    note = build_note(args, body)
    if path.exists():
        stamp = dt.datetime.now().strftime("%H%M%S")
        path = root / f"{date}-{slugify(args.title)}-{stamp}.md"
    path.write_text(note, encoding="utf-8")

    append_log(args.agent, args.type, path, args.title)
    append_index(args.type, args.title, path)

    if args.print_path:
        print(path)
    else:
        print(f"wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
