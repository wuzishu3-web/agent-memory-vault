---
name: agent-memory-vault
description: Persistent shared long-term memory + knowledge ingest for AI coding agents over a Markdown/Obsidian vault. Use when you need cross-session memory, want to remember decisions/projects/preferences, ingest an external source (article/video/repo/transcript) into durable notes with automatic cross-references, or search what was learned before. Triggers on "remember this", "save to memory", "what did we decide about X", "ingest this source", "boot memory", or starting work that should persist across sessions.
---

# agent-memory-vault

A shared long-term memory system over a Markdown vault at `$AGENT_MEMORY_VAULT`
(default `~/agent-memory-vault`). All scripts live in this skill's `scripts/`.

## When to use

- **Starting meaningful work** → boot, so you're oriented by prior context.
- **A durable fact emerged** (decision / project progress / user preference / verified conclusion) → write it.
- **You processed an external source** (read an article, analyzed a video/repo, fetched a page) worth keeping → ingest it.
- **You need prior context** ("did we solve this before?", "what did we decide about X?") → query.
- **Ending a session with state changes** → update the hot cache.

## Core commands

Boot at the start of substantive work:
```bash
python3 scripts/agent_memory_boot.py --agent <agent-name> --task "<one line>"
```
Read what it prints (hot cache + recent log + task hits) before acting. Current user instructions always override stored memory.

Write a durable note (see `_system/WRITE_GUIDE.md` for type → location):
```bash
python3 scripts/agent_memory.py --type <decision|knowledge|project|playbook|...> \
  --agent <agent-name> --title "<topic phrase, no date>" --summary "<one line>" --body "<...>"
```

Ingest an external source — **do NOT hand-write a `source` note; use this**:
```bash
python3 scripts/ingest.py --agent <agent-name> \
  --title "<topic>" --summary "<YOUR high-quality summary>" \
  --body-file <notes.md> --url "<source url>" --source-type <article|video|repo|transcript>
```
It writes the page, propagates bidirectional `[[cross-references]]`, runs a quality
gate (sub-par input is quarantined to the inbox, never the source folder), and
rebuilds indexes. **The summary must be your own in-context analysis — the script
only does bookkeeping. Never let it auto-summarize.**

Search:
```bash
python3 scripts/query_vault.py "<question>"
```

End-of-session, if state changed:
```bash
python3 scripts/update_hot.py --agent <agent-name> --last "<what you did>" \
  --active "<remaining>" --pending "<blocked on>" --dont "<pitfall to avoid>"
```

## Rules

1. Long-term value only — ephemeral chatter does not belong in the vault.
2. Never write secrets (API keys, tokens, passwords, cookies) or private/raw chat logs.
3. Uncertain facts get `status: 待核验` and a note of what needs verifying.
4. Don't hand-write `source` pages — route external sources through `ingest.py`.
5. `WRITE_GUIDE.md` is the single authority for what content goes where.

## Maintenance (optional, schedulable)

```bash
python3 scripts/vault_health_check.py     # lint: stale/overdue/orphan pages
python3 scripts/dream_cycle.py            # consolidation pass
python3 scripts/build_embeddings.py       # rebuild vector index (incremental)
python3 scripts/extract_relations.py      # rebuild relation graph
```
