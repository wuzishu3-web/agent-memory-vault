#!/usr/bin/env python3
"""
extract_relations.py — vault 知识图谱关系抽取器（v2）

灵感：Garry Tan / GBrain（github.com/garrytan/gbrain）—— 纯正则 + 模式匹配，零 LLM 成本。

设计原则（精度优先，召回次之）：
  1. frontmatter relations 数组 → 最高优先（high）
  2. 结构化字段（**字段**：[[X]] / **字段**：X）→ high
  3. wikilink 两端 + 严格关系动词 → high
  4. 单端 wikilink + 严格关系动词 → medium
  5. 自由文本严格关系动词 + 实体白名单两端 → medium
  6. 严格关系动词 + 实体白名单单端 → low
  7. 不确定一律跳过

实体白名单：vault 笔记标题 + 文件名（去日期前缀）+ frontmatter aliases
"""

from __future__ import annotations

import os

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore
except ImportError:
    print("[fatal] pyyaml 未安装。请 pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ============================== 常量 / 配置 ==============================

DEFAULT_VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", os.path.expanduser("~/agent-memory-vault")))
DEFAULT_OUTPUT = "_system/relations.json"

SCAN_DIRS = [
    "01_the user",
    "02_Agents",
    "02_People",
    "03_Projects",
    "04_Knowledge",
    "05_Daily",
    "06_Decisions",
    "07_Playbooks",
    "08_Sources",
    "10_Resume",
    "index.md",
    "AGENTS.md",
    "Agent系统状态.md",
]

EXCLUDE_DIRS = {"_system", "00_Inbox", "09_Archive"}

# 实体合法性约束
ENTITY_BAD_CHARS = set("，。！？；：""''（）《》[]{}<>|→←⬅️➡️│├└─\n\r\t")
ENTITY_MIN_LEN = 2
ENTITY_MAX_LEN = 30

# 文件名日期前缀去除（YYYY-MM-DD-…）
FILENAME_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")

# 关系动词关键词（用作"硬触发"，确保不会被字符子串误命中）
PREDICATE_KEYWORDS: dict[str, list[str]] = {
    "works_at": ["任职于", "就职于", "隶属于"],
    "reports_to": ["向 X 汇报", "汇报给", "嫡系上级", "直接上级"],
    "manages": ["管理", "下辖", "旗下", "负责管理"],
    "partners_with": ["合作伙伴", "合作方", "战略合作", "联合"],
    "founded": ["创办", "创立", "创始人", "成立"],
    "invested_in": ["投资", "投了", "领投", "跟投"],
    "advises": ["顾问", "咨询"],
    "located_in": ["位于", "总部在", "总部位于", "坐落于"],
}


# ============================== 数据结构 ==============================

@dataclass
class Relation:
    subject: str
    predicate: str
    object: str
    confidence: str  # high / medium / low
    source: str  # 相对 vault 路径
    evidence: str  # 截取的证据片段（≤ 80 字）

    def key(self) -> tuple:
        return (self.subject, self.predicate, self.object)


# ============================== 实体白名单 ==============================

def normalize_entity(s: str) -> str:
    """实体规范化：去前后空白、去 markdown 加粗符号、去括号内补充、去尾随标点。"""
    if s is None:
        return ""
    s = s.strip()
    # 去 markdown bold/italic 符号
    s = re.sub(r"^[\*_]+", "", s)
    s = re.sub(r"[\*_]+$", "", s)
    # 去括号内补充：“Acme Inc（Example Holdings，0000.HK）” → “Acme Inc”
    s = re.sub(r"[（(].*?[）)]", "", s)
    s = re.sub(r"[（(].*$", "", s)  # 未闭合括号也截断
    # 去尾随标点 / 空格
    s = s.rstrip("，。！？；：、,. \t")
    s = s.strip()
    return s


def is_valid_entity(s: str) -> bool:
    """实体合法性校验：长度、字符、不能纯数字、不能日期片段、不能含 markdown 树形符号。"""
    if not s:
        return False
    s = s.strip()
    if len(s) < ENTITY_MIN_LEN or len(s) > ENTITY_MAX_LEN:
        return False
    # 含禁用字符
    for ch in ENTITY_BAD_CHARS:
        if ch in s:
            return False
    # 纯数字
    if s.isdigit():
        return False
    # 日期片段（YYYY-MM-DD）
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", s):
        return False
    # 时间戳样
    if re.match(r"^\d{8,}$", s):
        return False
    # 文件名碎片特征
    if "首次实战" in s or "测试报告" in s:
        return False
    # 含 markdown 链接 / URL / 代码符号
    if any(ch in s for ch in "()<>{}\\/`"):
        return False
    # 单字符基本不收
    return True


def collect_entity_whitelist(vault: Path) -> set[str]:
    """从 vault 笔记标题、文件名、frontmatter aliases 收集实体白名单。"""
    whitelist: set[str] = set()

    # 内置已知实体（核心人物 / 公司 / 团队）
    builtin = [
        # 通用示例种子实体（用户可按需替换/扩充为自己领域的人名、公司、团队）。
        # 这里只放几个占位，避免把任何真实私人信息硬编码进开源版本。
        "Alice", "Bob", "Acme Inc", "Acme Labs", "Engineering",
        # 常见 AI agent / 工具名（公开），便于多 agent 协作笔记里识别。
        "Claude Code", "claude-code", "Codex", "codex", "Hermes", "hermes",
        "OpenAI", "Anthropic",
    ]
    whitelist.update(builtin)

    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault)
        # 跳过排除目录
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue

        # 不再把文件名加入白名单，避免"Hermes-Acme-competitor-research-round2"这种碎片污染

        # 标题 + frontmatter aliases
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _ = split_frontmatter(text)
        if fm:
            aliases = fm.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            for a in aliases:
                a = normalize_entity(str(a))
                if is_valid_entity(a):
                    whitelist.add(a)

        # H1 标题
        m = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
        if m:
            t = normalize_entity(m.group(1))
            # 标题里去除括号内容（如"Engineering Dept（owned by the user）" → "Engineering"）
            t_clean = re.sub(r"[（(].*?[）)]", "", t).strip()
            if is_valid_entity(t_clean):
                whitelist.add(t_clean)

    return whitelist


# ============================== frontmatter 解析 ==============================

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    try:
        fm = yaml.safe_load(raw) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
    return fm, body


# ============================== 抽取器 ==============================

WIKILINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]+)?\]\]")


def make_evidence(text: str, span: tuple[int, int], window: int = 30) -> str:
    a, b = span
    start = max(0, a - window)
    end = min(len(text), b + window)
    snippet = text[start:end].replace("\n", " ").strip()
    return snippet[:120]


def extract_from_frontmatter_relations(fm: dict, source: str) -> list[Relation]:
    """frontmatter 里有 `relations: [{subject, predicate, object}]`。"""
    rels = []
    raw = fm.get("relations")
    if not isinstance(raw, list):
        return rels
    for item in raw:
        if not isinstance(item, dict):
            continue
        subj = normalize_entity(str(item.get("subject", "")))
        pred = str(item.get("predicate", "")).strip()
        obj = normalize_entity(str(item.get("object", "")))
        if not (subj and pred and obj):
            continue
        if not (is_valid_entity(subj) and is_valid_entity(obj)):
            continue
        if pred not in PREDICATE_KEYWORDS:
            continue
        rels.append(Relation(subj, pred, obj, "high", source, "frontmatter.relations"))
    return rels


# 结构化字段：- **嫡系上级**：**Alice** / - **嫡系上级**：[[Alice]]
# 字段名 → predicate
STRUCTURED_FIELD_MAP = {
    "嫡系上级": "reports_to",
    "直接上级": "reports_to",
    "上级": "reports_to",
    "汇报对象": "reports_to",
    "老板": "reports_to",
    "公司": "works_at",
    "雇主": "works_at",
    "任职单位": "works_at",
    "所在单位": "works_at",
    "总部": "located_in",
    "总部所在地": "located_in",
    "注册地": "located_in",
    "成立地": "located_in",
    "地点": "located_in",
    "创始人": "founded_by",  # 反向：obj founded_by subj
    "创办人": "founded_by",
    "投资人": "invested_by",  # 反向
    "资方": "invested_by",
    "顾问": "advised_by",  # 反向
    "合作方": "partners_with",
    "合作伙伴": "partners_with",
}

STRUCTURED_FIELD_RE = re.compile(
    r"^\s*[-*]?\s*\*\*(?P<field>[^*\n]+?)\*\*\s*[:：]\s*(?P<rest>.+?)\s*$",
    re.MULTILINE,
)

# 表格行：| **嫡系上级** | Alice |
TABLE_FIELD_RE = re.compile(
    r"^\s*\|\s*\*\*(?P<field>[^*\n|]+?)\*\*\s*\|\s*(?P<rest>[^|\n]+?)\s*\|",
    re.MULTILINE,
)


def extract_from_structured_fields(body: str, doc_subject: str | None, source: str, whitelist: set[str] | None = None) -> list[Relation]:
    """结构化字段：'- **嫡系上级**：**Alice**' 和表格行 '| **嫡系上级** | Alice |'。"""
    rels = []
    for regex in (STRUCTURED_FIELD_RE, TABLE_FIELD_RE):
        for m in regex.finditer(body):
            field_name = normalize_entity(m.group("field"))
            rest = m.group("rest").strip()
            pred_info = STRUCTURED_FIELD_MAP.get(field_name)
            if not pred_info:
                continue

            # rest 里取第一个实体：bold > 纯实体 wikilink（不含 /） > 首句
            target = None
            bm = re.match(r"\*\*([^*\n]+?)\*\*", rest)
            if bm:
                target = normalize_entity(bm.group(1))
            if not target or not is_valid_entity(target):
                wm = WIKILINK_RE.search(rest)
                if wm:
                    wk_text = wm.group(1).strip()
                    if "/" not in wk_text:
                        target = normalize_entity(wk_text)
            if not target or not is_valid_entity(target):
                first = re.split(r"[，。、（()【\[]", rest, maxsplit=1)[0]
                target = normalize_entity(first)

            if not target or not is_valid_entity(target):
                continue
            if not doc_subject or not is_valid_entity(doc_subject):
                continue

            # 严格：doc_subject 必须在白名单里才信任（避免文件名碎片当主体）
            if whitelist is not None and doc_subject not in whitelist:
                continue

            # 过滤 placeholder
            if any(w in target for w in ("暂无", "未查", "待查", "未知", "不详", "不明", "TBD", "TODO")):
                continue

            # 反向关系映射
            if pred_info == "founded_by":
                rels.append(Relation(target, "founded", doc_subject, "high", source,
                                     f"**{field_name}**：{target}"))
            elif pred_info == "invested_by":
                rels.append(Relation(target, "invested_in", doc_subject, "high", source,
                                     f"**{field_name}**：{target}"))
            elif pred_info == "advised_by":
                rels.append(Relation(target, "advises", doc_subject, "high", source,
                                     f"**{field_name}**：{target}"))
            else:
                rels.append(Relation(doc_subject, pred_info, target, "high", source,
                                     f"**{field_name}**：{target}"))
    return rels


def infer_doc_subject(fm: dict, body: str, fallback_filename: str) -> str | None:
    """推断这篇文档的“主体实体”。

    优先级：frontmatter title > frontmatter aliases[0] > H1 标题 > 文件名（去日期）
    后续会做“标题归一化”把“the user 画像” / “the user 工作背景” 这类压缩为“the user”。
    """
    candidates: list[str] = []
    if isinstance(fm.get("subject"), str):
        candidates.append(fm["subject"])
    aliases = fm.get("aliases") or []
    if isinstance(aliases, list) and aliases:
        candidates.append(str(aliases[0]))
    m = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if m:
        candidates.append(m.group(1))
    candidates.append(FILENAME_DATE_PREFIX.sub("", fallback_filename))

    for raw in candidates:
        s = normalize_entity(raw)
        # 去括号
        s = re.sub(r"[（(].*?[）)]", "", s).strip()
        # “标题归一化”：说明型后缀还原为主体
        for suffix in ("画像", "工作背景", "背景", "个人档案", "档案", "总览", "介绍"):
            if s.endswith(suffix) and len(s) > len(suffix) + 1:
                base = s[: -len(suffix)].strip()
                if is_valid_entity(base):
                    s = base
                    break
        if is_valid_entity(s):
            return s
    return None


# ============================== 严格文本模式 ==============================

# 用 (?P<subj>...) (?P<obj>...) 捕获，subj/obj 不允许跨标点和空白
ENTITY_PATTERN_INNER = r"[\u4e00-\u9fa5A-Za-z0-9·\-_\.]{2,30}"
WIKILINK_PATTERN = r"\[\[(?P<wk>[^\]\|]+?)(?:\|[^\]]+)?\]\]"


def ent_or_wiki_or_bold(name: str) -> str:
    """返回三选一的命名捕获模式：[[wk]] | **bold** | plain。

    name 用作捕获组名前缀（避免 .format 与 {2,30} 冲突）。
    bold 上限拉到 60 并允许包含括号内补充，捕获后会在 normalize_entity 去括号。
    """
    return (
        r"(?:\[\[(?P<" + name + r"_wk>[^\]\|]+?)(?:\|[^\]]+)?\]\]"
        r"|\*\*(?P<" + name + r"_b>[^*\n]{2,60}?)\*\*"
        r"|(?P<" + name + r"_p>" + ENTITY_PATTERN_INNER + r"))"
    )


def both_groups(m: re.Match, name: str) -> str | None:
    for suf in ("_wk", "_b", "_p"):
        v = m.groupdict().get(name + suf)
        if v:
            return normalize_entity(v)
    return None


# 文本规则集合：每条 (predicate, regex, role)
# role: "subj_obj"（subj→pred→obj）或 "obj_subj"（反向）

_S = ent_or_wiki_or_bold("s")
_O = ent_or_wiki_or_bold("o")


def _alt(name_pos1: str, name_pos2: str) -> tuple[str, str]:
    """为反向规则单独构造命名（避免重复组名）。"""
    return ent_or_wiki_or_bold(name_pos1), ent_or_wiki_or_bold(name_pos2)


PATTERNS = [
    # works_at: "X 任职于 Y" / "X 就职于 Y" / "X 隶属于 Y"
    ("works_at",
     re.compile(_S + r"\s*(?:任职于|就职于|隶属于)\s*" + _O),
     "subj_obj"),

    # works_at: "X 在 Y 任 ..."
    ("works_at",
     re.compile(_S + r"\s*(?:在|于)\s*" + _O + r"\s*任\s*\*{0,2}[\u4e00-\u9fa5A-Za-z]"),
     "subj_obj"),

    # works_at: "X 在 Y 工作"
    ("works_at",
     re.compile(_S + r"\s*(?:在|于)\s*" + _O + r"\s*工作"),
     "subj_obj"),

    # reports_to: "X 向 Y...汇报"
    ("reports_to",
     re.compile(_S + r"\s*向\s*" + _O + r"\s*(?:[（(][^）)]*[）)])?\s*汇报"),
     "subj_obj"),

    # reports_to: "X 汇报给 Y"
    ("reports_to",
     re.compile(_S + r"\s*汇报给\s*" + _O),
     "subj_obj"),

    # founded: "X 创办/创立/创建 Y"
    ("founded",
     re.compile(_S + r"\s*(?:创办|创立|创建)\s*(?:了)?\s*" + _O),
     "subj_obj"),

    # founded（反向）: "Y 由 X 创办/创立"
    ("founded",
     re.compile(_O + r"\s*由\s*" + _S + r"\s*(?:创办|创立|创建)"),
     "subj_obj"),

    # invested_in: "X 投资了 Y" / "X 投了 Y" / "X 领投 Y"
    ("invested_in",
     re.compile(_S + r"\s*(?:投资了|投了|领投)\s*" + _O),
     "subj_obj"),

    # located_in: "X 位于 Y" / "X 总部在 Y" / "X 总部位于 Y"
    ("located_in",
     re.compile(_S + r"\s*(?:位于|总部位于|总部在|坐落于)\s*" + _O),
     "subj_obj"),

    # partners_with: "X 与 Y 合作" / "X 和 Y 合作"
    ("partners_with",
     re.compile(_S + r"\s*(?:与|和)\s*" + _O + r"\s*合作"),
     "subj_obj"),

    # advises: "X 是/担任 Y 的顾问"
    ("advises",
     re.compile(_S + r"\s*(?:是|担任)\s*" + _O + r"\s*的?\s*顾问"),
     "subj_obj"),

    # manages: "X 管理/下辖/旗下 Y"
    ("manages",
     re.compile(_S + r"\s*(?:管理|下辖|旗下)\s*" + _O),
     "subj_obj"),

    # works_at（反向）: "X 是 Y 的负责人/员工/CEO/总裁"
    ("works_at",
     re.compile(
         _S + r"\s*是\s*" + _O + r"\s*的?\s*(?:负责人|员工|CEO|总裁|董事长|联席总裁|总经理)"
     ),
     "subj_obj"),

    # works_at: "X 加入 Y"（严要求：后面不能是营业/讨论等动词）
    ("works_at",
     re.compile(
         _S + r"\s*(?:\d{4}[-\s年]?\d{0,2}[-\s月]?\d{0,2}\s*)?\s*加入\s*" + _O + r"\s*[任担作是\s,，。\.]"
     ),
     "subj_obj"),

    # works_at: "X 担任/兼任 Y 联席总裁/总裁/CEO/负责人" —— Y 是公司
    ("works_at",
     re.compile(
         _S + r"\s*(?:担任|兼任)\s*" + _O + r"\s*(?:联席总裁|总裁|CEO|负责人|高级副总裁|副总裁)"
     ),
     "subj_obj"),

    # reports_to: "X 的嵡系上级是 Y" / "X 的上级是 Y"
    ("reports_to",
     re.compile(
         _S + r"\s*的\s*(?:嵡系)?\s*(?:上级|老板)\s*是\s*" + _O
     ),
     "subj_obj"),

    # manages reverse: "Y ⬅️ X 负责"
    ("manages",
     re.compile(_O + r"\s*(?:⬅️|<-|←)\s*" + _S + r"\s*负责"),
     "subj_obj"),
]

# manages 专用规则：树形结构。例子：
#   Engineering
#       ├─ Team-A
#       └─ Acme HK International Ltd
# 抽出：Engineering manages Team-A / Team-B...
TREE_BLOCK_RE = re.compile(r"^[ \t]*[├└][─────]\s*\*{0,2}([^\n\*]+?)\*{0,2}\s*[（(]?[^\n]*$", re.MULTILINE)


def text_extract(body: str, source: str, whitelist: set[str]) -> list[Relation]:
    rels = []
    for predicate, regex, _role in PATTERNS:
        for m in regex.finditer(body):
            subj = both_groups(m, "s")
            obj = both_groups(m, "o")
            if not subj or not obj:
                continue
            subj = normalize_entity(subj)
            obj = normalize_entity(obj)
            if not (is_valid_entity(subj) and is_valid_entity(obj)):
                continue
            if subj == obj:
                continue
            # 过滤 placeholder 字样
            placeholders = ("暂无", "未查", "待查", "未知", "不详", "不明")
            if any(p in subj for p in placeholders) or any(p in obj for p in placeholders):
                continue
            # 看是否 wikilink / bold（“强证据”）
            s_strong = bool(m.groupdict().get("s_wk") or m.groupdict().get("s_b"))
            o_strong = bool(m.groupdict().get("o_wk") or m.groupdict().get("o_b"))
            in_wl_s = subj in whitelist
            in_wl_o = obj in whitelist

            if s_strong and o_strong:
                conf = "high"
            elif (s_strong or o_strong) and (in_wl_s or in_wl_o):
                conf = "high"
            elif s_strong or o_strong:
                conf = "medium"
            elif in_wl_s and in_wl_o:
                conf = "medium"
            else:
                # 两端都不是 wikilink/bold 且不是双白名单命中 → 丢
                continue

            rels.append(Relation(
                subj, predicate, obj, conf, source,
                make_evidence(body, m.span(), 20),
            ))
    # 补一轮：树形结构抽 manages
    rels.extend(_extract_tree_manages(body, source, whitelist))
    return rels


def _extract_tree_manages(body: str, source: str, whitelist: set[str]) -> list[Relation]:
    """从代码块里的树形结构抽 manages 关系。

    例子：
        Engineering
            ├─ Team-A
            └─ Acme HK International Ltd
    抽出：Engineering manages Team-A / Team-B...
    """
    rels: list[Relation] = []
    # 逐行扫，记录上一个“可能是父节点”的行
    lines = body.split("\n")
    parent: str | None = None
    for line in lines:
        # 取去 code fence 以外的行：这里我们简单判断，树形符号字符足以锁定区域
        m = re.match(r"^[ \t]*[├└][─────]\s*\*{0,2}([^\n\*（(]+?)\*{0,2}\s*(?:[（(]|$)", line)
        if m:
            child_raw = m.group(1).strip()
            child = normalize_entity(child_raw)
            if parent and is_valid_entity(parent) and is_valid_entity(child):
                # parent / child 至少一端在白名单才保留
                if parent in whitelist or child in whitelist:
                    conf = "high" if (parent in whitelist and child in whitelist) else "medium"
                    rels.append(Relation(parent, "manages", child, conf, source,
                                         f"树形: {parent} ├─ {child}"))
            continue
        # 不含树形符号的“顿位”行作为候选父节点
        # 如果该行干净且是个实体，则设为 parent
        stripped = line.strip()
        # 去 markdown bold/list/code-fence 符号
        cleaned = re.sub(r"^[\-\*\s>]*", "", stripped)
        cleaned = re.sub(r"^\*\*([^*]+)\*\*$", r"\1", cleaned).strip()
        # 锁定为“业务/部门”型名词：必须在白名单里
        if cleaned and len(cleaned) <= 30 and cleaned in whitelist:
            parent = cleaned
        else:
            # 遇到空行 / 其他重点后 parent 失效
            if not stripped or stripped.startswith("#") or stripped.startswith("```"):
                parent = None
    return rels


# ============================== 主流程 ==============================

def iter_md_files(vault: Path) -> Iterable[Path]:
    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        yield md


def dedup(rels: list[Relation]) -> list[Relation]:
    seen: dict[tuple, Relation] = {}
    rank = {"high": 3, "medium": 2, "low": 1}
    for r in rels:
        k = r.key()
        if k not in seen or rank[r.confidence] > rank[seen[k].confidence]:
            seen[k] = r
    return list(seen.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="vault 关系图谱抽取（v2）")
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--output", type=str, default=None,
                    help="输出 JSON 路径，默认 <vault>/_system/relations.json")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--limit-files", type=int, default=0,
                    help="只处理前 N 个文件（调试）")
    args = ap.parse_args(argv)

    vault: Path = args.vault.resolve()
    if not vault.is_dir():
        print(f"[fatal] vault 目录不存在: {vault}", file=sys.stderr)
        return 2

    out_path = Path(args.output) if args.output else (vault / DEFAULT_OUTPUT)

    if args.verbose:
        print(f"[info] 收集实体白名单中…", file=sys.stderr)
    whitelist = collect_entity_whitelist(vault)
    if args.verbose:
        print(f"[info] 白名单实体数: {len(whitelist)}", file=sys.stderr)

    all_rels: list[Relation] = []
    files_scanned = 0
    files = list(iter_md_files(vault))
    if args.limit_files:
        files = files[: args.limit_files]

    for md in files:
        try:
            text = md.read_text(encoding="utf-8")
        except Exception as e:
            if args.verbose:
                print(f"[warn] 读取失败 {md}: {e}", file=sys.stderr)
            continue

        files_scanned += 1
        rel_path = str(md.relative_to(vault))
        fm, body = split_frontmatter(text)
        doc_subject = infer_doc_subject(fm, body, md.stem)

        # 1. frontmatter relations
        all_rels.extend(extract_from_frontmatter_relations(fm, rel_path))
        # 2. 结构化字段
        all_rels.extend(extract_from_structured_fields(body, doc_subject, rel_path, whitelist))
        # 3. 文本模式
        all_rels.extend(text_extract(body, rel_path, whitelist))

    deduped = dedup(all_rels)

    # 排序
    rank = {"high": 0, "medium": 1, "low": 2}
    deduped.sort(key=lambda r: (rank[r.confidence], r.predicate, r.subject, r.object))

    # 统计
    by_type: dict[str, int] = {}
    by_conf: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for r in deduped:
        by_type[r.predicate] = by_type.get(r.predicate, 0) + 1
        by_conf[r.confidence] += 1

    payload = {
        "version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "files_scanned": files_scanned,
            "whitelist_size": len(whitelist),
            "relations_extracted": len(deduped),
            "by_type": by_type,
            "by_confidence": by_conf,
        },
        "relations": [asdict(r) for r in deduped],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 写入 {out_path}", file=sys.stderr)
    print(f"[ok] 文件 {files_scanned} 个，关系 {len(deduped)} 条 "
          f"(high={by_conf['high']} medium={by_conf['medium']} low={by_conf['low']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
