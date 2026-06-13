# scripts/

| script | purpose |
|---|---|
| `init_vault.py` | scaffold a fresh vault from `../vault-template/` |
| `agent_memory_boot.py` | print boot context (hot cache + log + task hits) |
| `agent_memory.py` | write a typed note (+ log + index) |
| `update_hot.py` | overwrite the hot cache |
| `ingest.py` | ingest an external source → page + cross-refs + quality gate + reindex |
| `ingest_stop_hook.py` | Claude Code Stop hook: auto-ingest gatekeeper |
| `build_embeddings.py` | incremental vector index |
| `extract_relations.py` | regex relation graph (needs `pyyaml`) |
| `query_vault.py` | hybrid vector + keyword + relations search |
| `vault_health_check.py` | lint: stale / overdue / orphan pages |
| `dream_cycle.py` | consolidation pass |
| `stop_hook_beat.py` | Claude Code Stop hook: session beats |

All read the vault path from `$AGENT_MEMORY_VAULT` (default `~/agent-memory-vault`).
