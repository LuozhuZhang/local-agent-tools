# local-agent-tools

An experimental MCP server that lets an AI agent operate the machine it runs on:
read and edit files, run commands, keep multi-stage Tasks with original receipts,
browse project directories, delegate browser subtasks to a local Codex, and read
built-in operating Skills.

**Experimental and unmaintained.** No releases, support or compatibility promises.

## Install

Python 3.12 or newer on macOS or Linux, with Git. From a local checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
```

Tasks are stored in SQLite and need SQLite 3.51.3 or newer, or a patched 3.50.7+
or 3.44.6+ release. Check the interpreter the server runs on with
`.venv/bin/python -c 'import sqlite3; print(sqlite3.sqlite_version)'`;
the python.org 3.12 and 3.13 installers ship older versions. The first `create_task`
or `save_task` creates the Task database. With an older SQLite, Task writes fail with
`task_sqlite_unsupported`, and once a Task database exists so do file writes,
commands and worktree creation, which check it for Task reservations. Reads work
with any SQLite.

## Configure

```sh
.venv/bin/local-agent-tools init --workspace /absolute/path/to/project
```

This writes `~/.config/local-agent-tools/config.toml` (mode 0600) and never
overwrites it. By default access is read-only. Commands need
`--local-access full_access --enable-commands`; file writes need
`--local-access full_access --enable-workspace-writes`. Private state lives in
`~/.local/state/local-agent-tools`. `--config` and `--state-dir` go before the
subcommand.

## Use with an MCP client

The server speaks MCP over stdio; the client starts it. One server runs per state
directory, and a second one on the same directory is refused, so give each client
its own `--state-dir` under `~/.local/state/local-agent-tools/`, where scoped
commands cannot reach it. Each state directory coordinates only itself: locks,
resource leases, Tasks and receipts do not cover other clients, so give concurrent
clients separate workspaces or worktrees.

```json
{
  "mcpServers": {
    "local-agent-tools": {
      "command": "/absolute/path/to/.venv/bin/local-agent-tools",
      "args": ["--state-dir", "/Users/you/.local/state/local-agent-tools/this-client", "serve"]
    }
  }
}
```

## Security

The tools act as your user account, not inside a sandbox:

- Reads reach any directory your account can read, not only configured
  workspaces; only credential-named paths (such as `.ssh`, `.env*` and `*.key`)
  are refused.
- With Full Access, enabled commands and file writes apply to everything your
  account can change, not only configured workspaces.
- A call may carry an `operation_scope`, which confines that call's command under
  macOS Seatbelt and its file writes to the scoped Full roots; Linux refuses
  scoped commands. The calling client chooses whether to send it, so it does not
  restrain an untrusted agent.
- File contents and command output go to the client and its model provider;
  redaction is best effort.

Enable commands and writes only for an agent you trust with your account.
