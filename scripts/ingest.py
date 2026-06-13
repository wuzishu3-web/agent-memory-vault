#!/usr/bin/env python3
"""
ingest.py — 外部知识入库管道（三 agent 共享：claude-code / codex / hermes）

把"任意外部源（文章 / 视频 / 仓库 / 转录）"变成 vault 里一篇结构化 source 页，
并完成 Karpathy LLM Wiki Pattern 里最值钱、本库此前缺失的那一步——**交叉引用传播**：
自动找出语义最相近的若干旧页，双向写入 [[链接]]，让新知识织进既有的知识网。

设计原则：把"理解"与"记账"分开
  - 理解（摘要 / 要点）：由调用的 agent 在场产出（高质量），经 --summary/--body 传入。
  - 记账（写页 / 交叉引用 / 索引 / log）：本脚本确定性完成，可全自动。

质量闸门（不达标一律不进 08_Sources，落 00_Inbox 标 待核验）：
  1. 摘要校验：摘要字数 + 正文字数达阈值。
  2. 交叉引用阈值：cosine ≥ --sim-threshold 才连，宁缺毋滥。
  3. 去重：按 url / 标题 查 08_Sources，已有则更新不新建。

降级：LM Studio（:1234）不在跑时，照常写页，跳过交叉引用/重嵌，页内标 待索引。

复用既有脚本：agent_memory（log/index）、query_vault（向量检索）、
build_embeddings（增量重嵌）、extract_relations（关系抽取）。零额外第三方依赖。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 复用现有零件 ---------------------------------------------------------------
from agent_memory import (  # type: ignore
    VAULT, TYPE_DIR, AGENT_FILE, slugify, append_log, append_index,
)
from query_vault import (  # type: ignore
    cosine, embed_query, DEFAULT_LM_STUDIO_URL, DEFAULT_EMBED_MODEL,
)

# 闸门阈值（可被 CLI 覆盖）----------------------------------------------------
MIN_SUMMARY_CHARS = 20
MIN_BODY_CHARS = 50
DEFAULT_SIM_THRESHOLD = 0.75
DEFAULT_TOPK = 5
DEFAULT_MAX_MUTATE = 5          # 每次最多回写多少篇旧页
EMBED_INPUT_CAP = 4000          # 嵌入新页时截断长度

SOURCE_DIR = TYPE_DIR["source"]   # 08_Sources
INBOX_DIR = TYPE_DIR["inbox"]     # 00_Inbox


# ============================== 工具 ==============================

def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    return args.body or ""


def url_hash(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:12]


# ============================== 质量闸门 ==============================

def quality_check(summary: str, body: str, min_summary: int, min_body: int) -> list[str]:
    """返回不合格原因列表；空列表表示通过。"""
    problems: list[str] = []
    if len((summary or "").strip()) < min_summary:
        problems.append(f"摘要过短(<{min_summary}字)")
    if len((body or "").strip()) < min_body:
        problems.append(f"正文过短(<{min_body}字)")
    return problems


def find_duplicate(url: str, title: str) -> Path | None:
    """按 url（frontmatter）或标题 slug 在 08_Sources 查重。命中返回已有页路径。"""
    src_root = VAULT / SOURCE_DIR
    if not src_root.is_dir():
        return None
    want_slug = slugify(title)
    url_norm = (url or "").strip().rstrip("/")
    for md in src_root.rglob("*.md"):
        if md.name.endswith("索引.md"):
            continue
        try:
            head = md.read_text(encoding="utf-8")[:600]
        except Exception:
            continue
        if url_norm and re.search(r"(?m)^url:\s*" + re.escape(url_norm) + r"/?\s*$", head):
            return md
        # 文件名去日期前缀后与 slug 完全相同也算重复
        stem_noprefix = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", md.stem)
        if stem_noprefix == want_slug:
            return md
    return None


# ============================== 页面写入 ==============================

def build_source_page(args: argparse.Namespace, body: str, related_links: list[str],
                      indexed: bool) -> str:
    tags = ", ".join(args.tags or [])
    rel_section = "\n".join(f"- {l}" for l in related_links) if related_links else "暂无"
    status = args.status
    if not indexed and status == "已确认":
        status = "已确认（待索引）"
    url_line = f"url: {args.url}" if args.url else "url:"
    src_block = f"- URL: {args.url}" if args.url else "- （无外部 URL，本地源）"
    return f"""---
type: source
status: {status}
source_agent: {args.agent}
created: {now_str()}
confidence: {args.confidence}
source_type: {args.source_type}
{url_line}
tags: [{tags}]
---

# {args.title}

## 摘要

{(args.summary or '待补充').strip()}

## 要点 / 内容

{body.strip() or '待补充'}

## 来源

{src_block}

## 相关

{rel_section}
"""


def build_quarantine_page(args: argparse.Namespace, body: str, problems: list[str]) -> str:
    tags = ", ".join(args.tags or [])
    url_line = f"url: {args.url}" if args.url else "url:"
    return f"""---
type: inbox
status: 待核验
source_agent: {args.agent}
created: {now_str()}
confidence: low
source_type: {args.source_type}
{url_line}
tags: [{tags}]
quarantine_reason: {'; '.join(problems)}
---

# {args.title}

> ⚠️ 未过质量闸门，已隔离到 00_Inbox。原因：{'; '.join(problems)}
> 补全摘要/正文后，重新跑 ingest.py 即可升级进 08_Sources。

## 摘要

{(args.summary or '待补充').strip()}

## 要点 / 内容

{body.strip() or '待补充'}

## 来源

- URL: {args.url or '（无）'}
"""


def write_page(root_dir: str, title: str, content: str, project: str = "") -> Path:
    date = dt.datetime.now().strftime("%Y-%m-%d")
    root = VAULT / root_dir
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{date}-{slugify(title)}.md"
    if path.exists():
        stamp = dt.datetime.now().strftime("%H%M%S")
        path = root / f"{date}-{slugify(title)}-{stamp}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ============================== 交叉引用 ==============================

def embed_new_page(args: argparse.Namespace, body: str) -> list[float] | None:
    text = f"{args.title}\n{args.summary}\n{body}"[:EMBED_INPUT_CAP]
    try:
        return embed_query(text, args.url_lm, args.model)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"[warn] LM Studio 不可用，跳过交叉引用/重嵌: {e}", file=sys.stderr)
        return None


def find_related(qvec: list[float], exclude_rel: str, threshold: float, topk: int) -> list[tuple[str, float]]:
    """对每个旧页取其 chunk 最高 cosine，过阈值后取 topk。返回 [(rel_path, score)]。"""
    emb_path = VAULT / "_system" / "embeddings.json"
    if not emb_path.exists():
        return []
    try:
        data = json.loads(emb_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    best: dict[str, float] = {}
    for c in data.get("chunks", []):
        src = c.get("source", "")
        if not src or src == exclude_rel:
            continue
        if src.endswith("索引.md") or src.startswith("_system"):
            continue
        s = cosine(qvec, c["embedding"])
        if s > best.get(src, -1.0):
            best[src] = s
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    return [(src, sc) for src, sc in ranked if sc >= threshold][:topk]


def title_of(rel_path: str) -> str:
    p = VAULT / rel_path
    try:
        m = re.search(r"(?m)^#\s+(.+?)\s*$", p.read_text(encoding="utf-8"))
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return Path(rel_path).stem


def link_to(rel_path: str) -> str:
    stem = Path(rel_path).stem
    return f"[[{stem}|{title_of(rel_path)}]]"


def add_related_link(page_path: Path, link_line: str) -> bool:
    """往一篇旧页的 ## 相关 段追加一条 link（只加不删、去重、文件锁）。"""
    try:
        text = page_path.read_text(encoding="utf-8")
    except Exception:
        return False
    bullet = f"- {link_line}"
    if bullet in text:
        return False  # 已有，去重

    m = re.search(r"(?m)^##\s+相关\s*$", text)
    if m:
        sec_start = m.end()
        nxt = re.search(r"(?m)^##\s+", text[sec_start:])
        sec_end = sec_start + nxt.start() if nxt else len(text)
        section = text[sec_start:sec_end].strip()
        if section in ("暂无", ""):
            new = f"\n\n{bullet}\n" + ("\n" if nxt else "")
            text = text[:sec_start] + new + text[sec_end:]
        else:
            before = text[:sec_end].rstrip()
            after = text[sec_end:]
            text = before + "\n" + bullet + "\n" + ("\n" + after.lstrip("\n") if after.strip() else "\n")
    else:
        text = text.rstrip() + f"\n\n## 相关\n\n{bullet}\n"

    with page_path.open("w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(text)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return True


# ============================== 重建索引 ==============================

def reindex(verbose: bool) -> None:
    for script in ("build_embeddings.py", "extract_relations.py"):
        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / script)],
                capture_output=True, text=True, timeout=300,
            )
            if verbose:
                print(f"[reindex] {script}: rc={r.returncode}", file=sys.stderr)
                if r.stderr.strip():
                    print(r.stderr.strip()[-400:], file=sys.stderr)
        except Exception as e:
            print(f"[warn] {script} 失败: {e}", file=sys.stderr)


# ============================== 主流程 ==============================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="外部知识入库管道（三 agent 共享）")
    ap.add_argument("--agent", default="unknown", help="claude-code | codex | hermes")
    ap.add_argument("--title", required=True)
    ap.add_argument("--summary", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--body-file")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--url", default="", help="外部源 URL（用于查重 + 溯源）")
    ap.add_argument("--source-type", dest="source_type", default="web",
                    choices=["article", "video", "repo", "transcript", "web", "paper"])
    ap.add_argument("--tags", nargs="*", default=[])
    ap.add_argument("--links", nargs="*", default=[], help="额外手动指定的相关 wikilink")
    ap.add_argument("--status", default="已确认")
    ap.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--update", action="store_true", help="命中查重时更新而非跳过")
    ap.add_argument("--sim-threshold", type=float, default=DEFAULT_SIM_THRESHOLD)
    ap.add_argument("--topk", type=int, default=DEFAULT_TOPK)
    ap.add_argument("--max-mutate", type=int, default=DEFAULT_MAX_MUTATE)
    ap.add_argument("--min-summary", type=int, default=MIN_SUMMARY_CHARS)
    ap.add_argument("--min-body", type=int, default=MIN_BODY_CHARS)
    ap.add_argument("--no-index", action="store_true", help="跳过重建 embeddings/relations")
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算并打印将写的页与交叉引用，不落盘、不改旧页")
    ap.add_argument("--url-lm", default=DEFAULT_LM_STUDIO_URL)
    ap.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    body = read_body(args)

    # --- 闸门 1：去重 ---
    dup = find_duplicate(args.url, args.title)
    if dup and not args.update:
        print(f"[skip] 已存在同源页：{dup.relative_to(VAULT)}（加 --update 可更新）")
        return 0

    # --- 闸门 2：质量校验 → 不达标隔离到 00_Inbox ---
    problems = quality_check(args.summary, body, args.min_summary, args.min_body)
    if problems:
        if args.dry_run:
            print(f"[dry-run] 未过闸门 → 将隔离到 {INBOX_DIR}/，原因：{'; '.join(problems)}")
            return 0
        page = build_quarantine_page(args, body, problems)
        path = write_page(INBOX_DIR, args.title, page)
        append_log(args.agent, "inbox", path, args.title + "（待核验·隔离）")
        print(f"[quarantine] 未过质量闸门，已隔离：{path.relative_to(VAULT)}")
        print(f"             原因：{'; '.join(problems)}")
        return 0

    # --- 交叉引用：嵌入新页 → 找相似旧页 ---
    qvec = embed_new_page(args, body)
    indexed = qvec is not None
    # 新页落盘后的相对路径（用于排除自身），先预测 slug
    date = dt.datetime.now().strftime("%Y-%m-%d")
    predicted_rel = f"{SOURCE_DIR}/{date}-{slugify(args.title)}.md"

    related: list[tuple[str, float]] = []
    if qvec is not None:
        related = find_related(qvec, predicted_rel, args.sim_threshold, args.topk)

    # 新页 ## 相关 链接 = 自动交叉引用 + 手动 --links + agent 自身档案
    auto_links = [link_to(rel) for rel, _ in related]
    manual_links = []
    for l in args.links:
        l = l.strip()
        if not l:
            continue
        manual_links.append(l if (l.startswith("[[") and l.endswith("]]")) else f"[[{l}]]")
    agent_link = []
    ak = args.agent.strip().lower()
    if ak in AGENT_FILE:
        stem = Path(AGENT_FILE[ak]).stem
        agent_link = [f"[[02_Agents/{stem}|{stem}]]"]
    new_page_links = agent_link + auto_links + manual_links

    # --- dry-run：只打印 ---
    if args.dry_run:
        print(f"[dry-run] 将写 source 页：{predicted_rel}")
        print(f"[dry-run] 质量闸门：通过（摘要 {len(args.summary)} 字 / 正文 {len(body)} 字）")
        print(f"[dry-run] LM Studio：{'可用' if indexed else '不可用 → 标待索引、跳过交叉引用'}")
        if related:
            print(f"[dry-run] 交叉引用（cosine≥{args.sim_threshold}，将双向写入，最多 {args.max_mutate} 篇）：")
            for rel, sc in related:
                print(f"          {sc:.3f}  {rel}")
        else:
            print(f"[dry-run] 交叉引用：无（无旧页超过阈值，或索引不可用）")
        return 0

    # --- 落盘：新页 ---
    page = build_source_page(args, body, new_page_links, indexed)
    path = write_page(SOURCE_DIR, args.title, page)
    rel_new = str(path.relative_to(VAULT))
    append_log(args.agent, "source", path, args.title)
    append_index("source", args.title, path)
    print(f"[ok] 写入 source 页：{rel_new}")

    # --- 双向：往旧页回写反向链接 ---
    if related:
        back_link = f"[[{path.stem}|{args.title}]]（相关来源·ingest）"
        mutated = 0
        for rel, sc in related[: args.max_mutate]:
            if add_related_link(VAULT / rel, back_link):
                mutated += 1
                print(f"[xref] {sc:.3f}  双向 ←→ {rel}")
        print(f"[ok] 交叉引用：新页连 {len(auto_links)} 篇，回写旧页 {mutated} 篇")
    else:
        print(f"[info] 无交叉引用（{'索引不可用' if not indexed else '无旧页超阈值'}）")

    # --- 重建索引 ---
    if not args.no_index and indexed:
        reindex(args.verbose)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
