---
id: tools.workspace
name: tools-workspace
description: Read, search, edit or patch local files and Git state; choose, create or recover optional worktrees safely.
---

# Workspace operations

Each local machine sets `local_access = "read_only"` or `local_access = "full_access"`.
With either, `workspace_name` accepts a canonical absolute directory as well as a
configured name. Configured Workspaces remain names and context; they do not
restrict this whole-device reach. Full Access allows commands and typed writes
only where `execution_enabled` and `workspace_write_enabled` are true; Read Only
denies both. Credential paths, links, hashes, native receipts and Task checks still
apply. Non-Git whole-device mutations use one conservative shared lease domain.

Read current source, applicable instructions, raw whole-file SHA256, workspace
identity and HEAD before proposing an edit. Identity includes root and branch.
Preserve unrelated dirty/staged work; an old hash or test PASS is not current review.

- Read [file discovery, exact edits and patches](files.md) before source paging,
  replacement, multi-file changes or original change recovery.
- Read [worktree selection and recovery](worktrees.md) before selecting or creating
  a tree. Inspect existing trees and tasks first; allocation is never automatic.

Prefer an exact small edit, preview it with an empty change ID, then apply under one
saved original ID and current guards. Whole-file writes have no preview API: review
the complete replacement. After a lost reply read the original change receipt.
Preflight is not a multi-file transaction; retain partial/unknown outcomes.

Typed writes need the workspace-write grant. Structured task effects also need the
[current claim and task context](../tasks/continue.md). Verify actual changed files
and run explicitly authorized validation through the [execution tools](../execution/SKILL.md).

Typed mutations honor Task reservations; reads/previews do not. Worktree
creation locks its destination, not the source for Task lifetime; revise the
Task target before its first write. See [Tasks](../tasks/SKILL.md).
