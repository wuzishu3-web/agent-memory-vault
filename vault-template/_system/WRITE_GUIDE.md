---
type: system
title: WRITE GUIDE
applies_to: [claude-code, codex, hermes]
---

# WRITE GUIDE — what goes where

> The single authority every agent reads **before writing** to the vault.
> The reason a shared vault turns to mush is that *the same content gets filed
> in different places by different agents*. This pins down "what content → which
> type → how to name it" so there's no freelancing.

## Ask three questions before writing

1. **Will this still matter in a month?** No → don't write it (or drop it in `00_Inbox`, which gets swept).
2. **Is it a fact/decision/knowledge, or just "what I did today"?** The former goes to a structured folder; only the latter goes to `05_Daily`.
3. **Does a note on this topic already exist?** Yes → update it, don't create a duplicate. Search first (`query_vault.py`), then write.

## Content → type table (the standard)

| What you're recording | type | folder | example |
|---|---|---|---|
| Stable user preference / background / goal | `user` | `01_User` | "always deploy via the release script" |
| An agent's capabilities / config / known issues | `agent` | `02_Agents` | "Agent X main model = …; known issue Y" |
| Project progress | `project` | `03_Projects/<name>/` | "checkout redesign v3 shipped" |
| Reusable long-lived knowledge / research | `knowledge` | `04_Knowledge` | "comparison of 5 vector DBs" |
| **Why** a config/approach was chosen | `decision` | `06_Decisions` | "why we moved to model X" |
| Reusable step-by-step SOP | `playbook` | `07_Playbooks` | "how we cut a release" |
| Summary of an external article/video/repo | `source` | `08_Sources` | **via `ingest.py`, not by hand** |
| Progress snapshot of an interrupted long task | `resume` | `10_Resume` | "migration — paused at step 4" |
| What happened today (a meaningful recap) | `daily` | `05_Daily` | "baseline benchmark done" |
| Unsorted scratch | `inbox` | `00_Inbox` | drafts to be sorted later |

### decision vs knowledge vs daily (most commonly confused)
- **decision**: there's a *trade-off*. Records *why A over B*. One decision per note.
- **knowledge**: timeless fact / methodology. Someone else can reuse it directly.
- **daily**: time-bound "today's actions". Don't write decisions or knowledge as daily.

## ⭐ External sources always go through `ingest.py`

After processing any external source (read an article, analyzed a video/repo),
**do not** hand-write a `source` page. Use the ingest pipeline (all agents share it):

```bash
python3 scripts/ingest.py --agent <name> \
  --title "..." --summary "<your high-quality summary>" \
  --body-file <notes> --url "..." --source-type <article|video|repo|transcript>
```

It does three things plain note-writing doesn't:
1. **Cross-reference propagation** — auto-links the new page to the most similar existing notes, both ways.
2. **Quality gate** — sub-par input is quarantined to `00_Inbox` as `待核验`; it never reaches `08_Sources`.
3. **Incremental re-index** — embeddings + relations.

> The summary must be produced by **you (the agent)**, in-context. The script only
> does bookkeeping — never let it auto-summarize. That's what keeps half-baked
> pages out of the vault.

## Naming

- Scripts auto-prepend the date. **Don't put a date in the title.**
- Title = a topic phrase, ≤ ~30 chars, usable as a wikilink anchor.
- Project notes must pass `--project <subfolder>` so they land in `03_Projects/<name>/`.

## Frontmatter schema (scripts emit this; match it if writing by hand)

```yaml
---
type: <see table>
status: 已确认 | 待核验      # confirmed | needs-verification
source_agent: claude-code | codex | hermes
created: YYYY-MM-DD HH:MM:SS
confidence: low | medium | high
tags: [domain, project]
---
```

- Uncertain content → `status: 待核验` and state what needs verifying.
- Secrets: only ever note "configured / has a key" — **never** write the key/token/password/cookie or raw private logs.

## Who writes what (by domain, no turf wars)

| Agent | mainly writes |
|---|---|
| Claude Code | `decision` (architecture/quality), `knowledge` (research), `playbook`; external sources → `ingest.py` |
| Codex | `decision` (ops/config), `resume` (long-task snapshots), `project`; external sources → `ingest.py` |
| Hermes | `daily` (meaningful recaps), `knowledge`; **primary for external-source ingest** (batch/scheduled) |

> Cross-domain: whoever did the work writes it, then leaves a line in `hot.md`.
> Adjust this table to your own agent roster.

## End-of-session: two things (whenever state changed)

1. `agent_memory.py` (or `ingest.py`) — write the durable memory.
2. `update_hot.py` — overwrite the hot cache.
