# Version 1.0.0

The first release includes a clean local Markdown Vault, a Context Vault skill,
a harness fragment, two hooks and Python tools for project memory.

- Activate each project explicitly.
- Start reads from its manifest.
- Validate changed notes and reject stale replacements.
- Keep compact daily logs and generated reading routes.

Install and read the setup steps in the [human guide](HUMAN_GUIDE.md).
The [agent guide](AGENT_GUIDE.md) defines the memory work sequence.

The release checks run on macOS. Linux and Windows runtime acceptance is not
claimed. Windows installation is not supported. Native Codex hook trust must
be configured by the user; hook payload tests do not prove a new Codex session.
