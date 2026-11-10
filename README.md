# local-agent-tools

An experimental MCP server that lets an AI agent operate the machine it runs on:
read and edit files in workspaces, run commands (confined by macOS Seatbelt when a
call carries an operation scope), keep multi-stage Tasks with original receipts,
browse project directories, delegate browser subtasks to a local Codex, and read
built-in operating Skills.

**Experimental and unmaintained.** No releases, support or compatibility promises.

## Install

Python 3.12 or newer on macOS or Linux, with Git. Task storage needs SQLite
3.51.3 or newer (or the patched 3.50.7+ or 3.44.6+ releases). From a local checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
```

## Configure

```sh
.venv/bin/local-agent-tools init --workspace /absolute/path/to/project
```

This writes `~/.config/local-agent-tools/config.toml` (mode 0600) and never
overwrites it. Commands and file writes stay disabled unless you pass
`--local-access full_access` with `--enable-commands` and
`--enable-workspace-writes`, or edit the file. Private state lives in
`~/.local/state/local-agent-tools`. `--config` and `--state-dir` go before the
subcommand.

## Use with an MCP client

The server speaks MCP over stdio; the client starts it:

```json
{
  "mcpServers": {
    "local-agent-tools": {
      "command": "/absolute/path/to/.venv/bin/local-agent-tools",
      "args": ["serve"]
    }
  }
}
```

One server runs per state directory.

## Security

Commands and file edits run as your user with your account's access. Without an
operation scope this is not a sandbox. Output and file contents go to the client
and its model provider; redaction is best effort. Grant commands and writes only
for workspaces you are prepared to let an agent change.
