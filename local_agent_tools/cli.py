"""Create first-run configuration or serve the tools over stdio."""

import argparse
import json
from pathlib import Path
import sys

from local_agent_tools import __version__
from local_agent_tools.config import DEFAULT_CONFIG, DEFAULT_STATE, initialize, load
from local_agent_tools.foundation.sanitize import Fault


def main() -> int:
    parser = argparse.ArgumentParser(prog="local-agent-tools")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create first-run configuration without overwriting existing files")
    init.add_argument("--local-access", choices=("read_only", "full_access"), default="read_only")
    init.add_argument("--enable-commands", action="store_true")
    init.add_argument("--enable-workspace-writes", action="store_true")
    init.add_argument("--workspace", type=Path, help="An existing absolute local workspace to register")
    commands.add_parser("serve", help="Serve MCP over stdin and stdout")
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(json.dumps(initialize(args.config, args.workspace, local_access=args.local_access,
                                        commands=args.enable_commands, workspace_writes=args.enable_workspace_writes)))
            return 0
        from local_agent_tools.server import serve
        serve(load(args.config), args.state_dir)
        return 0
    except (Fault, OSError) as error:
        print(json.dumps({"error": {"code": error.code if isinstance(error, Fault) else type(error).__name__,
                                     "message": str(error) if isinstance(error, Fault) else "Operation failed; check paths and permissions."}}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
