"""Initialize, diagnose and manage the runtime; not a chat UI."""

import argparse
import json
import os
from pathlib import Path
import sys

from openmachine import __version__
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE, load
from openmachine.foundation.sanitize import Fault


def main() -> int:
    parser = argparse.ArgumentParser(prog="openmachine")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create disabled configuration without overwriting existing files")
    init.add_argument("--workspace", type=Path, help="An existing absolute local workspace to register")
    doctor = commands.add_parser("doctor", help="Inspect configuration or explicitly probe one integration or managed service")
    target = doctor.add_mutually_exclusive_group()
    target.add_argument("--machine", help="Explicit SSH probe; never installs or enables a target")
    target.add_argument("--github", action="store_true", help="Probe the existing GitHub identity without changing login")
    target.add_argument("--service", action="store_true", help="Verify managed service identity and its configured file read")
    doctor.add_argument("--repository", default="", help="With --github, probe one configured repository")
    bundle = commands.add_parser("worker-bundle", help="Build a content-addressed standard-library SSH worker")
    bundle.add_argument("--directory", type=Path, required=True)
    serve = commands.add_parser("serve", help="Serve only on the private state-directory Unix socket")
    serve.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    service = commands.add_parser("service", help="Manage this macOS user service without controlling the tunnel")
    service_commands = service.add_subparsers(dest="service_action", required=True)
    install = service_commands.add_parser("install", help="Install a verified wheel as a stopped user service")
    install.add_argument("--wheel", type=Path, required=True)
    install.add_argument("--requirements", type=Path, required=True)
    install.add_argument("--probe-workspace", required=True)
    install.add_argument("--probe-path", required=True)
    for action in ("start", "status", "stop", "restart"):
        service_commands.add_parser(action)
    update = commands.add_parser("update", help="Verify and activate an explicit wheel; retain the original installation")
    update.add_argument("--wheel", type=Path, required=True)
    update.add_argument("--requirements", type=Path, required=True)
    commands.add_parser("rollback", help="Verify and restore the retained compatible installation, never user work")
    args = parser.parse_args()
    try:
        if args.command == "init":
            text = 'schema_version = 1\n\n[machines.local]\ntransport = "local"\nexecution_enabled = false\n'
            if args.workspace is not None:
                if not args.workspace.is_absolute() or not args.workspace.is_dir():
                    raise Fault("invalid_workspace", "--workspace must be an existing absolute directory.")
                text += '\n[workspaces.project]\nmachine = "local"\nroot = ' + json.dumps(str(args.workspace.resolve())) + '\n'
            args.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(args.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            print(json.dumps({"created": str(args.config), "execution_enabled": False}))
            return 0
        if args.command == "worker-bundle":
            from integrations.ssh.bundle import build_bundle
            print(json.dumps(build_bundle(args.directory)))
            return 0
        if args.command in ("service", "update", "rollback"):
            from openmachine.service import operate
            result = operate(args.config, args.state_dir,
                             args.service_action if args.command == "service" else args.command,
                             wheel=getattr(args, "wheel", None), requirements=getattr(args, "requirements", None),
                             workspace=getattr(args, "probe_workspace", ""), path=getattr(args, "probe_path", ""))
            print(json.dumps(result))
            return int(result["status"] in {"unready", "activation_pending"})
        config = load(args.config)
        if args.command == "doctor":
            if args.repository and not args.github:
                raise Fault("invalid_github_request", "--repository requires doctor --github.")
            if args.service:
                from openmachine.service import operate
                result = operate(args.config, args.state_dir, "status")
                print(json.dumps(result))
                return int(result["status"] != "ready")
            if args.github:
                from openmachine.bootstrap import create_application
                print(json.dumps(create_application(config, args.state_dir).github_read("status", args.repository)))
                return 0
            if args.machine:
                from openmachine.bootstrap import create_application
                result = create_application(config, args.state_dir).probe_machine(args.machine)
                print(json.dumps(result))
                return int(not result["identity_verified"] or bool(result["unavailable_workspaces"]))
            unavailable = [name for name, item in config.workspaces.items()
                           if config.machine(item.machine).transport == "local" and not Path(item.root).is_dir()]
            print(json.dumps({"version": __version__, "configuration": "valid", "unavailable_workspaces": unavailable,
                              "state_directory_exists": args.state_dir.is_dir(),
                              "mcp_service": "not_probed", "tunnel": "not_probed", "chatgpt": "not_probed",
                              "github": "not_probed" if config.github is not None else "disabled",
                              "ssh_targets": {name: "not_probed" for name, item in config.machines.items() if item.transport == "ssh"}}))
            return int(bool(unavailable))
        from openmachine.server import serve
        serve(config, args.state_dir, managed=args.managed)
        return 0
    except (Fault, OSError) as error:
        print(json.dumps({"error": {"code": error.code if isinstance(error, Fault) else type(error).__name__,
                                     "message": str(error) if isinstance(error, Fault) else "Operation failed; check paths and permissions."}}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
