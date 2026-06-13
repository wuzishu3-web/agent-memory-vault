# Agent Memory Protocol

Applies to every AI agent that shares this vault (e.g. Claude Code, Codex, Hermes).

> **Read before writing** → [[WRITE_GUIDE]]. This protocol defines the framework;
> WRITE_GUIDE defines "what content goes where".

## Goal

Let multiple agents use one local Markdown vault as a readable, queryable,
maintainable long-term knowledge base. This is not fine-tuning and not a
replacement for each agent's internal memory — it's the shared **fact layer,
project layer, and workflow layer**.

## Boot (run before substantive work)

```bash
python3 scripts/agent_memory_boot.py --agent <agent-name> --task "<one line>"
```

Then read the files it lists. The hot cache and the last 20 log lines are printed
inline so you start oriented.

## Write rules

1. Long-term value only.
2. Stable user preferences → `01_User/profile.md`.
3. Agent config / model / known issues → the matching `02_Agents/*.md`.
4. Project progress → `03_Projects/<name>/`.
5. Every meaningful change → a `06_Decisions/` note.
6. Uncertain content → `status: 待核验`.
7. Secrets: only "configured / has a key", never the secret itself.

## External knowledge ingest (shared pipeline)

Any external source (article / web page / video / repo / transcript) goes through
the ingest pipeline, not hand-writing:

```bash
python3 scripts/ingest.py --agent <name> \
  --title "..." --summary "<in-context high-quality summary>" \
  --body-file <notes> --url "..." --source-type <article|video|repo|transcript>
```

Beyond writing the page it does **bidirectional cross-reference propagation +
quality-gate quarantine + incremental re-index** (see [[WRITE_GUIDE]] ⭐).
Principle: **understanding by the agent, bookkeeping by the script.** Sub-par
sources are auto-quarantined to `00_Inbox/待核验` and never enter `08_Sources`.

## Read rules

Before answering, check, in order:
1. The project folder for this task.
2. `01_User/profile.md`.
3. Your own agent profile.
4. Recent relevant notes in `06_Decisions/`.

If stored info conflicts with current reality, **trust current verification** and
record the difference as a new decision or a `待核验` note.

## Collaboration (no central orchestrator by default)

- Define each agent's domain in `02_Agents/` (e.g. architecture / ops / long-running).
- `active_orchestrator` defaults to `none`: the user dispatches, or agents self-route by domain.
- After a meaningful change, leave a line in `_system/hot.md`.

When agents discuss: pick a facilitator → state the question + constraints →
others give input → facilitator merges → output one actionable result, don't debate forever.

## File naming

```
YYYY-MM-DD-topic.md      # scripts add the date automatically
```
