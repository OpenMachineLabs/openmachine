"""Cooperative task control and admission; domains retain every original effect."""

from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import PurePosixPath
import re

from openmachine import execution, tasks
from openmachine.foundation.sanitize import Fault, PRE_EFFECT_REQUEST_FAILURES
from openmachine.foundation.state import lease
from openmachine.tasks import effects, records, transitions
from . import task_attention

FILE_METHODS = {"write_workspace_file", "edit_workspace_file", "apply_workspace_patch"}
GITHUB_METHODS = {"github_issue", "github_pull_request", "github_comment", "github_review", "github_merge"}
METHODS = FILE_METHODS | GITHUB_METHODS | {"start_execution", "control_execution", "cancel_execution"}
MARKER = re.compile(r"<!-- (om-task:[A-Za-z0-9][A-Za-z0-9_-]{0,63}:[0-9a-f]{32}) -->")
# These failures are defined before effects by the existing domain owners. Anything
# else is unknown unless an original receipt proves its outcome.
NO_EFFECT = {"integration_disconnected", "integration_connection_changed", "integration_not_found", "integration_store_invalid", "execution_disabled", "workspace_write_disabled", "invalid_command", "invalid_timeout",
             "invalid_access", "invalid_resources", "invalid_head", "invalid_cwd", "context_mismatch",
             "context_unavailable", "path_denied", "legacy_workspace_busy",
             "ambiguous_workspace", "invalid_github_request", "github_not_allowed", "github_self_review",
             "github_object_changed", "github_candidate_changed", "github_identity_changed",
             "github_authentication_required", "github_access_denied",
             "github_not_found", "github_conflict", "github_rejected"}


def _receipt(status: str, facts: dict | None = None, summary: str = "", *, failure_code=None, effect=None) -> dict:
    result = {"status": status, "receipt_sha256": records.digest(facts) if facts is not None else None,
            "summary": summary or f"Original domain outcome: {status}.", "log_offset": None}
    if failure_code is not None:
        result["failure_code"] = failure_code
    if effect is not None:
        result["effect"] = effect
    return result


def _context(view: dict, operation_id: str) -> dict:
    record = view["record"]
    return {"task_id": record["task_id"], "task_revision": record["revision"],
            "plan_revision": record["plan"]["revision"], "continuation_epoch": record["control"]["epoch"],
            "operation_id": operation_id}


def _reference(view: dict) -> dict:
    record = view["record"]
    return {"task_id": record["task_id"], "task_revision": record["revision"],
            "plan_revision": record["plan"]["revision"], "continuation_epoch": record["control"]["epoch"],
            "index_status": view["index_status"], "handoff_id": (record["handoff"] or {}).get("id"),
            "control_revision": record.get("control_revision", record["revision"]),
            "unresolved_operation_ids": [item["id"] for item in record["operations"] if effects.unresolved(item)]}


class TaskAdmission:
    def __init__(self, application):
        self.app = application

    @property
    def principal(self) -> str:
        # The private native-account endpoint is the verified S1 boundary. This
        # is neither a model-supplied identity nor a ChatGPT conversation ID.
        return f"native-uid:{os.getuid()}"

    def _require_foreground_clear(self, view: dict) -> None:
        if any(effects.unresolved(item) for item in view["record"]["operations"]):
            return
        projected = task_attention.project(self.app.task_observer.project(view))
        foreground = projected.get("foreground", {})
        if foreground.get("status") == "attention_required":
            raise Fault("task_attention_required", foreground["summary"],
                        {"task_id": projected["record"]["task_id"], "foreground": foreground},
                        effect="known_no_effect")

    def control(self, request: dict) -> dict:
        request = records.normalize_control(request)
        view = tasks.read(self.app.state, request["task_id"])
        if "record" not in view:
            raise Fault("task_legacy", "Legacy checkpoints are history; conversion is not a control transition.")
        record = view["record"]
        update = {"id": request["update_id"], "request_sha256": records.digest(
            {"method": "control_task", "principal": self.principal, **request})}
        action = request["transition"]["action"]
        if record["last_update"]["id"] != update["id"]:
            transitions.guards(record, request["expected_revision"], request["expected_plan_revision"], request["expected_epoch"])
            if action not in {"pause", "cancel", "release"}:
                contract = request["transition"].get("contract", record["contract"])
                # Reattachment acquires ownership, not permission for a new effect.
                # Keep identity/grant checks; a changed HEAD still requires an
                # explicit plan revision before admission or acceptance.
                self.app._validate_task_scope(
                    {"contract": contract}, check_head=action not in {"claim", "renew", "resume"},
                    check_identity=action not in {"claim", "renew"})
            if action in {"record_decision", "finish", "revise_plan"}:
                view = self.reconcile(view)
                transitions.guards(view["record"], request["expected_revision"], request["expected_plan_revision"], request["expected_epoch"])
                selected = request["transition"].get("evidence", []) if action == "record_decision" else [
                    item["operation_id"] for item in request["transition"].get("acceptance", []) if item["operation_id"]]
                for identity in selected:
                    operation = next((item for item in view["record"]["operations"] if item["id"] == identity), None)
                    if operation is None:
                        raise Fault("task_evidence_invalid", "Acceptance must reference an original task operation.")
                    observed = self._read(operation, verify=True)
                    current, _ = self._observation(operation, observed)
                    if current != operation["result"]:
                        raise Fault("task_evidence_changed", "Selected evidence changed; reconcile its original result before accepting it.")
        scope = self._scope_witness(record, request) if (
            action == "bind_effect_scope" and record["last_update"]["id"] != update["id"]) else None
        view = tasks.update(self.app.state, request["task_id"],
            lambda value, now: self._bind_scope(value, request, scope, now) if action == "bind_effect_scope"
                else transitions.apply_control(value, request, self.principal, now),
            expected_revision=request["expected_revision"], last_update=update,
            require_current=action not in {"pause", "cancel"}, upgrade=True)
        if action == "cancel" and view["record"]["terminal"] is None:
            view = self._cancel_owned(view, request["update_id"])
        return view

    def _scope_witness(self, record: dict, request: dict) -> dict:
        transition = request["transition"]
        operation = next((item for item in record["operations"] if item["id"] == transition["operation_id"]), None)
        if (operation is None or operation["locator"]["kind"] != "github" or operation.get("effect_scope") is not None
                or not effects.unresolved(operation)):
            raise Fault("task_evidence_invalid", "Scope evidence requires an original unscoped GitHub operation.")
        context = {"task_id": record["task_id"], "operation_id": operation["id"]}
        method = transition["method"]
        try:
            arguments = self._normalize(method, transition["arguments"], context)
            if self._fingerprint(method, arguments, operation) != operation["request_sha256"]:
                raise Fault("task_operation_conflict", "Scope proof must reproduce the exact original normalized request fingerprint.")
            locator = self._locator(method, arguments, context)
            if (tasks.operation_key(locator) != tasks.operation_key(operation["locator"])
                    or locator["object_type"] != operation["locator"]["object_type"]):
                raise Fault("task_operation_conflict", "Scope proof must reproduce the exact original normalized request fingerprint.")
            return effects.github_scope(method, arguments)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise Fault("invalid_task_control", "Scope proof requires structurally valid original GitHub arguments.") from error

    def _bind_scope(self, record: dict, request: dict, scope: dict, now_ms: int) -> None:
        transitions.guards(record, request["expected_revision"], request["expected_plan_revision"], request["expected_epoch"])
        transitions.require_owner(record, self.principal, now_ms)
        if record["format"] < 3:
            raise Fault("task_scope_denied", "Explicitly revise historical account and recovery authority before scope enrollment.")
        operation_id = request["transition"]["operation_id"]
        if any(item["operation_id"] == operation_id for item in record.get("effect_scopes", [])):
            raise Fault("task_operation_conflict", "The original request already has immutable scope evidence.")
        operation = next(item for item in record["operations"] if item["id"] == operation_id)
        record.setdefault("effect_scopes", []).append({"id": request["update_id"], "operation_id": operation_id,
            "request_sha256": operation["request_sha256"], "scope": scope,
            "principal_ref": self.principal, "at_ms": now_ms})
        transitions.material(record, now_ms, "decision", request["transition"]["reason"], operation_id=operation_id)
        transitions.reserve(record)

    def _cancel_owned(self, view: dict, update_id: str) -> dict:
        # Stop was committed first. Only original task-owned command identities
        # can receive the existing bounded cancellation; no arbitrary PID signal.
        view = self.reconcile(view)
        if view["record"]["terminal"] is not None:
            return view
        for original in view["record"]["operations"]:
            locator = original["locator"]
            if (locator["kind"] != "execution" or locator["control_seq"] is not None
                    or original["id"] != locator["execution_id"] or not records.pending(original)):
                continue
            operation_id = records.digest(["cancel", view["task_id"], update_id, original["id"]])[:32]
            for attempt in range(2):
                current = tasks.read(self.app.state, view["task_id"])
                if current["record"]["terminal"] is not None:
                    return current
                try:
                    self.run("cancel_execution", {"machine": locator["machine"], "execution_id": locator["execution_id"]},
                             _context(current, operation_id))
                    break
                except Fault as error:
                    # A material original observation may advance the revision
                    # before cancellation admission. Re-read once, with the same
                    # cancellation identity; never retry uncertain domain effects.
                    if error.code != "task_conflict" or attempt:
                        raise
        return self.reconcile(tasks.read(self.app.state, view["task_id"]))

    def _dispatch(self, method: str, arguments: dict, *, supervised: bool = False, binding=None) -> dict:
        if method not in METHODS:
            raise Fault("unsupported_operation", "Task admission applies only to the explicit existing mutation entrypoints.")
        if method in GITHUB_METHODS:
            return self.app._github_call(method.removeprefix("github_"), arguments,
                                         binding=binding, historical_unbound=supervised and binding is None)
        if method == "start_execution" and supervised:
            return self.app.machines.start_execution(**arguments, task_admitted=True)
        return getattr(self.app.machines, method)(**arguments)

    def _locator(self, method: str, arguments: dict, context: dict | None) -> dict | None:
        if method in GITHUB_METHODS:
            match = MARKER.search(arguments.get("body") or "")
            anchor = f"om-task:{context['task_id']}:{context['operation_id']}" if context else match[1] if match else ""
            kind = method.removeprefix("github_")
            number = arguments.get("comment_id" if kind == "comment" else "review_id" if kind == "review" else "number", 0)
            return {"kind": "github", "repository": arguments["repository"], "object_type": kind,
                    "number": number or None, "parent_number": arguments.get("number") or None if kind in {"comment", "review"} else None,
                    "anchor": anchor}
        target = self.app.config.machine(arguments["machine"])
        if method in FILE_METHODS:
            if not arguments.get("change_id"):
                return None
            return {"kind": "change", "machine": arguments["machine"], "target_identity": target.ssh_identity or None,
                    "change_id": arguments["change_id"]}
        return {"kind": "execution", "machine": arguments["machine"], "target_identity": target.ssh_identity or None,
                "execution_id": arguments["execution_id"],
                "control_seq": arguments["expected_control_seq"] + 1 if method == "control_execution" else None}

    def _normalize(self, method: str, arguments: dict, context: dict) -> dict:
        value = deepcopy(arguments)
        if method == "start_execution":
            if value.get("task_id") not in {"", None, context["task_id"]} or value["execution_id"] != context["operation_id"]:
                raise Fault("task_operation_conflict", "Bind the original execution ID and task ID, not another identity.")
            value["task_id"] = context["task_id"]
            keys = execution.validate_start(value["cwd"], value["argv"], value["timeout_seconds"], value["expected_head_sha"],
                value["workspace_access"], value["resource_keys"], value["task_id"], value["termination_grace_seconds"],
                value["tty"], value["terminal_size"])
            value["resource_keys"] = sorted(set(keys))
            value["termination_grace_seconds"] = 5.0 if value["termination_grace_seconds"] is None else float(value["termination_grace_seconds"])
            value["terminal_size"] = (value["terminal_size"] or {"rows": 24, "cols": 80}) if value["tty"] else None
            value["cwd"] = str(PurePosixPath(value["cwd"]))
        elif method in FILE_METHODS:
            if not value.get("change_id") or value["change_id"] != context["operation_id"]:
                raise Fault("task_operation_conflict", "A supervised file effect requires its original change ID as operation_id.")
        elif method in {"control_execution", "cancel_execution"}:
            if value["execution_id"] == context["operation_id"]:
                raise Fault("task_operation_conflict", "A control or cancellation has its own operation ID; retain the original execution ID.")
            if method == "control_execution":
                value.update(execution.control_request(value["expected_control_seq"], value["action"], value["text"], value["terminal_size"]))
        elif method in GITHUB_METHODS and (value.get("body") is not None or value.get("action") == "create"):
            marker = f"<!-- om-task:{context['task_id']}:{context['operation_id']} -->"
            value["body"] = value.get("body") or ""
            if marker not in value["body"]:
                value["body"] += "\n\n" + marker
            if len(value["body"]) > 60000:
                raise Fault("invalid_github_request", "Reserve body space for the stable task operation marker.")
        return value

    def _fingerprint(self, method: str, arguments: dict, operation: dict) -> str:
        return records.digest({"method": method, "arguments": arguments,
            "plan_revision": operation["plan_revision"], "source_sha256": operation["source_sha256"]})

    def _legacy(self, method: str, arguments: dict, locator: dict | None) -> dict:
        if locator is None or method == "cancel_execution":
            return self._dispatch(method, arguments)
        if method in GITHUB_METHODS:
            proposed = {"locator": locator, "effect_scope": effects.github_scope(method, arguments)}
            if tasks.effect_conflicts(self.app.state, {}, proposed):
                raise Fault("task_effect_unresolved", "This GitHub resource overlaps retained uncertain work; preserve its original task and operation.")
        try:
            binding = tasks.operation_binding(self.app.state, tasks.operation_key(locator))
        except Fault as index_error:
            if method not in FILE_METHODS | {"start_execution"}:
                raise
            try:
                self._read({"locator": locator})
            except Fault:
                raise index_error from None
            return self._dispatch(method, arguments, supervised=method == "start_execution")
        if binding is None:
            return self._dispatch(method, arguments)
        if method in FILE_METHODS | {"start_execution"}:
            try:
                self._read({"locator": locator})
            except Fault:
                pass
            else:
                # Native immutable-ID reconciliation cannot create another effect.
                return self._dispatch(method, arguments, supervised=method == "start_execution")
        raise Fault("task_context_required", "This original effect was supervised; retain task_context before a new consequence.")

    def run(self, method: str, arguments: dict, context: dict | None) -> dict:
        if context is None and method == "cancel_execution":
            # Preserve the existing original-ID stop route even while a task
            # admission identity is busy or its canonical evidence is unavailable.
            return self._dispatch(method, arguments)
        if context is not None:
            context = records.normalize_context(context)
            arguments = self._normalize(method, arguments, context)
        locator = self._locator(method, arguments, context)
        if context is not None and locator is None:
            raise Fault("task_operation_conflict", "Supervised effects require a durable original operation identity.")
        if locator is None:
            return self._dispatch(method, arguments)
        key = tasks.operation_key(locator)
        # These coordinate one effect identity, not task progress or a running
        # worker's lifetime. Task locks are never held across domain/network IO.
        names = {"task-effect-" + records.digest(list(key))}
        if method in GITHUB_METHODS:
            # All native writer routes, including bare creation and Issue/PR
            # aliases, share this short lease through result publication. The
            # persistent exclusion below remains field-scoped, not repo-wide.
            names.add("task-github-repository-" + records.digest(locator["repository"].casefold()))
        if context is not None:
            names.add("task-operation-" + context["operation_id"])
        with ExitStack() as locks:
            try:
                for name in sorted(names):
                    locks.enter_context(lease(self.app.state / "locks" / (name + ".lock")))
            except Fault as error:
                if error.code != "resource_busy" or context is not None or method not in FILE_METHODS | {"start_execution"}:
                    raise
                try:
                    self._read({"locator": locator})
                except Fault:
                    raise error from None
                return self._dispatch(method, arguments)
            if context is None:
                return self._legacy(method, arguments, locator)
            view = tasks.read(self.app.state, context["task_id"])
            if "record" not in view:
                raise Fault("task_legacy", "A legacy checkpoint cannot acquire supervision through task_context.")
            record = view["record"]
            operation = next((item for item in record["operations"] if item["id"] == context["operation_id"]), None)
            if operation is not None:
                if (tasks.operation_key(operation["locator"]) != key or operation["locator"].get("control_seq") != locator.get("control_seq")
                        or operation["request_sha256"] != self._fingerprint(method, arguments, operation)):
                    raise Fault("task_operation_conflict", "The original operation ID belongs to different intent; do not replace or replay it.")
                observed = self._read(operation, arguments)
                result, enriched = self._observation(operation, observed)
                current = self._publish(record["task_id"], [(operation["id"], result, enriched)])
                return {**observed, "task": _reference(current), "task_operation_replayed": False}
            transitions.guards(record, context["task_revision"], context["plan_revision"], context["continuation_epoch"], allow_observations=True)
            if method != "cancel_execution":
                binding = tasks.operation_binding(self.app.state, key, context["operation_id"])
                if binding is not None and binding["task_id"] != context["task_id"]:
                    raise Fault("task_operation_conflict", "This original effect belongs to another task; no new association was admitted.")
            if method == "start_execution" or method in FILE_METHODS:
                try:
                    self._read({"locator": locator})
                except Fault as error:
                    if error.code not in {"execution_not_found", "change_not_found"}:
                        raise
                else:
                    raise Fault("task_operation_conflict", "An existing unbound domain operation cannot be adopted as newly admitted task evidence.")
            self._scope(record, method, arguments, locator)
            # Observe only already known identities, without writing through read_task.
            current = self.reconcile(view)
            transitions.guards(current["record"], context["task_revision"], context["plan_revision"], context["continuation_epoch"], allow_observations=True)
            if method != "cancel_execution":
                self._require_foreground_clear(current)
            stage = transitions.stage_for(record)
            operation = {"id": context["operation_id"], "stage_id": stage["id"], "plan_revision": record["plan"]["revision"],
                "epoch": record["control"]["epoch"], "request_sha256": "", "source_sha256": stage["source_sha256"],
                "locator": locator, "admitted_at_ms": 0, "state": "admitted", "observed_at_ms": None, "result": None}
            operation["request_sha256"] = self._fingerprint(method, arguments, operation)
            if method in GITHUB_METHODS:
                if record["format"] >= 3:
                    operation["effect_scope"] = effects.github_scope(method, arguments)
                conflicts = tasks.effect_conflicts(self.app.state, record, operation)
                if conflicts:
                    raise Fault("task_effect_unresolved", "Only overlapping unresolved GitHub resources block this intent.",
                                {"operations": [{"task_id": item["task_id"], "operation_ids": [op["id"] for op in item["operations"]]}
                                                for item in conflicts]})
            admitted = tasks.update(self.app.state, record["task_id"],
                lambda value, now: self._admit(value, operation, context, method, now),
                expected_revision=context["task_revision"], require_current=method != "cancel_execution",
                allow_observations=True, upgrade=True)
            if admitted["index_status"] != "current" and method != "cancel_execution":
                self._publish(record["task_id"], [(operation["id"], _receipt("failed", summary="Not dispatched: derived task index publication failed."), locator)])
                raise Fault("task_index_incomplete", "Intent was retained but not dispatched; rebuild derived metadata before new work.")
            try:
                observed = self._dispatch(method, arguments, supervised=True, binding=locator.get("connection"))
            except (Fault, OSError) as error:
                code = error.code if isinstance(error, Fault) else "domain_io_unknown"
                owner_effect = getattr(error, "effect", None)
                no_effect = owner_effect == "known_no_effect" or (owner_effect is None and method not in GITHUB_METHODS
                    and (code in NO_EFFECT or method in FILE_METHODS and code in PRE_EFFECT_REQUEST_FAILURES))
                if method in GITHUB_METHODS:
                    # Preserve the owner's dispatch boundary. A matching current
                    # snapshot cannot turn a lost reply into acknowledged work.
                    result, enriched = _receipt("failed" if no_effect else "unknown",
                                                summary=f"Domain failure: {code}."), locator
                else:
                    try:
                        observed = self._read(operation, arguments)
                        result, enriched = self._observation(operation, observed)
                    except (Fault, OSError):
                        unstarted = code == "resource_busy" and method == "start_execution" and self._unstarted_local(operation)
                        result, enriched = _receipt("failed" if no_effect or unstarted else "unknown",
                                                    summary=f"Domain failure: {code}."), locator
                if admitted["record"]["format"] >= 4:
                    phase = {"running": "active", "partial": "effect_partial", "unknown": "effect_unknown"}.get(result["status"], "effect_verified")
                    if no_effect and result["status"] == "failed":
                        phase = "known_no_effect"
                    result = dict(result, failure_code=code, effect=phase)
                self._publish(record["task_id"], [(operation["id"], result, enriched)])
                raise
            result, enriched = self._observation(operation, observed)
            current = self._publish(record["task_id"], [(operation["id"], result, enriched)])
            return {**observed, "task": _reference(current)}

    def _scope(self, record: dict, method: str, arguments: dict, locator: dict) -> None:
        contract = record["contract"]
        stopping = method == "cancel_execution"
        if method not in contract["tools"] and not (stopping and record["control"]["stop_intent"] == "cancel"):
            raise Fault("task_scope_denied", "This tool is outside the accepted task contract.")
        if method in GITHUB_METHODS:
            if arguments["repository"].casefold() not in {item.casefold() for item in contract["repositories"]}:
                raise Fault("task_scope_denied", "This repository is outside the accepted task contract.")
            binding = next((item for repo, item in contract.get("github_connections", {}).items()
                            if repo.casefold() == arguments["repository"].casefold()), None)
            if binding is not None:
                if arguments.get("connection_id") and arguments["connection_id"] != binding["connection_id"]:
                    raise Fault("task_scope_denied", "The task is bound to another GitHub connection.")
                locator["connection"] = binding
            elif arguments.get("connection_id") or record["format"] >= 2:
                raise Fault("task_scope_denied", "This task has no accepted binding for the selected account.")
            observed = self.app._github_call("read", dict(action="repository", repository=arguments["repository"]),
                                             binding=binding, historical_unbound=binding is None)
            if observed.get("configured_write") is not True or observed["data"].get("permissions", {}).get("push") is not True:
                raise Fault("github_not_allowed", "The selected task repository has no current verified write grant.")
            return
        machine = arguments["machine"]
        target = self.app.config.machine(machine, write=method == "start_execution" or
                                         method == "control_execution" and arguments["action"] in {"write_stdin", "resize"})
        if (target.ssh_identity or None) != locator["target_identity"]:
            raise Fault("task_scope_denied", "The original target identity changed.")
        if method in {"control_execution", "cancel_execution"}:
            original = next((item for item in record["operations"] if item["id"] == locator["execution_id"]
                and tasks.operation_key(item["locator"]) == tasks.operation_key(locator)), None)
            if original is None:
                raise Fault("task_scope_denied", "Control and cancellation must target an execution originally admitted to this task.")
            observed = self.app.machines.read_execution(machine, locator["execution_id"], max_bytes=1)
            if method == "control_execution" and (observed.get("control") or {}).get("seq") != arguments["expected_control_seq"]:
                raise Fault("task_operation_conflict", "A new task control identity must use the current original-worker sequence.")
            if method == "control_execution" and arguments["action"] in {"write_stdin", "resize"}:
                self.app._validate_task_scope({"contract": contract})
            return
        for accepted in contract["targets"]:
            if accepted["machine"] != machine:
                continue
            name = accepted["workspace"]
            configured = self.app.config.workspaces.get(name)
            if configured is None or configured.machine != machine:
                continue
            if method in FILE_METHODS and arguments["workspace_name"] != name:
                continue
            observed = self.app.machines.read_workspace_file(machine, name, "AGENTS.md", start_line=1, line_count=1)["context"]
            if observed.get("workspace_identity") != accepted["workspace_identity"] or (observed.get("head_sha") or None) != accepted["head_sha"]:
                raise Fault("context_mismatch", "The accepted task workspace or HEAD changed; revise the affected plan before new work.")
            if method == "start_execution":
                cwd, root = PurePosixPath(arguments["cwd"]), PurePosixPath(configured.root)
                if ".." in cwd.parts or not cwd.is_relative_to(root):
                    continue
                selected = self.app.machines.inspect_workspace(machine, name, "files", str(cwd.relative_to(root)), limit=1)
                if not PurePosixPath(selected["path"]).is_relative_to(PurePosixPath(observed["cwd"])):
                    raise Fault("task_scope_denied", "The command cwd resolves outside the accepted task workspace.")
            else:
                self.app.config.workspace_machine(machine)
                if arguments["expected_workspace_identity"] != accepted["workspace_identity"]:
                    raise Fault("context_mismatch", "The file intent does not bind the accepted task workspace.")
            if (arguments["expected_head_sha"] or None) != accepted["head_sha"]:
                raise Fault("context_mismatch", "The intent must retain the accepted task HEAD, including explicit non-Git identity.")
            return
        raise Fault("task_scope_denied", "This target or workspace is outside the accepted task contract.")

    def _admit(self, record: dict, operation: dict, context: dict, method: str, now_ms: int) -> None:
        transitions.guards(record, context["task_revision"], context["plan_revision"], context["continuation_epoch"], allow_observations=True)
        stopping = method == "cancel_execution"
        if not stopping:
            transitions.require_owner(record, self.principal, now_ms)
        elif record["terminal"] is not None:
            raise Fault("task_terminal", "Terminal history cannot admit another effect.")
        if any(item["id"] == operation["id"] for item in record["operations"]):
            raise Fault("task_operation_conflict", "The operation appeared concurrently; observe its original identity.")
        stage = transitions.stage_for(record)
        if not stopping and (stage["state"] == "accepted" or any(transitions.stage_for(record, key)["state"] != "accepted" for key in stage["depends_on"])):
            raise Fault("task_stage_conflict", "Admit only a remaining stage whose dependencies were accepted.")
        outstanding = [item for item in record["operations"] if effects.unresolved(item)]
        for item in outstanding:
            locator = item["locator"]
            same_live = (method == "control_execution" and locator["kind"] == "execution"
                and locator["control_seq"] is None and item["id"] == locator["execution_id"]
                and tasks.operation_key(locator) == tasks.operation_key(operation["locator"])
                and item["result"] is not None and item["result"]["status"] == "running")
            same_live |= stopping and locator["kind"] == "execution" and tasks.operation_key(locator) == tasks.operation_key(operation["locator"])
            if not same_live and effects.conflicts(record, item, record, operation):
                raise Fault("task_effect_unresolved", "Reconcile the original outstanding effect before admitting overlapping work.")
        operation = dict(operation, admitted_at_ms=now_ms)
        record["operations"].append(operation)
        if not stopping:
            stage["state"] = "running"
            record["control"].update(state="running", pending_decision=None)
            record["handoff"] = None
        if stopping:
            if len(record["operations"]) > 64 or len(records.canonical(record)) > records.MAX_RECORD - 6144:
                raise Fault("task_capacity", "Use the original-ID cancellation route; no room remains for another associated intent.")
        else:
            transitions.reserve(record, operation_limit=63 if method == "control_execution" else 62, new_operation=operation)

    def _read(self, operation: dict, arguments: dict | None = None, *, verify: bool = False, cancelled=None) -> dict:
        locator = operation["locator"]
        if locator["kind"] == "github":
            if operation.get("result") is not None and not records.pending(operation):
                # A terminal provider receipt proves historical effect. Fresh
                # merge/state gates belong to the subsequent owner operation.
                return {"status": "retained_task_evidence", "locator": locator,
                        "result": operation["result"], "current_state_observed": False}
            observed = self.app._github_call("reconcile", dict(locator=locator, request=arguments),
                                             binding=locator.get("connection"), historical_unbound="connection" not in locator)
            return observed
        target = self.app.config.machine(locator["machine"])
        if (target.ssh_identity or None) != locator["target_identity"]:
            raise Fault("task_target_changed", "The original target identity no longer matches; do not follow another host.")
        if locator["kind"] == "change":
            observed = self.app.machines.read_workspace_change(locator["machine"], locator["change_id"], observe_current=verify, cancelled=cancelled)
            if verify:
                current, plan = observed.get("current") or {}, observed.get("plan") or {}
                if observed.get("format") == 2:
                    expected = {item["path"]: item["after_sha256"] for item in plan.get("files", [])}
                    actual = {item["path"]: item.get("sha256") for item in current.get("files", [])}
                    matches = bool(expected) and current.get("complete") is True and expected == actual
                else:
                    matches = current.get("sha256") == plan.get("after_sha256") and "error" not in current and bool(plan)
                if not matches:
                    raise Fault("task_evidence_changed", "Current files differ from original accepted change evidence.")
            return observed
        observed = self.app.machines.read_execution(locator["machine"], locator["execution_id"], max_bytes=1, cancelled=cancelled)
        if locator["control_seq"] is not None:
            last = (observed.get("control") or {}).get("last")
            if last is None or last["seq"] != locator["control_seq"]:
                if operation["result"] is not None and not records.pending(operation):
                    return {"status": "retained_task_evidence", "execution_id": locator["execution_id"], "control_result": None}
                raise Fault("task_evidence_unavailable", "The original control receipt is no longer retained; never resend its input.")
            return {**observed, "control_result": last}
        return observed

    def _observation(self, operation: dict, observed: dict) -> tuple[dict, dict]:
        locator = operation["locator"]
        status = observed.get("status")
        if status == "retained_task_evidence":
            return operation["result"], locator
        if locator["kind"] == "github":
            if operation.get("result") is not None and not records.pending(operation):
                return operation["result"], locator
            if status not in {"verified", "merged_verified"} and operation.get("result") is not None:
                # An unproven mutable destination snapshot cannot rewrite the
                # original uncertainty or discard its retained failure phase.
                return operation["result"], locator
            receipt = observed.get("receipt") or {}
            if locator["object_type"] == "merge":
                receipt = {key: (receipt or observed).get(key) for key in ("number", "head_sha", "merge_commit_sha")}
            number = receipt.get("number" if locator["object_type"] in {"issue", "pull_request", "merge"} else "id")
            enriched = dict(locator, number=number) if type(number) is int and number > 0 else locator
            if status in {"verified", "merged_verified"}:
                return _receipt("succeeded", receipt), enriched
            if status == "observed" and operation["result"] is not None and not records.pending(operation):
                return operation["result"], enriched
            if status == "merge_blocked":
                return _receipt("failed", summary="Original merge was blocked before dispatch."), enriched
            result = _receipt("unknown", receipt or None, "Original provider outcome is unresolved; no request was repeated.")
            if operation.get("effect_scope") is not None:
                result["effect"] = "effect_unknown"
                code = observed.get("error_code")
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", code):
                    result["failure_code"] = code
            return result, enriched
        if locator["kind"] == "change":
            outcome = {"applied": "succeeded", "unchanged": "succeeded", "rejected": "failed", "partial": "partial"}.get(status, "unknown")
            facts = {key: observed.get(key) for key in ("change_id", "request_sha256", "revision", "status", "plan_sha256", "effects", "error")}
            return _receipt(outcome, facts), locator
        if locator["control_seq"] is not None:
            receipt = observed["control_result"]
            outcome = {"applied": "succeeded", "rejected": "failed", "partial": "partial"}.get(receipt["status"], "unknown")
            return _receipt(outcome, receipt), locator
        outcome = {"accepted": "running", "running": "running", "finishing": "running", "succeeded": "succeeded",
                   "failed": "failed", "timed_out": "failed", "cancelled": "cancelled"}.get(status, "unknown")
        if outcome in {"succeeded", "failed", "cancelled"} and (observed.get("cleanup", {}).get("status") != "verified"
                or outcome == "succeeded" and observed.get("exit_code") != 0):
            outcome = "unknown"
        facts = {key: observed.get(key) for key in ("execution_id", "created_at", "status", "exit_code", "command", "stop", "cleanup", "error")}
        result = _receipt(outcome, facts)
        result["log_offset"] = (operation["result"] or {}).get("log_offset") or 0
        return result, locator

    def _unstarted_local(self, operation: dict) -> bool:
        # A missing receipt alone never proves non-execution. Only the recorded
        # local start rejection and its original free launch lock can do so.
        locator = operation["locator"]
        if (locator["kind"] != "execution" or locator["control_seq"] is not None
                or locator["target_identity"] is not None or operation["id"] != locator["execution_id"]
                or self.app.config.machine(locator["machine"]).transport != "local"):
            return False
        identity = locator["execution_id"]
        try:
            with lease(self.app.state / "locks" / f"{identity}.lock", create=False):
                return not os.path.lexists(self.app.state / "executions" / identity)
        except (Fault, OSError):
            return False

    def reconcile(self, view: dict) -> dict:
        record = view["record"]
        if record["terminal"] is not None:
            return view
        changes = []
        for operation in record["operations"]:
            if not records.pending(operation):
                continue
            try:
                observed = self._read(operation)
            except Fault as error:
                result = operation.get("result") or {}
                if (error.code == "execution_not_found" and result.get("status") == "unknown"
                        and result.get("summary") == "Domain failure: resource_busy."
                        and self._unstarted_local(operation)):
                    changes.append((operation["id"],
                        _receipt("failed", summary="Original resource_busy confirmed before local launch; no execution created."),
                        operation["locator"]))
                continue
            except OSError:
                continue
            result, locator = self._observation(operation, observed)
            changes.append((operation["id"], result, locator))
        return self._publish(record["task_id"], changes) if changes else view

    def apply_observations(self, record: dict, now_ms: int, observations: list[tuple[str, dict, dict]]) -> None:
        """Pure result/handoff transition shared with the revision-checked observer."""
        if record["terminal"] is not None:
            return
        changed = []
        for operation_id, result, locator in observations:
            operation = next((item for item in record["operations"] if item["id"] == operation_id), None)
            if operation is None or tasks.operation_key(operation["locator"]) != tasks.operation_key(locator):
                raise Fault("task_operation_conflict", "Original observation binding changed; preserve its domain receipt.")
            if operation["result"] == result and operation["locator"] == locator:
                continue
            if operation["locator"]["kind"] == "github" and operation["result"] is not None and not records.pending(operation):
                continue
            # A transient unavailable read never erases a verified historical outcome.
            if operation["result"] is not None and not records.pending(operation) and result["status"] in {"unknown", "running"}:
                continue
            operation.update(result=result, locator=locator, state="unknown" if result["status"] == "unknown" else "observed",
                             observed_at_ms=now_ms)
            changed.append(operation)
        if not changed:
            return
        recovery = record["recovery"]
        if recovery is not None and recovery["category"] in {"local_observation", "ssh_observation"} and not any(
                records.pending(item) and item["locator"]["kind"] in {"execution", "change"} for item in record["operations"]):
            record["recovery"] = None
        control = record["control"]
        if control["stop_intent"] == "cancel" and not any(effects.unresolved(item) for item in record["operations"]):
            transitions.finish_record(record, "cancelled", [], "Explicit stop; original operation outcomes are retained separately.", now_ms)
        elif control["stop_intent"] == "none":
            stage = transitions.stage_for(record)
            decisive = next((item for item in reversed(changed) if item["result"]["status"] != "running"
                and item["stage_id"] == stage["id"] and item["plan_revision"] == record["plan"]["revision"]
                and item["source_sha256"] == stage["source_sha256"]), None)
            if decisive is not None:
                control["state"] = "waiting_for_ai"
                control["pending_decision"] = None if decisive["result"]["status"] == "succeeded" else "Review the original operation outcome before new work."
                transitions.handoff(record, decisive["id"], now_ms,
                                "result_ready" if decisive["result"]["status"] == "succeeded" else "decision_required")

    def _publish(self, identity: str, observations: list[tuple[str, dict, dict]]) -> dict:
        try:
            view = tasks.update(self.app.state, identity, lambda value, now: self.apply_observations(value, now, observations), observation=True)
        except (Fault, OSError) as error:
            raise Fault("task_result_unverified", "Original domain work may have finished, but task evidence publication was not verified. Read the same task and original operation; do not repeat its effect.",
                        {"task_id": identity, "operation_ids": [item[0] for item in observations]}) from error
        if view["status"] == "saved":
            self.app.task_observer.notify()
        return view
