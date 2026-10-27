"""Explicit SSH setup owned by Core; immutable worker preparation has original readback."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time

from pydantic import ValidationError

from openmachine import __version__
from openmachine.config import Machine, Settings, SshConnection, load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import state as service_state
from . import bundle, client

PHASES = {"discovering", "host_key_review", "inspecting", "ready_to_prepare", "preparing", "prepared", "project_ready", "saving", "save_failed", "completed", "cancelled"}


def _fault(code):
    raise Fault(code, "The SSH setup could not be verified; preserve its original reference and inspect the indicated step.")


def _path(state, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        _fault("invalid_operation_id")
    return state / "service/ssh-setups" / (identity + ".json")


def _read(state, identity):
    path = _path(state, identity)
    record = service_state._json(path)
    try:
        if (record["format"] != 1 or record["operation_id"] != identity or record["phase"] not in PHASES
                or not re.fullmatch(r"[0-9a-f]{64}", record["config_sha256"])
                or not re.fullmatch(r"[0-9a-f]{64}", record["artifact"]["sha256"])
                or not Path(record["config"]).is_absolute()):
            raise ValueError
        _target(record)
    except (KeyError, TypeError, ValueError):
        _fault("ssh_setup_invalid")
    return record


def check_state(state):
    root = state / "service/ssh-setups"
    if not os.path.lexists(root):
        return {"ssh_setup_records": 0, "ssh_setup_sha256": None}
    info = root.lstat()
    if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        _fault("ssh_setup_invalid")
    records = []
    for path in sorted(root.iterdir()):
        if path.name == "operation.lock" or path.name.startswith(".record-"):
            continue
        if len(records) >= 128 or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
            _fault("ssh_setup_invalid")
        record = _read(state, path.stem)
        # Reconciliation may advance while a candidate reads state. Compare the
        # immutable original request, while validating every current record.
        records.append({key: record[key] for key in ("format", "operation_id", "config", "config_sha256", "artifact", "candidate")})
    return {"ssh_setup_records": len(records), "ssh_setup_sha256": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest() if records else None}


def _target(record):
    candidate = record["candidate"]
    return Machine(transport="ssh", ssh_alias=candidate.get("alias"), ssh_connection=candidate.get("connection"),
                   ssh_worker=(record.get("facts") or {}).get("worker", "/" + record["artifact"]["sha256"] + ".pyz"),
                   ssh_identity=(record.get("facts") or {}).get("identity", ""),
                   execution_enabled=False, workspace_write_enabled=False)


def _fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(key.split()[1], validate=True)).digest()).decode().rstrip("=")


def _keys(raw):
    result = []
    for line in raw.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0].startswith("@"):
            _fault("ssh_host_key_review_required")
        if len(parts) < 3:
            _fault("ssh_host_key_review_required")
        key = " ".join(parts[1:3])
        try:
            SshConnection(host="fixture.invalid", user="fixture", host_key=key)
        except ValidationError:
            _fault("ssh_host_key_review_required")
        result.append(key)
        if len(result) > 32:
            _fault("ssh_host_key_review_required")
    return result


def _discover(connection):
    host, port = connection["host"], connection["port"]
    code, raw = client._exchange(["ssh-keyscan", "-T", "5", "-p", str(port), "-t", "ed25519,ecdsa,rsa", host], b"")
    keys = _keys(raw)
    if code or not keys:
        _fault("ssh_unavailable")
    known = []
    address = host if port == 22 else f"[{host}]:{port}"
    for path in (Path.home() / ".ssh/known_hosts", Path("/etc/ssh/ssh_known_hosts")):
        if path.is_file():
            code, raw = client._exchange(["ssh-keygen", "-F", address, "-f", str(path)], b"")
            if code not in (0, 1):
                _fault("ssh_host_key_review_required")
            known.extend(_keys(raw))
    if known and not any(key in keys for key in known):
        _fault("ssh_host_key_mismatch")
    selected = next((key for key in known if key in keys), keys[0])
    return selected, bool(known)


def _remote(record, action, **fields):
    target = _target(record)
    facts = record.get("facts") or {}
    request = {"action": action, "sha256": record["artifact"]["sha256"],
               **{key: facts[key] for key in ("home", "uid", "identity") if key in facts}, **fields}
    source = Path(__file__).with_name("provision.py").read_text()
    with client.connection_args(target.ssh_alias, target.ssh_connection) as argv:
        code, raw = client._exchange([*argv, shlex.join(["python3", "-I", "-c", source])], json.dumps(request).encode())
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and re.fullmatch(r"[a-z_]{1,64}", str(value.get("error", ""))):
            _fault(value["error"])
        expected = f'{value["home"]}/.local/share/openmachine/workers/{request["sha256"]}.pyz'
        if (code or not isinstance(value, dict) or not isinstance(value["home"], str)
                or not value["home"].startswith("/") or len(value["home"]) > 4096
                or any(ord(c) < 32 for c in value["home"])
                or value["worker"] != expected or type(value["uid"]) is not int
                or not re.fullmatch(r"[0-9a-f]{64}", value["identity"])
                or value["worker_status"] not in {"absent", "available"}
                or value["os"] != "Linux"
                or any(value.get(key) != facts[key] for key in ("home", "uid", "identity") if key in facts)):
            raise ValueError
        if any(not isinstance(value.get(key), str) or len(value[key]) > 80
               or any(ord(c) < 32 for c in value[key]) for key in ("python", "systemd", "architecture")):
            raise ValueError
        if action == "project" and (not isinstance(value.get("root"), str) or not value["root"].startswith("/")
                                     or value["root"] == "/" or len(value["root"]) > 4096):
            raise ValueError
        return {key: value[key] for key in ("home", "uid", "identity", "worker", "worker_status", "os", "python", "systemd", "architecture", "root") if key in value}
    except (ValueError, TypeError, KeyError, UnicodeError):
        _fault("remote_runtime_unavailable" if code in (2, 127) else "ssh_protocol_error")


def _probe(record, config_path):
    config = load(config_path)
    name = record["candidate"]["name"]
    candidate = Settings.model_validate(config.model_dump() | {"machines": {
        **{key: value.model_dump() for key, value in config.machines.items()}, name: _target(record).model_dump()}})
    result = client.call(candidate, name, "probe")
    if result["target_identity"] != record["facts"]["identity"]:
        _fault("identity_mismatch")
    return result


def view(record):
    candidate, facts = record["candidate"], record.get("facts")
    route = candidate.get("connection")
    return {"operation_id": record["operation_id"], "phase": record["phase"], "reason": record.get("reason"),
            "name": candidate["name"], "destination": record["destination"], "host_key_known": record["host_key_known"],
            "fingerprint": _fingerprint(route["host_key"]) if route and record["phase"] != "discovering" else None,
            "version": record["artifact"]["version"], "sha256": record["artifact"]["sha256"],
            "worker": facts["worker"] if facts else None,
            "worker_available": bool(facts and facts["worker_status"] == "available"),
            "project": record.get("project"), "operation": record.get("operation"),
            "observed_at_ms": time.time_ns() // 1_000_000}


def act(config_path, state, action, identity, value):
    from openmachine.service import maintenance
    path = _path(state, identity)
    root = private_directory(path.parent)
    if root != path.parent or not isinstance(value, dict):
        _fault("ssh_setup_invalid")
    with lease(root / "operation.lock"):
        if action == "inspect" and not path.exists():
            if len(list(root.glob("*.json"))) >= 128:
                _fault("ssh_setup_limit")
            if set(value) - {"name", "alias", "host", "user", "port", "identity_file"}:
                _fault("invalid_request")
            name = value.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
                _fault("invalid_machine")
            config = load(config_path)
            if name in config.machines:
                _fault("duplicate_machine")
            alias = value.get("alias")
            if alias:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", alias) or set(value) - {"name", "alias"}:
                    _fault("invalid_machine")
                destination, connection = alias, None
            else:
                # Validate the single destination before passing it to keyscan.
                placeholder = "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519" + b"\0\0\0\x20" + bytes(32)).decode()
                try:
                    connection = SshConnection(host=value.get("host"), user=value.get("user"), port=value.get("port", 22),
                                               identity_file=value.get("identity_file"), host_key=placeholder).model_dump()
                except ValidationError:
                    _fault("invalid_machine")
                destination = f'{connection["user"]}@{connection["host"]}:{connection["port"]}'
            artifact = bundle.build_bundle(state / "service/setup-workers")
            record = {"format": 1, "operation_id": identity, "config": str(config_path),
                      "config_sha256": maintenance._digest(config_path), "artifact": artifact,
                      "candidate": {"name": name, "alias": alias or None, "connection": connection},
                      "destination": destination, "host_key_known": False, "facts": None,
                      "phase": "discovering", "reason": None}
            save_json(path, record)
        else:
            record = _read(state, identity)
        if record["config"] != str(config_path):
            _fault("configuration_changed")
        if action == "cancel":
            if record["phase"] in {"preparing", "saving"}:
                _fault("original_operation_required")
            record["phase"] = "cancelled"
        elif action == "trust":
            route = record["candidate"].get("connection")
            if record["phase"] != "host_key_review" or value != {"fingerprint": _fingerprint(route["host_key"]), "confirmed": True}:
                _fault("ssh_host_key_review_required")
            record["phase"] = "inspecting"
            save_json(path, record)
        elif action not in {"inspect", "status", "prepare", "project", "save", "continue"}:
            _fault("invalid_request")
        try:
            if record["phase"] == "discovering":
                candidate = record["candidate"]
                if candidate["alias"]:
                    code, raw = client._exchange(["ssh", "-G", "--", candidate["alias"]], b"")
                    resolved = dict(line.split(" ", 1) for line in raw.decode().splitlines() if " " in line)
                    if code or not all(resolved.get(k) for k in ("hostname", "user", "port")):
                        _fault("ssh_unavailable")
                    destination = f'{resolved["user"]}@{resolved["hostname"]}:{resolved["port"]}'
                    if len(destination) > 512 or any(ord(c) < 32 for c in destination):
                        _fault("invalid_machine")
                    record["destination"], known = destination, True
                else:
                    host_key, known = _discover(candidate["connection"])
                    candidate["connection"]["host_key"] = host_key
                record["host_key_known"] = known
                record["phase"] = "inspecting" if known else "host_key_review"
                save_json(path, record)
            if record["phase"] == "inspecting":
                record["facts"] = _remote(record, "inspect")
                if record["facts"]["worker_status"] == "available":
                    _probe(record, config_path)
                    record["phase"] = "prepared"
                else:
                    record["phase"] = "ready_to_prepare"
            if action == "prepare" and record["phase"] == "ready_to_prepare":
                if value != {"sha256": record["artifact"]["sha256"], "confirmed": True}:
                    _fault("preparation_consent_required")
                if record["artifact"]["version"] != __version__:
                    _fault("protocol_mismatch")
                raw = bundle._archive_bytes(Path(record["artifact"]["path"]))
                record["phase"] = "preparing"
                save_json(path, record)
                record["facts"] = _remote(record, "prepare", archive=base64.b64encode(raw).decode())
            if record["phase"] == "preparing":
                record["facts"] = _remote(record, "inspect")
                if record["facts"]["worker_status"] == "available":
                    _probe(record, config_path)
                    record["phase"] = "prepared"
            if action == "project" and record["phase"] in {"prepared", "project_ready"}:
                record["phase"] = "prepared"
                record.pop("project", None)
                if (set(value) != {"name", "root", "commands", "writes"} or not isinstance(value["name"], str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value["name"])
                        or type(value["commands"]) is not bool or type(value["writes"]) is not bool):
                    _fault("invalid_remote_project")
                facts = _remote(record, "project", root=value["root"])
                _probe(record, config_path)
                record["project"] = value | {"root": facts["root"]}
                record["phase"] = "project_ready"
            if action == "save" and record["phase"] == "project_ready":
                if value != {"confirmed": True}:
                    _fault("configuration_consent_required")
                if maintenance._digest(config_path) != record["config_sha256"]:
                    _fault("configuration_changed")
                if _remote(record, "project", root=record["project"]["root"])["root"] != record["project"]["root"]:
                    _fault("remote_project_changed")
                _probe(record, config_path)
                record["phase"] = "saving"
                save_json(path, record)
                target = _target(record)
                try:
                    maintenance.change_machine(config_path, state, "machine-add", identity, record["candidate"]["name"],
                        record["config_sha256"], alias=target.ssh_alias, connection=target.ssh_connection,
                        worker=target.ssh_worker, pinned=target.ssh_identity, project=record["project"])
                except Fault:
                    if not maintenance._operation_path(state, identity).exists():
                        record["phase"] = "project_ready"
                    raise
            if record["phase"] == "saving" and maintenance._operation_path(state, identity).exists():
                operation = maintenance.read_operation(config_path, state, identity)
                if action == "continue" and operation["status"] == "continuation_required":
                    operation = maintenance.continue_operation(config_path, state, identity)
                record["operation"] = {"status": operation["status"], "reason": operation.get("reason"),
                                       "can_continue": operation["status"] == "continuation_required"}
                if operation["status"] == "completed":
                    record["phase"] = "completed"
                elif operation["status"] in {"rejected", "restored"}:
                    record["phase"] = "save_failed"
            record["reason"] = None
        except (Fault, OSError, TimeoutError, UnicodeError) as error:
            record["reason"] = error.code if isinstance(error, Fault) else "ssh_unavailable"
        save_json(path, record)
        return {"status": "observed", "setup": view(record)}
