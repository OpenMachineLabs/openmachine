"""Initialize, diagnose and manage the runtime; not a chat UI."""

import argparse
import json
from pathlib import Path
import sys

from openmachine import __version__
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE, initialize, load
from openmachine.foundation.sanitize import Fault


def main() -> int:
    parser = argparse.ArgumentParser(prog="openmachine")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create disabled configuration without overwriting existing files")
    init.add_argument("--enable-commands", action="store_true")
    init.add_argument("--enable-workspace-writes", action="store_true")
    init.add_argument("--workspace", type=Path, help="An existing absolute local workspace to register")
    prepare = commands.add_parser("prepare-runtime", help="Prepare a verified offline payload without starting or selecting a service")
    prepare.add_argument("--payload", type=Path, required=True)
    prepare.add_argument("--sha256", required=True, help="Manifest digest from the trusted product artifact")
    doctor = commands.add_parser("doctor", help="Inspect configuration or explicitly probe one integration or managed service")
    target = doctor.add_mutually_exclusive_group()
    target.add_argument("--machine", help="Explicit SSH probe; never installs or enables a target")
    target.add_argument("--github", action="store_true", help="Probe the selected native GitHub identity without initiating login")
    target.add_argument("--service", action="store_true", help="Verify managed service identity and its configured file read")
    doctor.add_argument("--repository", default="", help="With --github, probe one configured repository")
    rebuild = commands.add_parser("rebuild-task-index", help="Explicitly rebuild one bounded page of derived task metadata, never task history")
    rebuild.add_argument("--expected-generation", type=int, default=-1, help="Required observed generation when continuing an incomplete rebuild")
    bundle = commands.add_parser("worker-bundle", help="Build a content-addressed standard-library SSH worker")
    bundle.add_argument("--directory", type=Path, required=True)
    serve = commands.add_parser("serve", help="Serve only on the private state-directory Unix socket")
    serving = serve.add_mutually_exclusive_group()
    serving.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    serving.add_argument("--managed-child", action="store_true", help=argparse.SUPPRESS)
    service = commands.add_parser("service", help="Manage this macOS user service without controlling the tunnel")
    service_commands = service.add_subparsers(dest="service_action", required=True)
    install = service_commands.add_parser("install", help="Install a verified wheel as a stopped user service")
    install.add_argument("--wheel", type=Path, required=True)
    install.add_argument("--requirements", type=Path, required=True)
    install.add_argument("--probe-workspace", required=True)
    install.add_argument("--probe-path", required=True)
    for action in ("start", "status", "stop", "restart"):
        operation = service_commands.add_parser(action)
        if action == "restart":
            operation.add_argument("--reset-recovery", action="store_true", help="Explicitly renew a verified readiness recovery budget after stop")
    update = commands.add_parser("update", help="Verify and activate an explicit wheel; retain the original installation")
    update.add_argument("--wheel", type=Path, required=True)
    update.add_argument("--requirements", type=Path, required=True)
    update.add_argument("--candidate-config", type=Path, help="Explicit candidate configuration; preserve and restore the original on activation failure")
    commands.add_parser("rollback", help="Verify and restore the retained compatible installation, never user work")
    manage = commands.add_parser("manage", help="Typed product management; explicit config/state binding and original operation readback")
    manage.add_argument("--contract", type=int, default=1)
    management = manage.add_subparsers(dest="management_action", required=True)
    for action in ("status", "start", "stop", "rollback", "machines"):
        management.add_parser(action)
    bind = management.add_parser("bind", help="Bind the stable entry to an exact retained payload implementation")
    bind.add_argument("--identity", required=True)
    startup = management.add_parser("startup")
    startup.add_argument("--enabled", choices=("true", "false"), required=True)
    configure = management.add_parser("configure", help="Create first-run config without overwrite; grants are independent")
    configure.add_argument("--workspace", type=Path, required=True)
    configure.add_argument("--enable-commands", action="store_true")
    configure.add_argument("--enable-workspace-writes", action="store_true")
    configure.add_argument("--recovery-connection", help="Existing ChatGPT connection label, not credentials")
    configure.add_argument("--browser-account", help="Explicit signed-in browser account label")
    configure.add_argument("--browser-workspace", help="Explicit ChatGPT workspace label")
    configure.add_argument("--recovery-model", help="Explicit existing model label")
    configure.add_argument("--verify-existing", action="store_true", help="Read back the exact first-run inputs after a lost reply; never create or overwrite")
    for action in ("install", "update"):
        operation = management.add_parser(action)
        operation.add_argument("--operation-id", required=True)
        operation.add_argument("--payload", type=Path, required=True)
        operation.add_argument("--sha256", required=True)
        if action == "install":
            operation.add_argument("--probe-workspace", required=True)
            operation.add_argument("--probe-path", required=True)
        else:
            operation.add_argument("--candidate-config", type=Path)
    for action in ("operation", "continue", "retire"):
        operation = management.add_parser(action)
        operation.add_argument("--operation-id", required=True)
    for action in ("machine-probe", "machine-test", "machine-add", "machine-remove"):
        operation = management.add_parser(action)
        operation.add_argument("--name", required=True)
        if action in {"machine-test", "machine-add"}:
            operation.add_argument("--ssh-alias", required=True)
            operation.add_argument("--ssh-worker", required=True)
        if action == "machine-add":
            operation.add_argument("--ssh-identity", required=True)
        if action in {"machine-add", "machine-remove"}:
            operation.add_argument("--operation-id", required=True)
            operation.add_argument("--config-sha256", required=True)
    args = parser.parse_args()
    if args.command == "manage" and (args.config is None or args.state_dir is None):
        parser.error("manage requires explicit --config and --state-dir before the subcommand")
    args.config = args.config if args.config is not None else DEFAULT_CONFIG
    args.state_dir = args.state_dir if args.state_dir is not None else DEFAULT_STATE
    try:
        if args.command == "manage":
            from openmachine.service.maintenance import dispatch
            inputs = {}
            action = args.management_action
            if action in {"install", "update", "operation", "continue", "retire"}:
                inputs["identity"] = args.operation_id
            if action in {"install", "update"}:
                inputs.update(source=args.payload, sha256=args.sha256)
                if action == "install":
                    inputs.update(workspace=args.probe_workspace, path=args.probe_path)
                else:
                    inputs["candidate_config"] = args.candidate_config
            if action.startswith("machine-"):
                inputs["name"] = args.name
                if action in {"machine-test", "machine-add"}:
                    inputs.update(alias=args.ssh_alias, worker=args.ssh_worker)
                if action == "machine-add":
                    inputs["pinned"] = args.ssh_identity
                if action in {"machine-add", "machine-remove"}:
                    inputs.update(identity=args.operation_id, expected_config_sha256=args.config_sha256)
            if action == "bind":
                inputs["identity"] = args.identity
            if action == "startup":
                inputs["enabled"] = args.enabled == "true"
            if action == "configure":
                inputs.update(workspace=args.workspace, commands=args.enable_commands,
                              workspace_writes=args.enable_workspace_writes, verify_existing=args.verify_existing)
                profile = dict(connection=args.recovery_connection, browser_account=args.browser_account,
                               browser_workspace=args.browser_workspace, model=args.recovery_model)
                if any(value is not None for value in profile.values()):
                    inputs["chat_recovery"] = {"workspace": "project", **profile}
            result = dispatch(args.config, args.state_dir, action, contract=args.contract, **inputs)
            print(json.dumps(result))
            return int(result["status"] in {"unknown", "incompatible", "busy", "unready", "recovering", "blocked",
                                            "activation_pending", "recovery_required", "rejected", "restored"})
        if args.command == "prepare-runtime":
            from openmachine.service import prepare_payload
            print(json.dumps(prepare_payload(args.config, args.state_dir, args.payload, args.sha256)))
            return 0
        if args.command == "init":
            print(json.dumps(initialize(args.config, args.workspace, commands=args.enable_commands,
                                        workspace_writes=args.enable_workspace_writes)))
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
                             workspace=getattr(args, "probe_workspace", ""), path=getattr(args, "probe_path", ""),
                             candidate_config=getattr(args, "candidate_config", None),
                             reset_recovery=getattr(args, "reset_recovery", False))
            print(json.dumps(result))
            return int(result["status"] in {"unready", "recovering", "blocked", "activation_pending"})
        if args.command == "serve" and args.managed:
            from openmachine.service import supervise
            supervise(args.config, args.state_dir)
            return 0
        config = load(args.config)
        if args.command == "rebuild-task-index":
            from openmachine.bootstrap import create_application
            print(json.dumps(create_application(config, args.state_dir).rebuild_task_index(args.expected_generation)))
            return 0
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
        serve(config, args.state_dir, managed=args.managed_child)
        return 0
    except (Fault, OSError) as error:
        print(json.dumps({"error": {"code": error.code if isinstance(error, Fault) else type(error).__name__,
                                     "message": str(error) if isinstance(error, Fault) else "Operation failed; check paths and permissions."}}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
