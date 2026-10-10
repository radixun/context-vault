# Contributor Instructions

Use Python 3.10 or later for application code. The standard library covers the
local file, JSON, validation and lock operations. Keep this language choice.
Use clear, short English sentences. Keep technical identifiers correct.

Do not add user notes, credentials, machine names, local paths, chat transcripts
or runtime files to this repository. Use synthetic test data only.
The `vault/` directory is a distribution template, not a personal memory store.
Do not activate memory in this checkout. Install a separate private Vault.

Keep `vault/50_System/Scripts` as the source for the system tools. Keep the
metadata parser and validator copies in `skill/scripts` byte-identical.
Run `python3 tools/check.py` on the operating system under test.
Use a single writer per note. Test revision conflicts and invalid metadata.
Do not increase the audit baseline to hide a failure.

Keep public descriptions focused on the product. Put agent procedures in the
agent guide and skill. Use only synthetic data in screenshots.
