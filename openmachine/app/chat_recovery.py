"""Observer-owned, bounded same-Chat recovery; business work remains with Chat."""

import os
import re
import threading
import uuid

import integrations.chatgpt as chatgpt
from openmachine import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from openmachine.tasks import records, transitions
from . import task_observation as observation


def _authority(record: dict) -> str:
    # A reconciled commit is ordinary progress, not a new business grant.
    contract = dict(record["contract"])
    contract["targets"] = [{key: value for key, value in target.items() if key != "head_sha"}
                           for target in contract["targets"]]
    return records.digest(contract)


def _progress(record: dict) -> int:
    accepted = {stage["id"] for stage in record["plan"]["stages"] if stage["state"] == "accepted"}
    times = [record["created_at_ms"], *(item["admitted_at_ms"] for item in record["operations"])]
    times.extend(item["at_ms"] for item in record["history"]
                 if item["kind"] == "decision" and item["stage_id"] in accepted)
    if record["terminal"] is not None:
        times.append(record["terminal"]["finished_at_ms"])
    return max(times)


class ChatRecovery:
    def __init__(self, application):
        self.app = application
        self._stop = threading.Event()
        self._stop.set()
        self._thread = None
        self._not_before = 0
        self._next_tick = 0
        self._blockers: dict[str, str] = {}
        self._failure = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.busy:
            raise Fault("chat_recovery_busy", "The original recovery reader must exit before replacement.")
        self._not_before = chatgpt.now_ms() + chatgpt.GRACE_MS
        self._next_tick = 0
        self._stop.clear()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        # TaskObserver owns the coordination lease and drains this slot on stop.
        if self._stop.is_set() or self.busy or chatgpt.now_ms() < self._next_tick:
            return
        self._next_tick = chatgpt.now_ms() + chatgpt.INTERVAL_MS
        self._thread = threading.Thread(target=self._scan, name="om-chat-recovery", daemon=True)
        self._thread.start()

    def _quiet(self, binding) -> None:
        if self._stop.is_set():
            raise Fault("chat_recovery_stopped", "The owning observer is not admitting recovery.")
        if chatgpt.now_ms() < max(self._not_before, binding.activity_at_ms + chatgpt.GRACE_MS):
            raise Fault("chat_recovery_caller_active", "The observation grace has not elapsed.")

    def _observe(self, record: dict, binding, *, activity: bool = False) -> None:
        delivery = binding.delivery
        owner = record["control"]["owner"]
        claimed = bool(delivery and owner and owner["claim_id"] == delivery.claim_update_id)
        epoch = record["control"]["epoch"] if claimed else None
        progress = None
        if delivery and delivery.continuation_at_ms is None and _progress(record) > (
                delivery.progress_at_ms if delivery.progress_at_ms is not None else delivery.created_at_ms):
            claim_epoch = epoch if claimed else delivery.claim_epoch
            intents = [item["admitted_at_ms"] for item in record["operations"]
                       if item["epoch"] == claim_epoch and item["admitted_at_ms"] >= delivery.created_at_ms]
            after_claim = delivery.claim_at_ms is not None and _progress(record) >= delivery.claim_at_ms
            attributed = bool(intents) or after_claim and (claimed or (claim_epoch is not None
                and record["terminal"] is not None and record["control"]["epoch"] == claim_epoch + 1))
            progress = (min(intents) if intents else _progress(record), "recovery" if attributed else "manual")
        if activity or epoch is not None or progress is not None:
            with self.app._admit():
                chatgpt.observe(self.app.state, record["task_id"], activity=activity,
                    recovery_id=delivery.recovery_id if delivery else "", claim_epoch=epoch, progress=progress)

    def external_activity(self, name: str, arguments: dict, payload: dict) -> None:
        """Called only for successful external MCP calls, never internal reads."""
        if name == "configure_chat_recovery":
            return  # Configuration owns its timestamp and exact retry semantics.
        try:
            record = payload.get("record")
            identity = (record or {}).get("task_id") or (payload.get("task") or {}).get("task_id") or payload.get("task_id")
            if identity:
                binding = chatgpt.get(self.app.state, identity)
                if binding:
                    self._observe(record or tasks.read(self.app.state, identity)["record"], binding, activity=True)
            elif name in {"read_execution", "read_workspace_change", "read_codex_task"}:
                key = "change_id" if name == "read_workspace_change" else "execution_id"
                for binding in chatgpt.bindings(self.app.state):
                    record = tasks.read(self.app.state, binding.task_id)["record"]
                    if any(item["locator"].get(key) == arguments.get(key)
                           and item["locator"]["machine"] == arguments.get("machine") for item in record["operations"]):
                        self._observe(record, binding, activity=True)
                        break
        except Exception as error:
            # Do not turn an already-completed business effect into an apparent
            # failure. Failed activity persistence conservatively delays dispatch.
            self._failure = observation.error_code(error)
            self._not_before = chatgpt.now_ms() + chatgpt.GRACE_MS

    def _scan(self) -> None:
        try:
            retained = chatgpt.bindings(self.app.state)
            self._blockers = {key: value for key, value in self._blockers.items()
                              if any(item.task_id == key for item in retained)}
            self._failure = None
            # Original reservations are observed even after pause/disable/expiry.
            for binding in retained:
                if self._stop.is_set():
                    return
                if binding.delivery and binding.delivery.status == "reserved":
                    result = self.read_delivery(binding.task_id)
                    if result["chat_recovery"]["delivery"]["status"] == "reserved":
                        return  # Never overlap another native browser execution.
            for previous in retained:
                if self._stop.is_set():
                    return
                identity = previous.task_id
                try:
                    binding = chatgpt.get(self.app.state, identity)
                    record = tasks.read(self.app.state, identity)["record"]
                    self._observe(record, binding)
                    if binding.disabled or not binding.chat_url or record["terminal"] is not None:
                        continue
                    delivery = binding.delivery
                    if delivery and (delivery.status == "outcome_unknown" or
                            _progress(record) <= (delivery.progress_at_ms if delivery.progress_at_ms is not None else delivery.created_at_ms)
                            and delivery.status != "deferred"):
                        continue
                    self._quiet(binding)
                    result = self.start_delivery(identity, uuid.uuid4().hex, uuid.uuid4().hex, automatic=True)
                    self._blockers.pop(identity, None)
                    if result["chat_recovery"]["delivery"]["status"] == "reserved":
                        return
                except (Fault, OSError) as error:
                    self._blockers[identity] = observation.error_code(error)
        except (Fault, OSError) as error:
            self._failure = observation.error_code(error)
        except Exception:
            self._failure = "chat_recovery_internal_error"
            self._stop.set()  # Preserve the original slot/evidence; never retry a crash blindly.

    def capabilities(self) -> dict:
        return {
            "supported": True,
            "default_enabled": True,
            "automatic_detection": True,
            "interval_ms": chatgpt.INTERVAL_MS,
            "activity_grace_ms": chatgpt.GRACE_MS,
            "max_attempts": chatgpt.MAX_ATTEMPTS,
            "browser": "chrome",
            "state_format": chatgpt.FORMAT,
            "max_active_bindings": chatgpt.MAX_ACTIVE,
            "max_retained_bindings": chatgpt.MAX_BINDINGS,
        }

    def project(self, view: dict) -> dict:
        if "record" not in view:
            return {**view, "chat_recovery": {
                "eligible": False, "default_enabled": True, "state": "legacy_ineligible",
                "binding_revision": 0, "disabled": False, "bound": False, "delivery": None}}
        record = view["record"]
        binding = chatgpt.get(self.app.state, record["task_id"])
        public = chatgpt.public(binding)
        now = chatgpt.now_ms()
        authority = _authority(record)
        eligible = (record["terminal"] is None and record["control"]["stop_intent"] == "none"
                    and now < record["contract"]["expires_at_ms"])
        if record["terminal"] is not None:
            state = "terminal"
        elif record["control"]["stop_intent"] != "none":
            state = "paused" if record["control"]["stop_intent"] == "pause" else "cancelling"
        elif record["contract"]["expires_at_ms"] <= now:
            state = "expired"
        elif binding is not None and binding.disabled:
            state = "disabled"
        elif binding is None or binding.chat_url is None:
            state = "waiting_for_binding"
        elif binding.authority_sha256 not in {authority, records.digest(record["contract"])}:
            state = "stale_binding"
        elif (binding.expires_at_ms or 0) <= now:
            state = "expired"
        else:
            state = "bound"
        delivery = binding.delivery if binding else None
        progress_state = None
        blocker = self._blockers.get(record["task_id"]) or self._failure
        if delivery:
            progress_state = delivery.status
            if delivery.continuation_at_ms is not None:
                progress_state = "continuation_observed" if delivery.continuation == "recovery" else "superseded"
            elif delivery.claim_at_ms is not None:
                progress_state = "claim_observed"
            if (delivery.sent_at_ms is not None and delivery.continuation_at_ms is None
                    and now >= min(delivery.sent_at_ms + chatgpt.CONTINUATION_MS, delivery.deadline_ms)):
                blocker = "continuation_not_observed"
        return {**view, "chat_recovery": {
            "eligible": eligible,
            "default_enabled": True,
            "state": state,
            "delivery_state": progress_state,
            "observer_running": not self._stop.is_set(),
            "observation_grace_until_ms": self._not_before or None,
            "blocker": blocker,
            **public,
        }}

    def configure(self, task_id: str, update_id: str, expected_revision: int, change: dict) -> dict:
        view = tasks.read(self.app.state, task_id)
        if "record" not in view:
            raise Fault("task_legacy", "Legacy checkpoints are not eligible for automatic Chat recovery.")
        record = view["record"]
        action = change.get("action")
        request_sha256 = records.digest({
            "method": "configure_chat_recovery",
            "task_id": task_id,
            "expected_revision": expected_revision,
            "change": change,
        })
        if action == "disable":
            with self.app._admit():
                status = chatgpt.configure(self.app.state, task_id=task_id, update_id=update_id,
                    expected_revision=expected_revision, action=action, request_sha256=request_sha256)
            return {"task_id": task_id, "default_enabled": True, "state": "disabled", **status}
        if action != "enable":
            raise Fault("invalid_chat_recovery_change", "Choose enable or disable.")
        now = chatgpt.now_ms()
        if record["terminal"] is not None or now >= record["contract"]["expires_at_ms"]:
            raise Fault("task_stopped", "Terminal or expired task authority cannot establish a recovery binding.")
        authority_ref = change.get("authorization_ref")
        if authority_ref not in record["contract"]["authority_refs"]:
            raise Fault("chat_recovery_authority_invalid",
                        "Recovery authorization must reference the original accepted task authority.")
        expires = change.get("expires_at_ms")
        if type(expires) is not int or not now < expires <= record["contract"]["expires_at_ms"]:
            raise Fault("chat_recovery_authority_invalid",
                        "Recovery expiry must be future and no later than original task authority.")
        machine, workspace_name = change.get("machine"), change.get("workspace")
        selected = self.app.config.workspaces.get(workspace_name)
        if selected is None or selected.machine != machine:
            raise Fault("chat_recovery_scope_invalid",
                        "Recovery must use one configured controller workspace on its configured machine.")
        target = self.app.config.machine(machine, write=True)
        if target.transport != "local":
            raise Fault("chat_recovery_scope_invalid", "Same-Chat recovery uses a configured local controller.")
        try:
            chat_url, _ = chatgpt.conversation(change.get("chat_url", ""))
        except (ValueError, TypeError):
            raise Fault("invalid_chat_recovery_binding",
                        "Bind one exact authenticated ordinary https://chatgpt.com/c/<conversation> URL.") from None
        context = self.app.machines.read_workspace_file(
            machine, workspace_name, "AGENTS.md", start_line=1, line_count=1)["context"]
        workspace_identity = context.get("workspace_identity")
        if not isinstance(workspace_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", workspace_identity):
            raise Fault("context_unavailable", "The controller workspace identity could not be verified.")
        authority = _authority(record)
        with self.app._admit():
            status = chatgpt.configure(self.app.state, task_id=task_id, update_id=update_id,
                expected_revision=expected_revision, action=action, request_sha256=request_sha256,
                chat_url=chat_url, machine=machine, workspace=workspace_name,
                workspace_identity=workspace_identity, authority_ref=authority_ref,
                authority_sha256=authority, expires_at_ms=expires)
        return {"task_id": task_id, "default_enabled": True, "state": "bound", **status}

    def _task(self, task_id: str, *, automatic: bool = False) -> tuple[dict, bool]:
        view = tasks.read(self.app.state, task_id)
        if "record" not in view:
            raise Fault("task_legacy", "Legacy checkpoints are not eligible for Chat recovery.")
        record = view["record"]
        now = chatgpt.now_ms()
        if record["terminal"] is not None or record["control"]["stop_intent"] != "none":
            raise Fault("task_stopped", "Paused, cancelling or terminal work cannot be woken automatically.")
        if now >= record["contract"]["expires_at_ms"]:
            raise Fault("task_authority_expired", "Expired task authority cannot be extended by Chat recovery.")
        owner = record["control"]["owner"]
        if owner is not None and owner["expires_at_ms"] > now:
            raise Fault("chat_recovery_caller_active",
                        "An unexpired continuation claim already owns this task; do not send a recovery message.")
        reconciliation_only = False
        if record["recovery"] and observation.blocked(record["recovery"], now):
            raise Fault("chat_recovery_original_blocked", "The original observation budget or identity guard blocks recovery.")
        for operation in record["operations"]:
            if automatic and self._stop.is_set():
                raise Fault("chat_recovery_stopped", "The original observer stopped before dispatch.")
            result = operation["result"]
            if records.pending(operation):
                try:
                    observed = self.app.task_admission._read(operation,
                        cancelled=self._stop.is_set if automatic else None)
                    result, _ = self.app.task_admission._observation(operation, observed)
                except Fault as error:
                    if error.code not in observation.TRANSIENT | observation.SSH_TRANSIENT:
                        raise
                    result = {"status": "unknown"}
            if result is not None and result["status"] == "running":
                raise Fault("chat_recovery_work_running",
                            "Original work is still running or awaiting its authoritative receipt.")
            reconciliation_only |= result is None or result["status"] == "unknown"
        return view, reconciliation_only

    def _ensure_handoff(self, view: dict, recovery_id: str) -> dict:
        record = view["record"]
        if record["handoff"] is not None:
            return view
        def publish(value, now):
            if value["terminal"] is not None or value["control"]["stop_intent"] != "none":
                raise Fault("task_stopped", "Stopped work cannot acquire a recovery handoff.")
            if now >= value["contract"]["expires_at_ms"]:
                raise Fault("task_authority_expired", "Expired work cannot acquire a recovery handoff.")
            owner = value["control"]["owner"]
            if owner is not None and owner["expires_at_ms"] > now:
                raise Fault("chat_recovery_caller_active", "A live claim already owns task continuation.")
            transitions.handoff(value, recovery_id, now, "decision_required")
            if value["handoff"] is None:
                raise Fault("task_handoff_conflict", "No current task stage can be handed back to Chat.")
            value["control"]["state"] = "waiting_for_ai"
        with self.app._admit():
            return tasks.update(self.app.state, record["task_id"], publish,
                                expected_revision=record["revision"])

    def _binding(self, record: dict):
        binding = chatgpt.get(self.app.state, record["task_id"])
        now = chatgpt.now_ms()
        authority = _authority(record)
        if (binding is None or binding.disabled or binding.chat_url is None
                or binding.authority_sha256 not in {authority, records.digest(record["contract"])}
                or (binding.expires_at_ms or 0) <= now):
            raise Fault("chat_recovery_binding_unavailable",
                        "The default recovery policy is active but this task has no current valid Chat binding.")
        selected = self.app.config.workspaces.get(binding.workspace)
        if selected is None or selected.machine != binding.machine:
            raise Fault("chat_recovery_scope_invalid", "The retained controller workspace is no longer configured.")
        if self.app.config.machine(binding.machine, write=True).transport != "local":
            raise Fault("chat_recovery_scope_invalid", "The retained recovery controller is no longer local.")
        context = self.app.machines.read_workspace_file(
            binding.machine, binding.workspace, "AGENTS.md", start_line=1, line_count=1)["context"]
        if context.get("workspace_identity") != binding.workspace_identity:
            raise Fault("chat_recovery_scope_changed",
                        "The retained controller workspace identity changed; revalidate the recovery binding.")
        self.app._validate_task_scope({"contract": record["contract"]}, check_head=False)
        return binding, binding.authority_sha256

    def start_delivery(self, task_id: str, recovery_id: str, execution_id: str, *, automatic: bool = False) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", recovery_id) or not re.fullmatch(r"[0-9a-f]{32}", execution_id):
            raise Fault("invalid_chat_recovery_delivery", "Use bounded original recovery and execution IDs.")
        view, reconciliation_only = self._task(task_id, automatic=automatic)
        binding, authority = self._binding(view["record"])
        if automatic:
            self._quiet(binding)
        view = self._ensure_handoff(view, recovery_id)
        record = view["record"]
        handoff = record["handoff"]
        now = chatgpt.now_ms()
        if handoff["expires_at_ms"] <= now:
            raise Fault("task_handoff_conflict", "The current continuation handoff expired before delivery.")
        deadline = min(now + chatgpt.EPISODE_MS, binding.expires_at_ms, record["contract"]["expires_at_ms"])
        with self.app._admit():
            reserved, reused = chatgpt.reserve_delivery(self.app.state, task_id=task_id,
                authority_sha256=authority, handoff_id=handoff["id"], recovery_id=recovery_id,
                execution_id=execution_id, deadline_ms=deadline, progress_at_ms=_progress(record),
                binding_revision=binding.revision, quiet_before_ms=now - chatgpt.GRACE_MS if automatic else None)
        if reused:
            return self.read_delivery(task_id)
        recovery_id = reserved["delivery"]["recovery_id"]
        deadline = min(deadline, reserved["delivery"]["deadline_ms"])
        message = chatgpt.recovery_message(task_id, recovery_id, reconciliation_only=reconciliation_only)
        goal = chatgpt.browser_goal(binding.chat_url, message, recovery_id)
        launch_attempted = False
        try:
            # Revalidate under lifecycle admission immediately before this narrow dispatch.
            with self.app._admit():
                current, current_reconciliation = self._task(task_id, automatic=automatic)
                current_binding, current_authority = self._binding(current["record"])
                if automatic:
                    self._quiet(current_binding)
                if (current["record"]["handoff"] is None
                        or current["record"]["handoff"]["id"] != handoff["id"]
                        or current_authority != authority
                        or current_reconciliation != reconciliation_only
                        or current_binding.revision != binding.revision):
                    raise Fault("chat_recovery_conflict",
                                "Task or recovery authority changed before browser dispatch.")
                def launch(**arguments):
                    # Codex's generic seam accepts task_context; this private delivery is
                    # deliberately outside business-task admission.
                    nonlocal launch_attempted
                    arguments.pop("task_context", None)
                    launch_attempted = True
                    return self.app.machines.start_execution(**arguments)
                timeout = min(chatgpt.BROWSER_SECONDS, (deadline - chatgpt.now_ms()) // 1000)
                if timeout < 1:
                    raise Fault("task_authority_expired", "Recovery authority cannot cover a browser attempt.")
                started = self.app.codex.start(self.app.config, self.app.state,
                    launch, binding.machine, execution_id,
                    self.app.config.workspaces[binding.workspace].root, goal, binding.chat_url,
                    [chatgpt.ORIGIN], "browser", "chrome", timeout, "", None)
        except Fault as error:
            unsent = not launch_attempted or error.code == "execution_disabled"
            if error.code == "resource_busy":
                # A rejected local start is unsent only with its original launch
                # lock free and no execution directory; absence alone is not proof.
                try:
                    with lease(self.app.state / "locks" / f"{execution_id}.lock", create=False):
                        unsent = not os.path.lexists(self.app.state / "executions" / execution_id)
                except (Fault, OSError):
                    pass  # Preserve uncertainty and the original failure.
            if unsent:
                with self.app._admit():
                    chatgpt.release_reservation(self.app.state, task_id, execution_id)
            raise
        return {"task_id": task_id, "chat_recovery": reserved,
                "delivery_execution_id": execution_id, "execution_status": started["status"]}

    def read_delivery(self, task_id: str) -> dict:
        binding = chatgpt.get(self.app.state, task_id)
        if binding is None or binding.delivery is None:
            raise Fault("chat_recovery_delivery_not_found",
                        "This task has no retained recovery delivery to observe.")
        delivery = binding.delivery
        if delivery.status != "reserved":
            return {"task_id": task_id, "chat_recovery": chatgpt.public(binding),
                    "delivery_execution_id": delivery.execution_id,
                    "execution_status": delivery.status}
        try:
            result = self.app.codex.read(self.app.config, self.app.state,
                self.app.machines.read_execution, binding.machine, delivery.execution_id)
        except Fault as error:
            if error.code != "execution_not_found":
                raise
            result = {"status": "outcome_unknown"}  # Missing alone never proves no launch.
        status = result["status"]
        if status in {"accepted", "preparing", "running", "stopping", "finishing"}:
            return {"task_id": task_id, "chat_recovery": chatgpt.public(binding),
                    "delivery_execution_id": delivery.execution_id, "execution_status": status}
        observed, reason = chatgpt.delivery_outcome(result, binding)
        with self.app._admit():
            public = chatgpt.mark_delivery(self.app.state, task_id, delivery.execution_id, observed, reason)
        return {"task_id": task_id, "chat_recovery": public,
                "delivery_execution_id": delivery.execution_id, "execution_status": status}
