#!/usr/bin/env python3
"""Daily health check of the user's shared agent memory vault.

Scans for:
- 30+ day untouched files in 00_Inbox (should be promoted or archived)
- Notes with `status: 待核验` older than 7 days
- Frontmatter parse failures or missing required fields
- Resume points older than 14 days (stale)
- Orphan notes (no incoming wikilinks, excluding index/system pages)

Writes report to _system/logs/vault-health-YYYY-MM-DD.log (machine heartbeat, never to 05_Daily).
Intended to be invoked from cron / a scheduler.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))

EXCLUDE_DIRS = {".obsidian", "09_Archive", "_system"}
SKIP_FILENAMES = {"README.md", "欢迎.md"}
REQUIRED_FRONTMATTER = {"type", "status"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def iter_notes() -> list[Path]:
    notes: list[Path] = []
    for path in VAULT.rglob("*.md"):
        if any(part in EXCLUDE_DIRS for part in path.relative_to(VAULT).parts):
            continue
        if path.name in SKIP_FILENAMES:
            continue
        notes.append(path)
    return notes


def days_since_modified(path: Path) -> int:
    return (dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)).days


def collect_wikilink_targets(notes: list[Path]) -> set[str]:
    """Return set of all wikilink stems referenced anywhere in vault."""
    targets: set[str] = set()
    for note in notes:
        try:
            text = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in WIKILINK_RE.findall(text):
            target = match.split("|", 1)[0].strip()
            target = target.split("#", 1)[0]
            stem = target.rsplit("/", 1)[-1]
            targets.add(stem)
    return targets


def check_vault() -> dict[str, list[str]]:
    findings: dict[str, list[str]] = defaultdict(list)
    notes = iter_notes()
    referenced = collect_wikilink_targets(notes)

    for note in notes:
        rel = note.relative_to(VAULT)
        try:
            text = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            findings["read_errors"].append(f"{rel}: {e}")
            continue

        age = days_since_modified(note)
        fm = parse_frontmatter(text)

        # 1) stale inbox
        if rel.parts[0] == "00_Inbox" and age > 30:
            findings["stale_inbox"].append(f"{rel} (age {age}d)")

        # 2) overdue 待核验
        if fm.get("status") == "待核验" and age > 7:
            findings["overdue_pending"].append(f"{rel} (age {age}d)")

        # 3) frontmatter missing required fields (only check for non-index, non-readme)
        if rel.parts[0] not in {"00_Inbox", "10_Resume"} and "索引" not in note.name:
            missing = REQUIRED_FRONTMATTER - fm.keys()
            if missing and fm:  # only complain when fm exists but incomplete
                findings["frontmatter_incomplete"].append(
                    f"{rel} (missing: {', '.join(sorted(missing))})"
                )

        # 4) stale resume points
        if rel.parts[0] == "10_Resume" and note.name != "README.md" and age > 14:
            findings["stale_resume"].append(f"{rel} (age {age}d)")

        # 5) orphan note — no wikilink references TO this stem
        if rel.parts[0] in {"03_Projects", "04_Knowledge", "06_Decisions",
                            "07_Playbooks", "08_Sources"} and "索引" not in note.name:
            if note.stem not in referenced:
                findings["orphan_notes"].append(f"{rel}")

    return findings


def render_report(findings: dict[str, list[str]]) -> str:
    sections = [
        ("stale_inbox", "00_Inbox 中 30 天未动的草稿（建议归档或晋升）"),
        ("overdue_pending", "标记 `待核验` 超过 7 天未更新"),
        ("frontmatter_incomplete", "frontmatter 缺字段"),
        ("stale_resume", "10_Resume 中 14 天未更新的恢复点（任务可能已废弃）"),
        ("orphan_notes", "无反向链接的孤立笔记"),
        ("read_errors", "读取失败"),
    ]
    parts: list[str] = []
    total = 0
    for key, label in sections:
        items = findings.get(key, [])
        total += len(items)
        if not items:
            continue
        parts.append(f"### {label}（{len(items)}）")
        for it in items[:20]:
            parts.append(f"- {it}")
        if len(items) > 20:
            parts.append(f"- ... 还有 {len(items) - 20} 条")
        parts.append("")
    if not parts:
        return "## 巡检结果\n\nvault 健康，无需关注。\n"
    header = f"## 巡检结果\n\n共发现 {total} 项问题，分类如下：\n\n"
    return header + "\n".join(parts)


def write_report_to_logs(report: str, total_issues: int) -> int:
    """Write machine heartbeat report to _system/logs/ only. Never to vault daily."""
    logs_dir = VAULT / "_system" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    log_path = logs_dir / f"vault-health-{date_str}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"# Vault Health Check {date_str}\n\n")
        f.write(f"Total issues: {total_issues}\n\n")
        f.write(report)
    # Also echo to stdout for cron capture
    print(f"Vault health report written to {log_path}")
    print(report)
    return 0


def main() -> int:
    if not VAULT.exists():
        sys.stderr.write(f"vault not found: {VAULT}\n")
        return 1
    findings = check_vault()
    total = sum(len(v) for v in findings.values())
    report = render_report(findings)
    return write_report_to_logs(report, total)


if __name__ == "__main__":
    raise SystemExit(main())
