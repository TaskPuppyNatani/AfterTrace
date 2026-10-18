# Project scoping and explicit import (Milestone 3)

Open **Sources → Discover Codex**, then **Projects**. Discovery records bounded source
metadata, project evidence, and counts. It never loads complete transcripts or writes
normalized sessions/messages/tool calls. Opening the app only reloads saved AfterTrace
metadata; source discovery requires the button.

New projects default to **Excluded**. Choose a scope, then use **Import Included Projects**:

- **Included** permits an explicit import; changing scope never starts an import.
- **Excluded** blocks new imports. Previously permitted imported copies remain local.
- **Never Analyze** blocks content ingestion and removes existing AfterTrace copies,
  associated excerpts/derived records, import checkpoints, and per-session references.
  Only project/source identity, known locations, scope, timestamps, and counts remain.
  Original Codex histories and repository working trees are never modified.

After lifting Never Analyze, refresh discovery to recreate permitted lightweight references.
Unclassified Sessions remain visible as a count and are always excluded from import. Manual
assignment and interactive merge/separation are deferred.

## Conservative project identity

An accessible Git repository supplies identity evidence. Origin URLs normalize common
HTTPS, SSH, and SCP representations, without retaining URL credentials. Host names and
standard ports normalize; repository path case and custom ports remain distinct. Multiple
incompatible origins do not supply a remote identity. Without one, a Git directory's local
filesystem device/file identity provides a local anchor. This makes no cross-machine identity
claim and is unavailable on filesystems that do not expose a usable file identity.

Projects have logical IDs; ProjectSources record locations independently. An equivalent
normalized remote can connect a later location to an existing logical project and scope.
Directory names and path similarity never merge projects. Non-Git, missing, foreign-platform,
or otherwise untrustworthy paths stay unclassified. Git is optional and uses bounded,
read-only commands without optional locks, global config, hooks, or network access.

Known Never Analyze locations remain blocked when Git becomes inaccessible or changes.
Their descendant working-directory hints also remain blocked. This conservative rule avoids
lifting an exclusion merely because repository evidence disappeared.

## Persistence and import safety

Schema version **2** adds a lightweight discovered-reference table and privacy guards.
Migration preserves existing foundation records and advances atomically. An existing
Never Analyze decision is honored by purging only that project's prohibited content during
upgrade. SQLite secure deletion is enabled for AfterTrace-owned database pages.

Each import uses a separate write transaction. The repository rereads durable scope before
calling any content loader. That permission check, normalization, and persistence serialize
with scope changes; a later Never Analyze decision purges an import that committed first.
Database triggers also reject direct normalized writes to Never Analyze projects.

Import reconfirms Git identity before loading. A source snapshot that changed since discovery
is rejected before normalization: refresh discovery and retry. The existing inexpensive
size/mtime fingerprint guarantee from Milestone 2 still applies. An unchanged snapshot is
loaded read-only but skips normalization and duplicate persistence. Stable harness/native
session IDs reuse the logical imported session, including across active/archive sources.
Sessions without native IDs use their existing reference association.

Changed sessions replace their normalized children atomically after rediscovery, preserving
first-import time and updating fingerprints/checkpoints. A failed session rolls back its
changes without undoing earlier successful sessions. If existing evidence references prevent
a replacement, the failure preserves the old copy rather than invalidating evidence silently.
Import results report imported, unchanged, blocked, and failed counts with content-free
operation/source/project labels and retry guidance. No analysis or model call follows import.

Sources/Projects operations run in worker threads. Navigation stays usable, scope/import
controls pause during an operation, and closing waits for the active operation without
blocking the interactive UI. Unsupported harnesses are identified as unimplemented.

The regression suite uses only synthetic Codex fixtures and temporary offline Git repositories.
Native Windows/macOS runtime validation remains separate from the automated path coverage.
