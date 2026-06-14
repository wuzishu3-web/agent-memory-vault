# Contributing

Thanks for your interest! This is a small, focused toolkit — contributions that keep it simple and dependency-light are very welcome.

## Principles

- **Stdlib-first.** The only third-party dependency is `pyyaml` (for `extract_relations.py`). Embeddings use any OpenAI-compatible endpoint over plain `urllib`. Please don't add heavy frameworks.
- **Understanding vs bookkeeping stay separated.** Scripts do deterministic filing; they never auto-summarize content. The *agent* provides the understanding.
- **Never hard-code private data.** No real names, companies, paths, or secrets — even in examples or comments. Use generic placeholders (`Alice`, `Acme Inc`, `~/agent-memory-vault`).
- **Graceful degradation.** If the embedding endpoint is down, capturing knowledge must still work (skip cross-refs / indexing, mark for later).

## Dev setup

```bash
git clone https://github.com/wuzishu3-web/agent-memory-vault.git
cd agent-memory-vault
pip install pyyaml
export AGENT_MEMORY_VAULT="$(mktemp -d)/vault"   # a throwaway vault for testing
python3 scripts/init_vault.py
```

## Before you open a PR

```bash
# 1. Everything must compile (CI enforces this on 3.10–3.12)
python3 -m py_compile scripts/*.py

# 2. Run the stdlib-only smoke path
python3 scripts/agent_memory.py --type decision --agent claude-code --title "test" --summary "s" --body "b"
python3 scripts/agent_memory_boot.py --agent claude-code --task "test"
python3 scripts/vault_health_check.py
```

- Use `python3 -m py_compile` (not just `ast.parse`) — it catches things like misplaced `from __future__` imports that `ast.parse` lets through.
- Keep scripts readable and match the surrounding style (type hints, docstrings, `argparse`).

## Ideas / roadmap

- Make UI strings configurable / English-by-default (some are currently Chinese).
- Pluggable embedding backends beyond the OpenAI-compatible default.
- A `sync` command to vendor scripts into an existing vault's `_system/scripts/`.

Open an issue to discuss anything substantial before a large PR. Bug reports with a minimal repro are gold.
