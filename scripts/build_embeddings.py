#!/usr/bin/env python3
"""
build_embeddings.py — 为 vault 笔记构建嵌入索引

设计：
  - 嵌入源：LM Studio embedding endpoint (text-embedding-nomic-embed-text-v1.5, 768 维)
  - 存储：单 JSON 文件 `_system/embeddings.json`
  - 增量：基于 mtime + 内容哈希判断是否需要重新嵌入
  - 切片：每篇笔记按段落（双换行）切，每段 ≤ 800 字符，重叠 100 字符

零外部依赖（仅标准库）。
"""

from __future__ import annotations

import os

import argparse
import hashlib
import json
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# ============================== 配置 ==============================

DEFAULT_VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))
DEFAULT_OUTPUT = "_system/embeddings.json"
DEFAULT_LM_STUDIO_URL = "http://127.0.0.1:1234/v1/embeddings"
DEFAULT_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"

EXCLUDE_DIRS = {"_system", "00_Inbox", "09_Archive"}
CHUNK_SIZE = 800   # 字符
CHUNK_OVERLAP = 100


# ============================== 数据 ==============================

@dataclass
class Chunk:
    chunk_id: str       # f"{rel_path}#{idx}"
    source: str         # 相对 vault 路径
    chunk_idx: int
    text: str
    text_hash: str      # SHA1 of text，增量判断用
    embedding: list[float]


# ============================== 切片 ==============================

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """段落优先切片：先按双换行切，长段再按 size 滑动切。"""
    if not text.strip():
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for p in paragraphs:
        if not buffer:
            buffer = p
        elif len(buffer) + len(p) + 2 <= size:
            buffer = buffer + "\n\n" + p
        else:
            chunks.append(buffer)
            buffer = p
    if buffer:
        chunks.append(buffer)

    # 处理超长段：滑动窗口
    final: list[str] = []
    for c in chunks:
        if len(c) <= size:
            final.append(c)
        else:
            i = 0
            while i < len(c):
                final.append(c[i:i + size])
                i += size - overlap
    return final


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ============================== 嵌入 API ==============================

def embed_text(text: str, url: str, model: str, timeout: float = 30.0) -> list[float]:
    """单次调用 LM Studio embedding endpoint。"""
    payload = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["data"][0]["embedding"]


def embed_batch(texts: list[str], url: str, model: str, timeout: float = 60.0) -> list[list[float]]:
    """批量嵌入。LM Studio 可能不支持，做单条 fallback。"""
    if not texts:
        return []
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [item["embedding"] for item in data["data"]]
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        # 单条 fallback
        print(f"[warn] batch failed ({e}), 改单条…", file=sys.stderr)
        return [embed_text(t, url, model, timeout) for t in texts]


# ============================== 主流程 ==============================

def iter_md_files(vault: Path) -> Iterable[Path]:
    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        yield md


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="vault 嵌入索引构建")
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--output", type=str, default=None)
    ap.add_argument("--url", type=str, default=DEFAULT_LM_STUDIO_URL)
    ap.add_argument("--model", type=str, default=DEFAULT_EMBED_MODEL)
    ap.add_argument("--rebuild", action="store_true", help="强制重建（不增量）")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--limit-files", type=int, default=0)
    args = ap.parse_args(argv)

    vault: Path = args.vault.resolve()
    if not vault.is_dir():
        print(f"[fatal] vault 不存在: {vault}", file=sys.stderr)
        return 2

    out_path = Path(args.output) if args.output else (vault / DEFAULT_OUTPUT)

    # 加载已有索引（增量用）
    existing: dict[str, Chunk] = {}  # text_hash → Chunk
    if not args.rebuild and out_path.exists():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            for c in data.get("chunks", []):
                existing[c["text_hash"]] = Chunk(**c)
            if args.verbose:
                print(f"[info] 加载已有索引: {len(existing)} 个 chunk", file=sys.stderr)
        except Exception as e:
            print(f"[warn] 加载已有索引失败: {e}", file=sys.stderr)

    files = list(iter_md_files(vault))
    if args.limit_files:
        files = files[: args.limit_files]

    all_chunks: list[Chunk] = []
    files_scanned = 0
    chunks_new = 0
    chunks_reused = 0

    for md in files:
        try:
            text = md.read_text(encoding="utf-8")
        except Exception as e:
            if args.verbose:
                print(f"[warn] {md}: {e}", file=sys.stderr)
            continue
        files_scanned += 1
        rel = str(md.relative_to(vault))

        chunks = chunk_text(text)
        # 准备需要新嵌入的 chunk
        pending: list[tuple[int, str, str]] = []  # (idx, text, hash)
        for idx, ctext in enumerate(chunks):
            h = text_hash(ctext)
            if h in existing:
                # 复用
                old = existing[h]
                all_chunks.append(Chunk(
                    chunk_id=f"{rel}#{idx}",
                    source=rel,
                    chunk_idx=idx,
                    text=ctext,
                    text_hash=h,
                    embedding=old.embedding,
                ))
                chunks_reused += 1
            else:
                pending.append((idx, ctext, h))

        # 批量嵌入新 chunk
        if pending:
            texts = [t for _, t, _ in pending]
            t0 = time.time()
            embs = embed_batch(texts, args.url, args.model)
            dt = time.time() - t0
            if args.verbose:
                print(f"[embed] {rel}: +{len(pending)} chunks in {dt:.2f}s", file=sys.stderr)
            for (idx, ctext, h), emb in zip(pending, embs):
                all_chunks.append(Chunk(
                    chunk_id=f"{rel}#{idx}",
                    source=rel,
                    chunk_idx=idx,
                    text=ctext,
                    text_hash=h,
                    embedding=emb,
                ))
                chunks_new += 1

    # 排序
    all_chunks.sort(key=lambda c: (c.source, c.chunk_idx))

    # 取嵌入维度
    dim = len(all_chunks[0].embedding) if all_chunks else 0

    payload = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "url": args.url,
        "dim": dim,
        "stats": {
            "files_scanned": files_scanned,
            "chunks_total": len(all_chunks),
            "chunks_new": chunks_new,
            "chunks_reused": chunks_reused,
        },
        "chunks": [asdict(c) for c in all_chunks],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] 写入 {out_path}", file=sys.stderr)
    print(f"[ok] 文件 {files_scanned}, chunks 总 {len(all_chunks)} "
          f"(new={chunks_new}, reused={chunks_reused}, dim={dim})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
