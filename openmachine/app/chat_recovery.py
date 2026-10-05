"""One observer-owned task Chat route; business work remains with Chat."""

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
        self._known: dict[str, str] = {}
        self._failure = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.busy:
            raise Fault("chat_recovery_busy", "The original recovery reader must exit before replacement.")
        self._not_before = chatgpt.now_ms() + chatgpt.GRACE_MS
        self._next_tick = 0
        self._known.clear()
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
        if name == "read_task" and arguments.get("preparation_id") is not None:
            self._observe_preparation(arguments, payload)
            return  # Setup evidence is not a heartbeat or business advancement.
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

    @staticmethod
    def _disabled(record: dict, binding) -> bool:
        return binding.disabled if binding else (
            record.get("chat_registration") is None and record["contract"].get("recovery_policy") == "disabled")

    @staticmethod
    def _active(record: dict) -> None:
        if (record["terminal"] is not None or record["control"]["stop_intent"] != "none"
                or chatgpt.now_ms() >= record["contract"]["expires_at_ms"]):
            raise Fault("task_stopped", "Stopped or expired work cannot prepare or dispatch recovery.")

    def _profile(self):
        route = self.app.config.chat_recovery
        if route is None:
            raise Fault("chat_recovery_controller_unconfigured", "Configure the existing local browser route once.")
        workspace = self.app.config.workspaces[route.workspace]
        fingerprint = records.digest({"route": route.model_dump(), "workspace": workspace.model_dump()})
        return route, workspace, fingerprint

    def _registered(self, record: dict, binding) -> None:
        if binding is None or binding.preparation is None:
            code = "chat_recovery_registration_missing" if record.get("chat_registration") else "chat_recovery_preparing"
            raise Fault(code, "A matching durable recovery registration is required.")
        preparation = binding.preparation
        if record.get("chat_registration") != preparation.preparation_id:
            raise Fault("chat_recovery_registration_conflict", "The task and preparation identities must agree.")
        _, _, fingerprint = self._profile()
        if preparation.controller_sha256 != fingerprint:
            raise Fault("chat_recovery_controller_changed", "Preserve the original destination; the controller configuration changed.")
        if binding.authority_sha256 != _authority(record):
            raise Fault("chat_recovery_authority_changed", "Revalidate changed task authority before new work.")
        if (binding.expires_at_ms or 0) <= chatgpt.now_ms():
            raise Fault("chat_recovery_expired", "The original recovery authority has expired.")

    def register(self, view: dict) -> dict:
        record = view["record"]
        if record["contract"].get("recovery_policy") is None:
            return view
        binding = chatgpt.get(self.app.state, record["task_id"])
        if self._disabled(record, binding):
            return view
        self._active(record)
        if record.get("chat_registration") is not None:
            self._registered(record, binding)
            return view
        route, workspace, fingerprint = self._profile()
        self.app.config.machine(workspace.machine, write=True)
        context = self.app.machines.read_workspace_file(
            workspace.machine, route.workspace, "AGENTS.md", start_line=1, line_count=1)["context"]
        workspace_identity = context.get("workspace_identity")
        if not isinstance(workspace_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", workspace_identity):
            raise Fault("context_unavailable", "The recovery controller identity is unavailable.")
        identity = records.digest(["task-chat", record["task_id"], record["created_at_ms"], record["origin"]])[:32]
        authority = _authority(record)
        with self.app._admit(), tasks.locked_read(self.app.state, record["task_id"]) as current:
            self._active(current["record"])
            if _authority(current["record"]) != authority:
                raise Fault("chat_recovery_conflict", "Task scope changed before registration.")
            try:
                binding = chatgpt.register_preparation(self.app.state, task_id=record["task_id"],
                    preparation_id=identity, acknowledged=current["record"].get("chat_registration") is not None,
                    machine=workspace.machine, workspace=route.workspace, workspace_identity=workspace_identity,
                    authority_ref=record["contract"]["authority_refs"][0], authority_sha256=authority,
                    expires_at_ms=record["contract"]["expires_at_ms"], controller_sha256=fingerprint)
            except OSError as error:
                raise Fault("chat_recovery_store_unavailable", "Receiver registration is unverified.") from error
            revision = current["record"]["revision"]
        if binding.disabled:
            return view
        if binding.authority_sha256 != authority or binding.preparation.controller_sha256 != fingerprint:
            raise Fault("chat_recovery_registration_conflict", "Retained registration belongs to different scope.")
        def acknowledge(value, now):
            self._active(value)
            if _authority(value) != authority or value.get("chat_registration") not in {None, identity}:
                raise Fault("chat_recovery_conflict", "Task scope changed before registration acknowledgement.")
            value["chat_registration"] = identity
        with self.app._admit():
            saved = tasks.update(self.app.state, record["task_id"], acknowledge,
                                 expected_revision=revision, require_current=True)
        return {**saved, "status": view.get("status", saved["status"])}

    def created(self, view: dict) -> dict:
        """Creation has already committed; a preparation failure cannot erase it."""
        try:
            view = self.register(view)
            self._blockers.pop(view["task_id"], None)
        except (Fault, OSError) as error:
            self._blockers[view["task_id"]] = observation.error_code(error)
            view = {**tasks.read(self.app.state, view["task_id"]), "status": view.get("status", "saved")}
        return self.project(view)

    def _discover(self) -> None:
        page = tasks.observation_page(self.app.state, self._known)
        self._known = {key: value for key, value in self._known.items() if key in page["identities"]}
        self._blockers = {key: value for key, value in self._blockers.items() if key in page["identities"]}
        if not page["complete"]:
            self._failure = "task_index_incomplete"
        for entry in page["entries"]:
            identity, record = entry["task_id"], entry["record"]
            if entry["error"]:
                self._blockers[identity] = entry["error"]
                continue
            try:
                if record is not None and record["contract"].get("recovery_policy") is not None and record["terminal"] is None:
                    self.register(tasks.read(self.app.state, identity))
                self._known[identity] = entry["metadata_sha256"]
                self._blockers.pop(identity, None)
            except (Fault, OSError) as error:
                code = observation.error_code(error)
                self._blockers[identity] = code
                if code not in {"resource_busy", "task_conflict", "chat_recovery_capacity", "chat_recovery_store_unavailable"}:
                    self._known[identity] = entry["metadata_sha256"]

    def require_ready(self, record: dict) -> None:
        if record["contract"].get("recovery_policy") is None:
            return  # Historical tasks retain their original admission contract.
        binding = chatgpt.get(self.app.state, record["task_id"])
        if self._disabled(record, binding):
            return
        self._registered(record, binding)
        if binding.preparation.status != "ready":
            raise Fault("chat_recovery_not_ready", "Receiver preparation must complete before a new business effect.",
                        {"preparation_state": binding.preparation.status, "reason": binding.preparation.reason})
        if binding.delivery is not None and binding.delivery.status == "blocked":
            raise Fault("chat_recovery_route_blocked", "The prepared route has a known browser blocker.",
                        {"reason": binding.delivery.reason})
        self._controller(binding)

    def _preparation_binding(self, record: dict, preparation_id: str):
        if not isinstance(preparation_id, str) or re.fullmatch(r"[0-9a-f]{32}", preparation_id) is None:
            raise Fault("invalid_preparation_id", "Use the exact original preparation ID.")
        now = chatgpt.now_ms()
        if (record["terminal"] is not None or record["control"]["stop_intent"] != "none"
                or now >= record["contract"]["expires_at_ms"]):
            raise Fault("task_stopped", "Stopped or expired work cannot prove receiver readiness.")
        binding = chatgpt.get(self.app.state, record["task_id"])
        preparation = binding.preparation if binding else None
        if (binding is None or binding.disabled or binding.authority_sha256 != _authority(record)
                or preparation is None or preparation.preparation_id != preparation_id
                or preparation.status != "reserved"
                or now >= min(preparation.deadline_ms, binding.expires_at_ms or 0)):
            raise Fault("chat_recovery_preparation_conflict", "The original receiver preparation is not current.")
        self._registered(record, binding)
        return binding

    def preparation_read(self, view: dict, preparation_id: str) -> dict:
        if "record" not in view:
            raise Fault("task_legacy", "Legacy checkpoints cannot prove receiver readiness.")
        record = view["record"]
        binding = self._preparation_binding(record, preparation_id)
        return {**self.project(view), "preparation_read": chatgpt.preparation_response(binding, record)}

    def _observe_preparation(self, arguments: dict, payload: dict) -> None:
        identity, preparation_id = arguments.get("task_id"), arguments["preparation_id"]
        returned, response = payload.get("record"), payload.get("preparation_read")
        if not isinstance(returned, dict) or returned.get("task_id") != identity or not isinstance(response, dict):
            raise Fault("chat_recovery_preparation_conflict", "The external read does not match this task.")
        # Keep stop/scope stable through the short sidecar publication. No network
        # or browser work is performed while the canonical task read is held.
        with self.app._admit(), tasks.locked_read(self.app.state, identity) as current:
            record = current.get("record")
            if record is None or _authority(record) != _authority(returned):
                raise Fault("chat_recovery_preparation_conflict", "Task authority changed during the receiver read.")
            binding = self._preparation_binding(record, preparation_id)
            if ((payload.get("chat_recovery") or {}).get("binding_revision") != binding.revision
                    or response != chatgpt.preparation_response(binding, returned)):
                raise Fault("chat_recovery_preparation_conflict", "The returned receiver evidence differs.")
            try:
                chatgpt.observe_preparation_read(self.app.state, identity, preparation_id, binding.revision, response)
            except OSError as error:
                # Do not misclassify sidecar IO as corruption of the task reader.
                raise Fault("chat_recovery_store_unavailable", "Receiver evidence persistence is unverified.") from error

    def _scan(self) -> None:
        try:
            self._failure = None
            self._discover()
            retained = chatgpt.bindings(self.app.state)
            # Original reservations are observed even after pause/disable/expiry.
            for binding in retained:
                if self._stop.is_set():
                    return
                if binding.preparation and binding.preparation.status == "reserved":
                    result = self.read_preparation(binding.task_id)
                    if result["chat_recovery"]["preparation"]["status"] == "reserved":
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
                    if self._disabled(record, binding) or record["terminal"] is not None:
                        continue
                    if binding.preparation and binding.preparation.status in {"pending", "deferred"}:
                        result = self.start_preparation(identity, uuid.uuid4().hex)
                        if result["chat_recovery"]["preparation"]["status"] == "reserved":
                            return
                        continue
                    if not binding.chat_url:
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
            "automatic_preparation": True,
            "origin_chat_url_required": False,
            "controller_configured": self.app.config.chat_recovery is not None,
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
        failure = None
        try:
            binding = chatgpt.get(self.app.state, record["task_id"])
        except (Fault, OSError) as error:
            binding, failure = None, observation.error_code(error)
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
        task_state = state if state in {"terminal", "paused", "cancelling", "expired"} else "active"
        if record["contract"].get("recovery_policy") is not None:
            public["disabled"] = self._disabled(record, binding)
            if failure:
                state, blocker = "blocked", failure
            elif public["disabled"]:
                state, blocker = "disabled", None
            else:
                try:
                    self._profile()
                    self._registered(record, binding)
                    preparation = binding.preparation
                    if preparation.status == "ready":
                        self._controller(binding)
                    state = "ready" if preparation.status == "ready" else "preparing"
                    blocker = None
                    if preparation.status not in {"pending", "reserved", "deferred", "ready"}:
                        state, blocker = "blocked", preparation.reason or preparation.status
                    elif state != "ready" and now >= preparation.deadline_ms:
                        state, blocker = "blocked", "preparation_budget"
                    elif state == "ready" and delivery is not None and delivery.status == "blocked":
                        state, blocker = "blocked", "chat_recovery_route_blocked"
                except (Fault, OSError) as error:
                    code = observation.error_code(error)
                    if code == "chat_recovery_preparing":
                        state = "preparing" if blocker is None else "blocked"
                        blocker = blocker or code
                    else:
                        state, blocker = "blocked", code
                if not eligible and state != "ready":
                    state, blocker = "blocked", task_state
        elif failure:
            state, blocker = "blocked", failure
        if (blocker is None and delivery is not None and delivery.sent_at_ms is not None
                and delivery.continuation_at_ms is None
                and now >= min(delivery.sent_at_ms + chatgpt.CONTINUATION_MS, delivery.deadline_ms)):
            blocker = "continuation_not_observed"
        return {**view, "chat_recovery": {
            "eligible": eligible,
            "task_state": task_state,
            "default_enabled": True,
            "state": state,
            "delivery_state": progress_state,
            "observer_running": not self._stop.is_set(),
            "observation_grace_until_ms": self._not_before or None,
            "blocker": blocker,
            **public,
        }}

    def configure(self, task_id: str, update_id: str, expected_revision: int, change: dict) -> dict:
        if not isinstance(change, dict) or set(change) != {"action"} or change["action"] not in {"enable", "disable"}:
            raise Fault("invalid_chat_recovery_change", "Use only an explicit enable or disable override.")
        view = tasks.read(self.app.state, task_id)
        if "record" not in view:
            raise Fault("task_legacy", "Legacy checkpoints cannot acquire automatic recovery.")
        record, action = view["record"], change["action"]
        binding = chatgpt.get(self.app.state, task_id)
        request_sha256 = records.digest({"method": "configure_chat_recovery", "task_id": task_id,
                                        "expected_revision": expected_revision, "change": change})
        if binding is not None and binding.last_update_id == update_id:
            if binding.last_request_sha256 != request_sha256:
                raise Fault("chat_recovery_change_conflict", "This update ID belongs to different recovery intent.")
            return {"task_id": task_id, **self.project(view)["chat_recovery"]}
        if action == "enable":
            self._active(record)
            if record["contract"].get("recovery_policy") is None and not (binding and binding.chat_url):
                raise Fault("chat_recovery_enrollment_required", "Explicitly revise the historical plan before enrollment.")
            self.app._validate_task_scope({"contract": record["contract"]}, check_head=False)
            if binding and binding.preparation and binding.preparation.controller_sha256 != self._profile()[2]:
                raise Fault("chat_recovery_controller_changed", "Do not replace the original recovery controller.")
        with self.app._admit(), tasks.locked_read(self.app.state, task_id) as current:
            if _authority(current["record"]) != _authority(record):
                raise Fault("chat_recovery_conflict", "Task authority changed before the override.")
            if action == "enable":
                self._active(current["record"])
            try:
                chatgpt.configure(self.app.state, task_id=task_id, update_id=update_id,
                    expected_revision=expected_revision, action=action, request_sha256=request_sha256,
                    authority_sha256=_authority(record), expires_at_ms=record["contract"]["expires_at_ms"])
            except OSError as error:
                raise Fault("chat_recovery_store_unavailable", "Recovery override was not acknowledged.") from error
        view = self.created(view) if action == "enable" else self.project(view)
        self.app.task_observer.notify()
        return {"task_id": task_id, **view["chat_recovery"]}

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
            reconciliation_only |= result is None or result["status"] in {"unknown", "partial"}
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

    def _controller(self, binding) -> None:
        selected = self.app.config.workspaces.get(binding.workspace)
        if selected is None or selected.machine != binding.machine:
            raise Fault("chat_recovery_scope_invalid", "The retained controller workspace is no longer configured.")
        if self.app.config.machine(binding.machine, write=True).transport != "local":
            raise Fault("chat_recovery_scope_invalid", "The retained recovery controller is no longer local.")
        context = self.app.machines.read_workspace_file(
            binding.machine, binding.workspace, "AGENTS.md", start_line=1, line_count=1)["context"]
        if context.get("workspace_identity") != binding.workspace_identity:
            raise Fault("chat_recovery_scope_changed", "The original recovery controller identity changed.")

    def start_preparation(self, task_id: str, execution_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
            raise Fault("invalid_execution_id", "Use an original preparation execution ID.")
        record = tasks.read(self.app.state, task_id)["record"]
        binding = chatgpt.get(self.app.state, task_id)
        self._active(record)
        self._registered(record, binding)
        self._controller(binding)
        self.app._validate_task_scope({"contract": record["contract"]}, check_head=False)
        with self.app._admit(), tasks.locked_read(self.app.state, task_id) as current:
            self._active(current["record"])
            self._registered(current["record"], binding)
            try:
                reserved, reused = chatgpt.reserve_preparation(self.app.state, task_id,
                    binding.preparation.preparation_id, binding.revision, execution_id)
            except OSError as error:
                raise Fault("chat_recovery_store_unavailable", "Preparation reservation is unverified.") from error
        if reused:
            return {"task_id": task_id, "chat_recovery": chatgpt.public(reserved)}
        def preflight():
            if self._stop.is_set():
                raise Fault("chat_recovery_stopped", "The original observer stopped before setup dispatch.")
            current = tasks.read(self.app.state, task_id)["record"]
            selected = chatgpt.get(self.app.state, task_id)
            self._active(current)
            self._registered(current, selected)
            self._controller(selected)
            if (selected.disabled or selected.revision != reserved.revision
                    or selected.preparation.status != "reserved"
                    or selected.preparation.execution_id != execution_id):
                raise Fault("chat_recovery_conflict", "Preparation changed before its browser launch.")
        route = self._profile()[0].model_dump()
        started = self._dispatch_browser(reserved, execution_id, chatgpt.preparation_goal(reserved, route),
            reserved.chat_url or chatgpt.ORIGIN + "/", reserved.preparation.deadline_ms, preflight, preparation=True)
        return {"task_id": task_id, "chat_recovery": chatgpt.public(reserved),
                "preparation_execution_id": execution_id, "execution_status": started["status"]}

    def read_preparation(self, task_id: str) -> dict:
        original = chatgpt.get(self.app.state, task_id)
        if original is None or original.preparation is None or original.preparation.execution_id is None:
            raise Fault("chat_recovery_preparation_unavailable", "No original preparation execution is retained.")
        execution_id = original.preparation.execution_id
        if original.preparation.status != "reserved":
            return {"task_id": task_id, "chat_recovery": chatgpt.public(original)}
        try:
            result = self.app.codex.read(self.app.config, self.app.state,
                self.app.machines.read_execution, original.machine, execution_id)
        except Fault as error:
            if error.code != "execution_not_found":
                raise
            result = {"status": "outcome_unknown"}
        if result["status"] in {"accepted", "preparing", "running", "stopping", "finishing"}:
            return {"task_id": task_id, "chat_recovery": chatgpt.public(original)}
        binding = chatgpt.get(self.app.state, task_id)
        route = self.app.config.chat_recovery
        observed, reason, url = chatgpt.preparation_outcome(
            result, binding, route.model_dump() if route else {"connection": ""})
        if observed == "ready":
            receipt = self.app.machines.read_execution(binding.machine, execution_id, max_bytes=1)
            if receipt.get("status") != "succeeded" or (receipt.get("cleanup") or {}).get("status") != "verified":
                observed, reason = "outcome_unknown", "browser_cleanup_unverified"
        with self.app._admit(), tasks.locked_read(self.app.state, task_id) as current:
            if observed == "ready":
                try:
                    record = current["record"]
                    self._active(record)
                    self._registered(record, binding)
                    if any(item["kind"] == "stopped" and item["at_ms"] >= binding.preparation.created_at_ms
                           for item in record["history"]):
                        raise Fault("task_stopped", "Setup crossed a task stop; preserve its evidence.")
                except Fault as error:
                    observed, reason = "blocked", error.code
            try:
                public = chatgpt.mark_preparation(self.app.state, task_id, execution_id, observed, reason,
                                                 url, expected_revision=original.revision)
            except OSError as error:
                raise Fault("chat_recovery_store_unavailable", "Preparation result publication is unverified.") from error
        return {"task_id": task_id, "chat_recovery": public, "preparation_execution_id": execution_id,
                "execution_status": result["status"]}

    def _dispatch_browser(self, binding, execution_id: str, goal: str, start_url: str,
                          deadline: int, preflight, *, preparation: bool = False) -> dict:
        """One private launch boundary for fixed setup and continuation messages."""
        launch_attempted = False
        try:
            with self.app._admit():
                preflight()
                def launch(**arguments):
                    nonlocal launch_attempted
                    arguments.pop("task_context", None)
                    launch_attempted = True
                    return self.app.machines.start_execution(**arguments)
                timeout = min(chatgpt.BROWSER_SECONDS, (deadline - chatgpt.now_ms()) // 1000)
                if timeout < 1:
                    raise Fault("task_authority_expired", "Recovery authority cannot cover a browser attempt.")
                return self.app.codex.start(self.app.config, self.app.state, launch, binding.machine,
                    execution_id, self.app.config.workspaces[binding.workspace].root, goal, start_url,
                    [chatgpt.ORIGIN], "browser", "chrome", timeout, "", None)
        except Fault as error:
            unsent = not launch_attempted or error.code == "execution_disabled"
            if error.code == "resource_busy" and launch_attempted:
                try:
                    with lease(self.app.state / "locks" / f"{execution_id}.lock", create=False):
                        unsent = not os.path.lexists(self.app.state / "executions" / execution_id)
                except (Fault, OSError):
                    pass  # Absence without the original launch lock is not proof.
            if unsent:
                with self.app._admit():
                    if preparation:
                        chatgpt.mark_preparation(self.app.state, binding.task_id, execution_id,
                            "deferred" if error.code == "resource_busy" else "blocked",
                            "prelaunch_rejection" if error.code == "resource_busy" else error.code)
                    else:
                        chatgpt.release_reservation(self.app.state, binding.task_id, execution_id)
            raise

    def _binding(self, record: dict):
        binding = chatgpt.get(self.app.state, record["task_id"])
        now = chatgpt.now_ms()
        authority = _authority(record)
        if (binding is None or binding.disabled or binding.chat_url is None
                or binding.authority_sha256 not in {authority, records.digest(record["contract"])}
                or (binding.expires_at_ms or 0) <= now):
            raise Fault("chat_recovery_binding_unavailable",
                        "The default recovery policy is active but this task has no current valid Chat binding.")
        self.require_ready(record)
        self._controller(binding)
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
        def preflight():
            current, current_reconciliation = self._task(task_id, automatic=automatic)
            current_binding, current_authority = self._binding(current["record"])
            if automatic:
                self._quiet(current_binding)
            if (current["record"]["handoff"] is None or current["record"]["handoff"]["id"] != handoff["id"]
                    or current_authority != authority or current_reconciliation != reconciliation_only
                    or current_binding.revision != binding.revision):
                raise Fault("chat_recovery_conflict", "Task or recovery authority changed before browser dispatch.")
        started = self._dispatch_browser(binding, execution_id, goal, binding.chat_url, deadline, preflight)
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
