## Local Context Vault

Use the local Vault at @VAULT_PATH@ only when project memory is active.
Resolve the current workspace before reading project memory:

```sh
python3 @RESOLVER@ --vault @VAULT_SHELL@ --workspace /absolute/path/to/project
```

Continue existing memory only when the result is `status=active`. Read the
returned manifest first. Load only the linked notes that the task needs.
For any other result, do not guess a project or create a routing exception.
Activate new project memory only after a direct user request.

Follow the installed `context-vault` skill for reads, writes and validation.
For an active project, record one compact daily entry per task. Update current
state when durable facts change. Reconcile priorities and reading routes.
A newer verified observation can invalidate an older manifest.

Memory operations do not grant permission to change a product or contact a
remote service. Keep these permissions separate.
Never store secrets, raw prompts, full transcripts, source copies or full logs.
Use one writer per note. Preserve concurrent edits. Validate changed notes.
