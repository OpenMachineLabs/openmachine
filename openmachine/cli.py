"""Typed Core management: python -I -B -m openmachine.cli --config C --state-dir S manage --contract 1 <action>."""

import argparse
import json
from pathlib import Path
import sys

from openmachine.service.maintenance import dispatch


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -I -B -m openmachine.cli")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    manage = commands.add_parser("manage", help="Typed product management with original operation readback")
    manage.add_argument("--contract", type=int, default=1)
    management = manage.add_subparsers(dest="action", required=True)
    management.add_parser("status")
    configure = management.add_parser("configure", help="Create the first-run configuration without overwrite")
    configure.add_argument("--verify-existing", action="store_true", help="Read back the first-run configuration after a lost reply")
    for action in ("install", "update"):
        operation = management.add_parser(action)
        operation.add_argument("--operation-id", required=True)
        operation.add_argument("--payload", type=Path, required=True)
        operation.add_argument("--sha256", required=True)
        if action == "update":
            operation.add_argument("--expected-selection-sha256", required=True, help="The selection status reported")
    for action in ("operation", "continue"):
        management.add_parser(action).add_argument("--operation-id", required=True)
    args = parser.parse_args()
    inputs = {}
    if args.action == "configure":
        inputs["verify_existing"] = args.verify_existing
    if args.action in {"install", "update", "operation", "continue"}:
        inputs["identity"] = args.operation_id
    if args.action in {"install", "update"}:
        inputs.update(source=args.payload, sha256=args.sha256)
    if args.action == "update":
        inputs["expected_selection_sha256"] = args.expected_selection_sha256
    result = dispatch(args.config, args.state_dir, args.action, contract=args.contract, **inputs)
    print(json.dumps(result))
    return int(result["status"] in {"unknown", "incompatible", "busy", "rejected", "recovery_required"})


if __name__ == "__main__":
    sys.exit(main())
