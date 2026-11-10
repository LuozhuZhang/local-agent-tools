# Install, configure and discover Skills

[Domain guide](SKILL.md)

On this page:

- [Install](#install)
- [Configure and run](#configure-and-run)
- [Connect a client](#connect-a-client)
- [Skills](#skills)
- [Client-call diagnostics](#diagnose-client-calls-and-interrupted-work)

## Install

Use Python 3.12 or newer on macOS or Linux, with Git available for workspace
identity. From a local checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
```

Use `requirements.lock` as the complete dependency source. Workers use the
installed package, never a source-checkout import.

The Task database needs SQLite 3.51.3 or newer, or the patched 3.50.7+ (3.50.x)
or 3.44.6+ (3.44.x) releases. The first `create_task` or `save_task` creates it. With an older
SQLite, Task writes fail with `task_sqlite_unsupported`; once the database exists,
file writes, commands and worktree creation fail the same way because they check
it for Task reservations. Reads work with any SQLite.

## Configure and run

Start with an existing disposable workspace; replace the example path:

```sh
.venv/bin/local-agent-tools init --workspace /absolute/path/to/disposable-project
```

Default configuration: `~/.config/local-agent-tools/config.toml`. Private state:
`~/.local/state/local-agent-tools/`. Global `--config` and `--state-dir` options
go before the subcommand. `init` creates mode-0600 configuration without
overwriting files. Access is read-only by default. Commands need
`--local-access full_access --enable-commands`; typed file writes need
`--local-access full_access --enable-workspace-writes`. Restart the server after a
configuration change. Unknown fields are rejected, not ignored.

`serve` speaks MCP over stdin and stdout. It opens no network port and installs
no OS service; each running command has a private Unix control socket under the
state directory. One server runs per state directory, because its background
Task observer must be the only one there; a second server on the same directory is
refused. Give each MCP client its own `--state-dir`. Each state directory
coordinates only itself: workspace leases, writer locks, Task reservations and
change records do not cover other clients, so give concurrent clients separate
workspaces or worktrees.

## Connect a client

The MCP client starts the server as a stdio subprocess and owns its lifetime, for
example `local-agent-tools --state-dir /absolute/state/for-this-client serve`.
Any MCP client works; the server does not depend on a particular client or model
provider. A completed handshake is not proof that the client approved every tool.

## Skills

Configure explicit existing sources:

```toml
[skills]
roots = ["/absolute/skills"]
```

A root is one Skill directory with `SKILL.md` or an immediate-child collection;
there is no recursive account scan. Canonical whole-directory links are supported,
duplicated sources deduplicate by identity, not name, and resource links cannot
escape the selected directory. Useful dotfiles remain visible; credentials are
excluded. Discovery scans at most 2,000 entries and reports skipped/inaccessible
sources. Follow hashes, snapshots and paging; incomplete discovery is not absence.
An empty read selector returns the short built-in Overview. `action=list` returns
metadata; `action=read, tool="<available-tool-name>"` selects its bound guide.
Use exact returned IDs and `action=files` before reading a relative topic path.
External user edits are live on the next read; changing configured roots follows
a server restart. Instructions never confer grants, install tools or execute
embedded resources automatically.

## Diagnose client calls and interrupted work

After an update, compare the actual client tool definitions with this server's
`tools/list`. A reconnect does not prove that a client refreshed its approved
catalog. Use the client's supported tool-refresh flow when definitions differ.

`get_capabilities.mcp_catalog` fingerprints the server's advertised descriptors,
sorted by name and encoded as sorted compact UTF-8 JSON plus LF. The digest covers
names, descriptions, schemas and annotations; it is not a client-match or health
claim.

An `invalid_tool_call` includes a safe diagnostic source, reason, phase and
catalog digest. `current_call_dispatch=not_attempted` applies only to that rejected
call before its handler, never to an earlier request with the same operation ID.
A handler failure reports dispatch as unknown: read the original task/execution or
destination before retrying. Structured domain errors retain their original codes.
Missing replies alone prove neither no effect nor a platform refusal.

Distinguish catalog mismatch, permission denial, task-authority expiry, transport
failure and an explicit client/platform refusal. Preserve the original error and
IDs. A bounded wait ending is not a command timeout or cancellation; original work
may still run. Read its original result before continuing. See
[task continuation](../tasks/continue.md). Do not relabel mutating tools as
read-only, expand grants, remove task context or use another executor to evade a
denial.
