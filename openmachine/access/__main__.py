"""python -I -m openmachine.access: the one Code launch boundary for the Go host (not MCP, not a Task)."""

import argparse
import json
import os
from pathlib import Path
import sys

from openmachine.access import sessions
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m openmachine.access")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="Print the supported protocol, kinds and policies")
    session = commands.add_parser("session", help="Admit one channel: request and reply on --control-fd, IO on 0-2")
    session.add_argument("--control-fd", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        print(json.dumps(sessions.capabilities(), sort_keys=True))
        return 0
    os.set_inheritable(args.control_fd, False)
    sessions.session(args.control_fd, args.config, args.state_dir)
    return 1  # session() replaces or exits this process.


if __name__ == "__main__":
    sys.exit(main())
