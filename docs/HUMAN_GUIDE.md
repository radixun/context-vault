# Human Guide

Context Vault keeps project context in a local folder of Markdown files.
You own the notes. Obsidian is optional. Python 3.10 or later runs the tools.
No Python packages, account, server or community plugin are required.

## Install

Use macOS or Linux with Python 3.10 or later. The release is tested on macOS.
Linux uses the same file tools, but this release has no Linux acceptance result.
Windows installation is not supported in this release.
The file tools include Windows locks and daily-log names. This support is
separate from installation and native hook acceptance.

```sh
git clone https://github.com/radixun/context-vault.git
cd context-vault
python3 install.py
```

The installer creates:

| Path | Use |
| --- | --- |
| `~/Documents/context_vault` | Your private local notes and tools |
| `~/.codex/skills/context-vault` | The installed skill |
| `~/.codex/context-vault-integration` | Hook runner, hook configuration and harness fragment |

The installer refuses existing destinations. It does not change your existing
`AGENTS.md`, `hooks.json` or other skills. Keep the distribution checkout separate
from your private Vault. Do not commit your populated Vault to this repository.

For other paths, use `--vault /absolute/path/to/vault` and
`--harness /absolute/path/to/harness`. Paths with spaces are supported.

## Connect Codex

1. Read the generated `~/.codex/context-vault-integration/AGENTS.fragment.md`.
   Append its contents to `~/.codex/AGENTS.md`. Create that file if needed.
2. Read the generated `hooks.json` in the same integration folder. Add its
   `PostToolUse` and `Stop` groups to the matching arrays in `~/.codex/hooks.json`.
   Preserve existing hook groups. If no file exists, copy the generated file.
3. Start a new Codex session. Review and trust the hook definitions when Codex
   requests it. Hook support and trust are controlled by the host application.

See the [official hook documentation](https://learn.chatgpt.com/docs/hooks).
For a different harness, use its skill and hook registration mechanism. The
commands and agent guide remain usable without native hooks. No other harness
adapter is tested in this release.

The write hook reports problems after recognized file edits. The Stop hook
requests one correction if an active project has no daily file for today.
It does not prove that every task was logged. Neither hook stores a transcript.
The explicit validation commands remain required.

## Activate a Project

Activation is a deliberate choice. Replace the workspace and slug below:

```sh
python3 ~/Documents/context_vault/50_System/Scripts/activate_project.py \
  --vault ~/Documents/context_vault \
  --workspace /absolute/path/to/my-project \
  --slug my-project
```

This creates a project home and manifest, then binds the exact workspace.
The command refuses an existing project or exact binding. It does not import
source code or previous conversations. Read the new notes and replace starter
text with facts about your project. Do not leave example facts as current truth.

Ask the agent to use Context Vault in the next task. It must resolve the
workspace and read the active manifest before using memory. To pause memory,
set `status: paused` in the project manifest and project home, then regenerate
indexes. Existing notes remain available to you.

## Read and Edit

In Obsidian, select **Open folder as vault**, then select your installed Vault.
Open `Welcome.md`. Follow the project, session and decision indexes.
Use any text editor if you do not use Obsidian.

| Folder | Content |
| --- | --- |
| `00_Inbox` | Unsorted notes |
| `10_Projects` | Project homes and manifests |
| `20_Areas` | Ongoing responsibilities |
| `30_Resources` | Reusable reference material |
| `40_Archive` | Inactive material |
| `50_System` | Policy, templates, indexes and tools |
| `60_Sessions` | Compact daily logs, grouped by project |
| `70_Decisions` | Decisions and their reasons |

Keep one writer per note. Do not edit a note in Obsidian while an agent replaces
it. Take backups with your normal local backup tool. To restore, use a separate
folder first and check the contents before replacing your active Vault.

## Check and Maintain

```sh
python3 ~/Documents/context_vault/50_System/Scripts/generate_indexes.py \
  --vault ~/Documents/context_vault --check
python3 ~/Documents/context_vault/50_System/Scripts/vault-audit.py \
  --vault-path ~/Documents/context_vault --ratchet --summary
```

Use `--write` instead of `--check` after a deliberate route change. Fix ERROR
and WARN findings. Never increase the baseline merely to pass the audit.
An audit checks structure and metadata. Review the meaning of changed facts.

## Remove or Upgrade

To remove the integration, remove only the Context Vault hook groups and harness
fragment that you added. Remove its skill and integration folders when no session
uses them. Your private Vault can remain as ordinary notes.

The installer does not upgrade an existing Vault. Test a new release in new
paths first. Review changes to tools, schemas and instructions before applying
them to a populated Vault. Preserve notes and use your normal backup procedure.
