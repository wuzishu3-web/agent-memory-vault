# Agent Memory Vault — Home

Shared long-term memory for your AI coding agents. Start every session by reading
`_system/hot.md` (current state) and the last lines of `_system/log.md` (timeline).

## Map

- [[_system/WRITE_GUIDE|WRITE GUIDE]] — what content goes where (read before writing)
- [[_system/AGENT_MEMORY_PROTOCOL|Protocol]] — how agents share this vault
- `01_User/profile.md` — stable user preferences & background
- `02_Agents/` — one profile per agent
- `03_Projects/` · `04_Knowledge/` · `06_Decisions/` · `07_Playbooks/` · `08_Sources/`

## Quick start

```bash
python3 scripts/agent_memory_boot.py --agent claude-code --task "..."   # boot
python3 scripts/ingest.py --agent claude-code --title "..." --url "..." --source-type article
python3 scripts/query_vault.py "..."                                     # search
```
