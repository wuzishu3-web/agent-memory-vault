#!/usr/bin/env python3
"""
query_vault.py — vault 混合检索（vector + keyword + RRF 融合）

使用：
  python3 query_vault.py "Alice 的老板是谁"
  python3 query_vault.py "Project-X 第一单" --top 10
  python3 query_vault.py "GBrain" --vector-only
  python3 query_vault.py "the user" --keyword-only

输出：
  排序后的 chunk 列表，含来源、得分、片段。

设计：
  - vector：cosine 相似度（暴力计算，136 文件 * 4 chunk 平均，毫秒级）
  - keyword：朴素子串匹配 + 词频得分
  - RRF 融合：score = sum(1/(k+rank)) for each retrieval method
  - relations.json 一跳查询：如果命中 subject == query，直接返回相关关系

零外部依赖（仅标准库）。
"""

from __future__ import annotations

import os

import argparse
import json
import math
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))
DEFAULT_LM_STUDIO_URL = "http://127.0.0.1:1234/v1/embeddings"
DEFAULT_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"


# ============================== 工具 ==============================

def cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def embed_query(text: str, url: str, model: str) -> list[float]:
    payload = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["data"][0]["embedding"]


# ============================== 检索 ==============================

@dataclass
class Hit:
    chunk_id: str
    source: str
    text: str
    vec_score: float = 0.0
    kw_score: float = 0.0
    vec_rank: int = -1   # -1 表示未在向量结果里
    kw_rank: int = -1
    rrf_score: float = 0.0


def vector_search(query: str, chunks: list[dict], url: str, model: str, top_k: int) -> list[Hit]:
    qvec = embed_query(query, url, model)
    scored = []
    for c in chunks:
        s = cosine(qvec, c["embedding"])
        scored.append(Hit(
            chunk_id=c["chunk_id"],
            source=c["source"],
            text=c["text"],
            vec_score=s,
        ))
    scored.sort(key=lambda h: -h.vec_score)
    for i, h in enumerate(scored[:top_k]):
        h.vec_rank = i
    return scored[:top_k]


def keyword_search(query: str, chunks: list[dict], top_k: int) -> list[Hit]:
    """简单子串匹配 + 词频得分。中英混合 query 拆词。"""
    # 拆词：连续中文当一段，连续英文/数字当一段
    tokens = [t for t in re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z0-9_]+", query) if len(t) >= 1]
    if not tokens:
        return []
    # 中文去掉单字以减噪
    tokens = [t for t in tokens if len(t) >= 2 or t.isascii()]
    if not tokens:
        return []

    scored = []
    for c in chunks:
        text_lower = c["text"].lower()
        score = 0.0
        for t in tokens:
            tl = t.lower()
            count = text_lower.count(tl)
            if count > 0:
                # 子串得分：log(1+count) * 词长权重
                score += math.log(1 + count) * (1.0 + 0.3 * len(t))
        if score > 0:
            scored.append(Hit(
                chunk_id=c["chunk_id"],
                source=c["source"],
                text=c["text"],
                kw_score=score,
            ))
    scored.sort(key=lambda h: -h.kw_score)
    for i, h in enumerate(scored[:top_k]):
        h.kw_rank = i
    return scored[:top_k]


def rrf_fuse(vec_hits: list[Hit], kw_hits: list[Hit], k: int = 60) -> list[Hit]:
    """RRF 融合：每个文档最终得分 = sum(1/(k+rank_i))。"""
    by_id: dict[str, Hit] = {}
    for h in vec_hits:
        by_id[h.chunk_id] = h
    for h in kw_hits:
        if h.chunk_id in by_id:
            existing = by_id[h.chunk_id]
            existing.kw_score = h.kw_score
            existing.kw_rank = h.kw_rank
        else:
            by_id[h.chunk_id] = h

    for h in by_id.values():
        score = 0.0
        if h.vec_rank >= 0:
            score += 1.0 / (k + h.vec_rank + 1)
        if h.kw_rank >= 0:
            score += 1.0 / (k + h.kw_rank + 1)
        h.rrf_score = score

    fused = list(by_id.values())
    fused.sort(key=lambda h: -h.rrf_score)
    return fused


# ============================== 一跳关系查询 ==============================

def relation_lookup(query: str, vault: Path) -> list[dict]:
    """如果 query 是已知实体，从 relations.json 返回相关关系。"""
    rel_path = vault / "_system" / "relations.json"
    if not rel_path.exists():
        return []
    try:
        data = json.loads(rel_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    q = query.strip()
    matches = []
    for r in data.get("relations", []):
        if q == r.get("subject") or q == r.get("object") or q in r.get("subject", "") or q in r.get("object", ""):
            matches.append(r)
    return matches


# ============================== 主流程 ==============================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="vault 混合检索")
    ap.add_argument("query", help="检索 query")
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--url", type=str, default=DEFAULT_LM_STUDIO_URL)
    ap.add_argument("--model", type=str, default=DEFAULT_EMBED_MODEL)
    ap.add_argument("--vector-only", action="store_true")
    ap.add_argument("--keyword-only", action="store_true")
    ap.add_argument("--no-relations", action="store_true", help="不查 relations.json")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    vault: Path = args.vault.resolve()

    # 加载 embeddings
    emb_path = vault / "_system" / "embeddings.json"
    if not emb_path.exists():
        print("[fatal] embeddings.json 不存在，先跑 build_embeddings.py", file=sys.stderr)
        return 2
    emb_data = json.loads(emb_path.read_text(encoding="utf-8"))
    chunks = emb_data["chunks"]

    # 1. 关系一跳
    rel_hits: list[dict] = []
    if not args.no_relations:
        rel_hits = relation_lookup(args.query, vault)

    # 2. 检索
    vec_hits: list[Hit] = []
    kw_hits: list[Hit] = []
    pool = max(args.top * 3, 20)
    if not args.keyword_only:
        vec_hits = vector_search(args.query, chunks, args.url, args.model, pool)
    if not args.vector_only:
        kw_hits = keyword_search(args.query, chunks, pool)

    # 3. 融合
    if args.vector_only:
        fused = vec_hits
    elif args.keyword_only:
        fused = kw_hits
    else:
        fused = rrf_fuse(vec_hits, kw_hits)

    fused = fused[: args.top]

    # 4. 输出
    if args.json:
        result = {
            "query": args.query,
            "relations": rel_hits,
            "hits": [
                {
                    "chunk_id": h.chunk_id,
                    "source": h.source,
                    "text": h.text,
                    "rrf_score": round(h.rrf_score, 4),
                    "vec_score": round(h.vec_score, 4),
                    "kw_score": round(h.kw_score, 4),
                    "vec_rank": h.vec_rank,
                    "kw_rank": h.kw_rank,
                }
                for h in fused
            ],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # 文本输出
    print(f"=== query: {args.query} ===")
    if rel_hits:
        print(f"\n📎 关系图谱命中 ({len(rel_hits)} 条):")
        for r in rel_hits:
            print(f"  [{r.get('confidence', '?')[:3]}] {r['subject']} -{r['predicate']}-> {r['object']}")
            print(f"      src: {r['source']}")

    print(f"\n📚 检索结果 (top {len(fused)}):\n")
    for i, h in enumerate(fused, 1):
        snippet = h.text.replace("\n", " ")[:200]
        method = []
        if h.vec_rank >= 0:
            method.append(f"vec#{h.vec_rank+1}({h.vec_score:.3f})")
        if h.kw_rank >= 0:
            method.append(f"kw#{h.kw_rank+1}({h.kw_score:.2f})")
        print(f"{i}. [{h.source}] rrf={h.rrf_score:.4f}  {' + '.join(method)}")
        print(f"   {snippet}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
