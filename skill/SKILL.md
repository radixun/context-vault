---
name: context-vault
description: Use local Markdown project memory when the user requests Context Vault or the workspace resolver returns one active project manifest.
---

# Context Vault

The local Vault is @VAULT_PATH@. It uses plain files and has no network service.
The harness rules define activation. This skill defines the work sequence.
The system manifest defines validation. Project manifests hold current facts.

## Read

1. Resolve the workspace:

   ```sh
   python3 @RESOLVER@ --vault @VAULT_SHELL@ --workspace /absolute/path/to/project
   ```

2. Continue existing memory only for `status=active`. For any other status,
   stop memory work. Do not infer a slug, fix routing silently or reactivate a
   paused project. A direct request can authorize new project activation.
3. Read the returned `Memory_Manifest.md`. Stop if it supplies enough context.
4. Read one linked note when necessary. Search only if these routes fail.
   Never read the whole Vault to prepare for a routine task.
5. Check facts by object, scope and observation date. A manifest can be stale.

## Write

Record durable facts, decisions, constraints and useful next steps. Keep the
manifest short. Move necessary history to linked dated notes. Do not discard
useful evidence to satisfy a size limit. Do not store credentials, tokens,
raw prompts, transcripts, source copies or full logs.

Use one writer per note. Before changing an existing manifest, project home,
decision or session note, create a revision receipt outside the Vault:

```sh
python3 @TOOLS@/guard_vault_write.py snapshot --vault @VAULT_SHELL@ --path 10_Projects/example/Memory_Manifest.md --receipt /outside/vault/receipt.json
python3 @TOOLS@/guard_vault_write.py commit --vault @VAULT_SHELL@ --receipt /outside/vault/receipt.json --content-file /outside/vault/proposal.md
```

A stale receipt must fail. Re-read and reconcile; never force the replacement.
Local locks cover cooperating tools. They do not lock a text editor.

Record one compact daily entry for every active-project task, including
read-only tasks. State the task, result, changed files and next step. Prepare
an entry outside the Vault, then run:

```sh
python3 @TOOLS@/append_vault_session.py --vault @VAULT_SHELL@ --project example --host local --entry-file /outside/vault/entry.md
```

The helper creates `60_Sessions/example/YYYY-MM-DD.md`. An exact repeated
entry is a no-op. Use the actual active slug instead of `example`.

## Validate

Check each changed Markdown note explicitly:

```sh
python3 @TOOLS@/validate_vault_write.py --vault @VAULT_SHELL@ --path 10_Projects/example/Memory_Manifest.md
```

Repeat `--path` for multiple notes. To check a proposal without writing it,
use one `--path` with `--content-file`. The PostToolUse hook is additional
feedback. It does not cover every shell or editor write.

After route changes or a new daily note, generate indexes, then check them:

```sh
python3 @TOOLS@/generate_indexes.py --vault @VAULT_SHELL@ --write
python3 @TOOLS@/generate_indexes.py --vault @VAULT_SHELL@ --check
```

Do not edit generated sections by hand. Run the full audit after broad schema,
policy or routing changes, or when explicitly requested:

```sh
python3 @TOOLS@/vault-audit.py --vault-path @VAULT_SHELL@ --ratchet --summary
```

Require zero ERROR and WARN findings. Never raise the baseline to hide debt.
Mechanical validation does not prove that a fact is true. Set `last_verified`
only after a semantic review. Reconcile known copies of changed facts.

## Complete

Before the final response, reconcile current state, priorities and reading
routes; append the daily entry; validate changed notes; and check indexes.
Report unresolved limits. Do not claim a successful check that did not run.
