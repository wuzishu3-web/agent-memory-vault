# Agent Memory Operating Rules

All agents working in this vault treat it as the shared long-term knowledge base.

Before important work, check:
1. `index.md`
2. `_system/AGENT_MEMORY_PROTOCOL.md`
3. the relevant file under `02_Agents/`
4. project files under `03_Projects/`
5. decisions under `06_Decisions/`

Write only durable information: user preferences, project decisions, agent config
changes, tested workflows, reusable lessons, verified source summaries.

Do NOT write: secrets / API keys / passwords / cookies / tokens; unverified claims
without a `待核验` marker; raw logs unless summarized; huge pasted content (that
belongs in a `source` reference via `ingest.py`).

Ingesting an external source (article / page / video / repo / transcript): do not
hand-write a `source` note — use the shared pipeline, which also propagates
bidirectional cross-references, quarantines low-quality input, and rebuilds indexes:

```bash
python3 scripts/ingest.py --agent <name> \
  --title "..." --summary "<your in-context summary>" --body-file <notes> \
  --url "..." --source-type <article|video|repo|transcript>
```

See `_system/WRITE_GUIDE.md` for the full filing standard.
