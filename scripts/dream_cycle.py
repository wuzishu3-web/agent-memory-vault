#!/usr/bin/env python3
"""
dream_cycle.py — vault 每夜整合周期

按顺序执行：
  1. 重抽关系图谱（extract_relations.py）
  2. 重建嵌入索引（build_embeddings.py，增量）
  3. 跑 vault health_check（保留原行为，不强制）
  4. 刷新 hot.md 的"高频实体"段（基于最近 7 天文件 + relations）
  5. 输出 dream_cycle 摘要日志

设计原则：
  - 每一步独立失败不阻塞下一步
  - 输出 JSON 摘要供 cron announce
  - 增量优先（embeddings 走 mtime + 内容哈希）

零外部依赖（仅标准库）。
"""

from __future__ import annotations

import os

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

DEFAULT_VAULT = Path(os.path.expanduser("~/agent-memory-vault"))
SCRIPTS_DIR = "_system/scripts"


def run_step(name: str, cmd: list[str], cwd: Path) -> dict:
    """运行一步，返回 {name, ok, duration, stdout, stderr}。"""
    print(f"\n=== [dream] {name} ===", file=sys.stderr)
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600,
        )
        ok = result.returncode == 0
        return {
            "name": name,
            "ok": ok,
            "duration": round(time.time() - t0, 2),
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "ok": False,
            "duration": round(time.time() - t0, 2),
            "stdout": "",
            "stderr": "timeout (10min)",
            "returncode": -1,
        }
    except Exception as e:
        return {
            "name": name,
            "ok": False,
            "duration": round(time.time() - t0, 2),
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        }


def collect_recent_entities(vault: Path, days: int = 7) -> list[tuple[str, int]]:
    """收集最近 N 天内修改的笔记里出现的实体（基于 wikilink 和已知白名单）。"""
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    counter: Counter = Counter()

    # 加载白名单
    rel_path = vault / "_system" / "relations.json"
    whitelist: set[str] = set()
    if rel_path.exists():
        try:
            data = json.loads(rel_path.read_text(encoding="utf-8"))
            for r in data.get("relations", []):
                whitelist.add(r.get("subject", ""))
                whitelist.add(r.get("object", ""))
        except Exception:
            pass

    wikilink_re = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]+)?\]\]")

    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault)
        if any(p in {"_system", "00_Inbox", "09_Archive"} for p in rel.parts):
            continue
        if dt.datetime.fromtimestamp(md.stat().st_mtime) < cutoff:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        # wikilinks
        for m in wikilink_re.finditer(text):
            target = m.group(1).strip().split("/")[-1]
            if 2 <= len(target) <= 30 and "/" not in target:
                counter[target] += 1
        # 白名单实体直接 grep
        for ent in whitelist:
            if ent and len(ent) >= 2 and ent in text:
                counter[ent] += 1

    # top N
    return counter.most_common(15)


def update_hot_entities(vault: Path, entities: list[tuple[str, int]]) -> bool:
    """在 hot.md 的"高频实体"段写入或更新。"""
    hot_path = vault / "_system" / "hot.md"
    if not hot_path.exists():
        return False
    try:
        text = hot_path.read_text(encoding="utf-8")
    except Exception:
        return False

    block_start = "<!-- DREAM_ENTITIES_START -->"
    block_end = "<!-- DREAM_ENTITIES_END -->"
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    block = [
        block_start,
        f"## 高频实体（{today} 自动）",
        "",
    ]
    if entities:
        for ent, count in entities[:10]:
            block.append(f"- **{ent}** ×{count}")
    else:
        block.append("- 暂无（最近 7 天 vault 无明显活跃实体）")
    block.append("")
    block.append(block_end)
    block_text = "\n".join(block)

    if block_start in text and block_end in text:
        # 替换已有块
        new_text = re.sub(
            re.escape(block_start) + r".*?" + re.escape(block_end),
            block_text,
            text,
            count=1,
            flags=re.DOTALL,
        )
    else:
        # 追加到文末
        new_text = text.rstrip() + "\n\n" + block_text + "\n"

    hot_path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="vault 每夜整合周期")
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--skip-health", action="store_true", help="跳过 health_check")
    ap.add_argument("--skip-embeddings", action="store_true", help="跳过 embeddings 重建")
    ap.add_argument("--rebuild-embeddings", action="store_true",
                    help="完全重建 embeddings（默认增量）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    vault: Path = args.vault.resolve()
    scripts = vault / SCRIPTS_DIR

    started_at = dt.datetime.now().isoformat(timespec="seconds")
    steps: list[dict] = []

    # Step 1: relations
    steps.append(run_step(
        "extract_relations",
        [sys.executable, str(scripts / "extract_relations.py"),
         "--vault", str(vault)],
        cwd=vault,
    ))

    # Step 2: embeddings
    if not args.skip_embeddings:
        cmd = [sys.executable, str(scripts / "build_embeddings.py"),
               "--vault", str(vault)]
        if args.rebuild_embeddings:
            cmd.append("--rebuild")
        steps.append(run_step("build_embeddings", cmd, cwd=vault))

    # Step 3: health check（可选）
    if not args.skip_health:
        steps.append(run_step(
            "vault_health_check",
            [sys.executable, str(scripts / "vault_health_check.py")],
            cwd=vault,
        ))

    # Step 4: hot 实体刷新
    print("\n=== [dream] update_hot_entities ===", file=sys.stderr)
    t0 = time.time()
    try:
        entities = collect_recent_entities(vault, days=7)
        ok = update_hot_entities(vault, entities)
        steps.append({
            "name": "update_hot_entities",
            "ok": ok,
            "duration": round(time.time() - t0, 2),
            "stdout": f"top entities: {entities[:10]}",
            "stderr": "" if ok else "hot.md 不存在或写入失败",
            "returncode": 0 if ok else 1,
        })
    except Exception as e:
        steps.append({
            "name": "update_hot_entities",
            "ok": False,
            "duration": round(time.time() - t0, 2),
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        })

    finished_at = dt.datetime.now().isoformat(timespec="seconds")
    summary = {
        "version": "1.0",
        "started_at": started_at,
        "finished_at": finished_at,
        "steps": steps,
        "ok": all(s["ok"] for s in steps),
    }

    # 写日志
    log_dir = vault / "_system" / "dream_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"dream_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== Dream Cycle 完成 ===", file=sys.stderr)
        for s in steps:
            mark = "✓" if s["ok"] else "✗"
            print(f"  {mark} {s['name']:25s} {s['duration']:6.2f}s", file=sys.stderr)
        print(f"\n日志: {log_path.relative_to(vault)}", file=sys.stderr)

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
