---
id: tools.browser
name: tools-browser
description: Delegate a bounded explicitly authorized browser or UI goal to installed native Codex, then verify semantic results and original execution evidence.
---

# Browser subtasks

Keep the client in control. Delegate only an explicitly authorized browser/UI subtask;
use existing API and workspace tools for their own operations.

Read [browser delegation](reference.md) before starting. Supply a self-contained
goal, permitted actions, visible acceptance, selected browser/tab context and exact
origins. Preserve the original execution ID and current task context. Installation,
login, account switching and permission confirmation are separate authority.

Use `read_codex_task` to inspect the actual report, final native page evidence and
pending action before accepting a stage. Exit zero alone is insufficient. Missing
bridge/login, confirmation or uncertain side effects remain blockers. Cancellation
is not rollback. A generic browser subtask does not wake the client.
