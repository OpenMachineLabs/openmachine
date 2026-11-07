"""python -I -m openmachine.access: the one Code launch boundary for the Go host (not MCP, not a Task)."""

import argparse
import json
import os
from pathlib import Path
import sys

from openmachine.access import sessions
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE
from openmachine.foundation.sanitize import Fault


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m openmachine.access")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="Print the supported protocol, kinds and policies")
    session = commands.add_parser("session", help="Admit one channel: request and reply on --control-fd, IO on 0-2")
    session.add_argument("--control-fd", type=int, required=True)
    commands.add_parser("terminate", help="End one lifetime: request on stdin, result on stdout")
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        print(json.dumps(sessions.capabilities(), sort_keys=True))
        return 0
    if args.command == "session":
        os.set_inheritable(args.control_fd, False)
        sessions.session(args.control_fd, args.config, args.state_dir)
        return 1  # session() replaces or exits this process.
    try:
        result = sessions.terminate(args.state_dir, sys.stdin.buffer.read(sessions.MAX_REQUEST + 1))
    except Fault as error:
        result = {"status": "denied", "error": {"code": error.code, "message": str(error)}}
    except OSError:
        # Cleanup may have started; no result was stored, so a repeated call retries it.
        result = {"status": "termination_unverified", "reason": "termination_error"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"verified", "not_found"} else 1


if __name__ == "__main__":
    sys.exit(main())
