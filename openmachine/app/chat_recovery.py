"""Task-aware same-Chat recovery delivery; automatic scheduling is a later slice."""

import os
import re

import integrations.chatgpt as chatgpt
from openmachine import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from openmachine.tasks import records, transitions


class ChatRecovery:
    def __init__(self, application):
        self.app = application

    def capabilities(self) -> dict:
        return {
            "supported": True,
            "default_enabled": True,
            "automatic_detection": False,
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
        authority = records.digest(record["contract"])
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
        elif binding.authority_sha256 != authority:
            state = "stale_binding"
        elif (binding.expires_at_ms or 0) <= now:
            state = "expired"
        else:
            state = "bound"
        return {**view, "chat_recovery": {
            "eligible": eligible,
            "default_enabled": True,
            "state": state,
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
        authority = records.digest(record["contract"])
        with self.app._admit():
            status = chatgpt.configure(self.app.state, task_id=task_id, update_id=update_id,
                expected_revision=expected_revision, action=action, request_sha256=request_sha256,
                chat_url=chat_url, machine=machine, workspace=workspace_name,
                workspace_identity=workspace_identity, authority_ref=authority_ref,
                authority_sha256=authority, expires_at_ms=expires)
        return {"task_id": task_id, "default_enabled": True, "state": "bound", **status}

    def _task(self, task_id: str) -> tuple[dict, bool]:
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
        for operation in record["operations"]:
            result = operation["result"]
            if operation["state"] != "observed" or result is None or result["status"] == "running":
                raise Fault("chat_recovery_work_running",
                            "Original work is still running or awaiting its authoritative receipt.")
            reconciliation_only |= result["status"] == "unknown"
        return view, reconciliation_only

    def _ensure_handoff(self, view: dict, recovery_id: str) -> dict:
        record = view["record"]
        if record["handoff"] is not None:
            return view
        def publish(value, now):
            if value["terminal"] is not None or value["control"]["stop_intent"] != "none":
                raise Fault("task_stopped", "Stopped work cannot acquire a recovery handoff.")
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
        authority = records.digest(record["contract"])
        if (binding is None or binding.disabled or binding.chat_url is None
                or binding.authority_sha256 != authority
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
        return binding, authority

    def start_delivery(self, task_id: str, recovery_id: str, execution_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", recovery_id) or not re.fullmatch(r"[0-9a-f]{32}", execution_id):
            raise Fault("invalid_chat_recovery_delivery", "Use bounded original recovery and execution IDs.")
        view, reconciliation_only = self._task(task_id)
        binding, authority = self._binding(view["record"])
        view = self._ensure_handoff(view, recovery_id)
        record = view["record"]
        handoff = record["handoff"]
        now = chatgpt.now_ms()
        if handoff["expires_at_ms"] <= now:
            raise Fault("task_handoff_conflict", "The current continuation handoff expired before delivery.")
        deadline = min(now + 3600000, binding.expires_at_ms, record["contract"]["expires_at_ms"])
        with self.app._admit():
            reserved, reused = chatgpt.reserve_delivery(self.app.state, task_id=task_id,
                authority_sha256=authority, handoff_id=handoff["id"], recovery_id=recovery_id,
                execution_id=execution_id, deadline_ms=deadline)
        if reused:
            return self.read_delivery(task_id)
        message = chatgpt.recovery_message(task_id, recovery_id, reconciliation_only=reconciliation_only)
        goal = chatgpt.browser_goal(binding.chat_url, message, recovery_id)
        launch_attempted = False
        try:
            # Revalidate under lifecycle admission immediately before this narrow dispatch.
            with self.app._admit():
                current, _ = self._task(task_id)
                current_binding, current_authority = self._binding(current["record"])
                if (current["record"]["handoff"] is None
                        or current["record"]["handoff"]["id"] != handoff["id"]
                        or current_authority != authority
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
        result = self.app.codex.read(self.app.config, self.app.state,
            self.app.machines.read_execution, binding.machine, delivery.execution_id)
        status = result["status"]
        if status == "completed":
            observed = "sent_reported"
        elif status in {"blocked", "needs_confirmation"}:
            observed = "blocked"
        elif status in {"accepted", "preparing", "running", "stopping", "finishing"}:
            return {"task_id": task_id, "chat_recovery": chatgpt.public(binding),
                    "delivery_execution_id": delivery.execution_id, "execution_status": status}
        else:
            observed = "outcome_unknown"
        with self.app._admit():
            public = chatgpt.mark_delivery(self.app.state, task_id, delivery.execution_id, observed)
        return {"task_id": task_id, "chat_recovery": public,
                "delivery_execution_id": delivery.execution_id, "execution_status": status}
