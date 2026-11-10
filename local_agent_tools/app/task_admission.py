"""Cooperative task control and admission; domains retain every original effect."""

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import os
from pathlib import Path, PurePosixPath

from local_agent_tools import execution, tasks, workspace
from local_agent_tools.foundation.sanitize import Fault, PRE_EFFECT_REQUEST_FAILURES
from local_agent_tools.foundation.state import lease
from local_agent_tools.tasks import effects, records, transitions
from . import task_attention, task_observation, task_receipts

FILE_METHODS = {"write_workspace_file", "edit_workspace_file", "apply_workspace_patch"}
EXECUTION_METHODS = {"start_execution", "create_worktree"}
METHODS = FILE_METHODS | EXECUTION_METHODS | {"control_execution", "cancel_execution"}
# These failures are defined before effects by the existing domain owners. Anything
# else is unknown unless an original receipt proves its outcome.
NO_EFFECT = {"execution_disabled", "workspace_write_disabled", "invalid_command", "invalid_timeout",
             "invalid_access", "invalid_resources", "invalid_head", "invalid_cwd", "context_mismatch",
             "context_unavailable", "path_denied", "legacy_workspace_busy", "ambiguous_workspace"}


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
            "control_revision": record["control_revision"],
            "unresolved_operation_ids": [item["id"] for item in record["operations"] if effects.unresolved(item)]}


class TaskAdmission:
    def __init__(self, application):
        self.app = application

    @property
    def principal(self) -> str:
        # The private native-account endpoint is the verified S1 boundary. This
        # is neither a model-supplied identity nor a client conversation ID.
        return f"native-uid:{os.getuid()}"

    def _require_foreground_clear(self, view: dict) -> None:
        projected = task_attention.project(self.app.task_observer.project(view))
        foreground = projected.get("foreground", {})
        if foreground.get("status") in {"attention_required", "renewal_required"}:
            code = "task_renewal_required" if foreground["status"] == "renewal_required" else "task_attention_required"
            if foreground["reason"] in {"task_stopped", "task_authority_expired"}:
                code = foreground["reason"]
            raise Fault(code, foreground["summary"],
                        {"task_id": projected["record"]["task_id"], "foreground": foreground},
                        effect="known_no_effect")

    def control(self, request: dict) -> dict:
        request = records.normalize_control(request)
        change = request["transition"]
        action = change["action"]
        selected = change.get("evidence", []) if action == "record_decision" else [
            item["operation_id"] for item in change.get("acceptance", []) if item["operation_id"]]
        operation_ids = selected
        view = tasks.read(self.app.state, request["task_id"], operation_ids=operation_ids)
        if "record" not in view:
            raise Fault("task_legacy", "Legacy checkpoints are history; conversion is not a control transition.")
        record = view["record"]
        update = {"id": request["update_id"], "request_sha256": records.digest(
            {"method": "control_task", "principal": self.principal, **request})}
        retained = tasks.control_update(self.app.state, request["task_id"], update["id"])
        if retained is None:
            transitions.guards(record, request["expected_revision"], request["expected_plan_revision"], request["expected_epoch"])
            if action not in {"pause", "cancel", "release", "abandon"}:
                contract = request["transition"].get("contract", record["contract"])
                # Reattachment acquires ownership, not permission for a new effect.
                # Scope evidence also describes a past request, not the current HEAD.
                # Keep identity/grant checks; a changed HEAD still requires an
                # explicit plan revision before admission or acceptance.
                self.app._validate_task_scope(
                    {"contract": contract}, check_head=action not in {"claim", "renew", "resume"},
                    check_identity=action not in {"claim", "renew"})
            if action in {"record_decision", "finish", "revise_plan"}:
                view = self.reconcile(view)
                transitions.guards(view["record"], request["expected_revision"], request["expected_plan_revision"], request["expected_epoch"])
                for identity in selected:
                    operation = next((item for item in view["record"]["operations"] if item["id"] == identity), None)
                    if operation is None:
                        raise Fault("task_evidence_invalid", "Acceptance must reference an original task operation.")
                    observed = self.app.task_receipts.read(operation, verify=True)
                    current, _ = task_receipts.observation(operation, observed)
                    if current != operation["result"]:
                        raise Fault("task_evidence_changed", "Selected evidence changed; reconcile its original result before accepting it.")
        if action == "abandon" and retained is None:
            self._check_abandon(record)
        retained_targets = None
        if action == "revise_plan" and retained is None:
            retained_targets = []
            for target in change["contract"]["targets"]:
                config = workspace.select_worktree(self.app.config, target["machine"], target["workspace"], target.get("worktree", ""))
                retained_targets.append(workspace.writable_target(workspace.root_for(config, target["machine"], target["workspace"])))
            # A reserved nested cwd moves with its accepted non-Git parent.
            # Verify the original inode at the exact relative path before rebinding.
            prior_roots = []
            for target in record["contract"]["targets"]:
                configured = self.app.config.workspaces.get(target["workspace"])
                old = Path(target.get("worktree") or (
                    configured.root if configured is not None and configured.machine == target["machine"]
                    else target["workspace"]))
                if old.is_absolute():
                    prior_roots.append(old)
            for held in tasks.writer_reservations(self.app.state, record["task_id"]):
                previous = Path(held["root"])
                for old in prior_roots:
                    if not previous.is_relative_to(old):
                        continue
                    suffix = previous.relative_to(old)
                    if not suffix.parts:
                        continue
                    for current in retained_targets[:len(change["contract"]["targets"])]:
                        try:
                            relocated = workspace.writable_target(Path(current["root"]) / suffix)
                        except Fault:
                            continue
                        if (relocated["device"], relocated["inode"]) == (held["device"], held["inode"]):
                            retained_targets.append(relocated)
        view = tasks.update(self.app.state, request["task_id"],
            lambda value, now: transitions.apply_control(value, request, self.principal, now),
            expected_revision=request["expected_revision"], last_update=update,
            expected_plan_revision=request["expected_plan_revision"], expected_epoch=request["expected_epoch"],
            operation_ids=operation_ids, retained_targets=retained_targets)
        if action == "cancel" and view["record"]["terminal"] is None:
            view = self._cancel_owned(view, request["update_id"])
        return view

    def _check_abandon(self, record: dict) -> None:
        """Only terminal uncertainty may be abandoned; original effect scopes survive."""
        unresolved = [item for item in record["operations"] if effects.unresolved(item)]
        if not unresolved:
            raise Fault("task_abandon_not_needed", "Use cancel when no original effect is unresolved.")
        for operation in unresolved:
            if (operation["result"] or {}).get("status") not in {"unknown", "partial"}:
                raise Fault("task_effect_active", "Observe or cancel active original effects before abandonment.")
            observed = self.app.task_receipts.read(operation)
            if operation["locator"]["kind"] == "execution":
                if (observed.get("status") not in {"outcome_unknown", "succeeded", "failed", "timed_out", "cancelled"}
                        or (observed.get("command") or {}).get("exited") is not True):
                    raise Fault("task_effect_active", "Original execution lacks a known leader exit; preserve its active exclusion.")
            elif observed.get("status") not in {"applied", "unchanged", "rejected", "partial"}:
                raise Fault("task_effect_active", "Original workspace change lacks terminal publication evidence.")

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

    def _dispatch(self, method: str, arguments: dict, *, supervised: bool = False, effect_guard=None,
                  scope: dict | None = None) -> dict:
        if method not in METHODS:
            raise Fault("unsupported_operation", "Task admission applies only to the explicit existing mutation entrypoints.")
        if effect_guard is not None:
            arguments = dict(arguments, effect_guard=effect_guard)
        if scope is not None:
            # The compiled operation scope is not request identity: retries keep their original receipt.
            if method not in EXECUTION_METHODS:
                raise Fault("scope_invalid", "Only native execution methods carry a process scope.", effect="known_no_effect")
            arguments = dict(arguments, scope=scope)
        if method in EXECUTION_METHODS and supervised:
            return getattr(self.app.machines, method)(**arguments, task_admitted=True)
        return getattr(self.app.machines, method)(**arguments)

    def _locator(self, method: str, arguments: dict, context: dict | None) -> dict | None:
        self.app.config.machine(arguments["machine"])
        if method in FILE_METHODS:
            if not arguments.get("change_id"):
                return None
            return {"kind": "change", "machine": arguments["machine"],
                    "change_id": arguments["change_id"]}
        return {"kind": "execution", "machine": arguments["machine"],
                "execution_id": arguments["execution_id"],
                "control_seq": arguments["expected_control_seq"] + 1 if method == "control_execution" else None}

    def _normalize(self, method: str, arguments: dict, context: dict) -> dict:
        value = deepcopy(arguments)
        if method in FILE_METHODS | {"create_worktree"} and value.get("worktree") == "":
            value.pop("worktree")
        if method in EXECUTION_METHODS:
            if value.get("task_id") not in {"", None, context["task_id"]} or value["execution_id"] != context["operation_id"]:
                raise Fault("task_operation_conflict", "Bind the original execution ID and task ID, not another identity.")
            value["task_id"] = context["task_id"]
        if method == "start_execution":
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
        return value

    def _fingerprint(self, method: str, arguments: dict, operation: dict) -> str:
        return records.digest({"method": method, "arguments": arguments,
            "plan_revision": operation["plan_revision"], "source_sha256": operation["source_sha256"]})

    def _legacy(self, method: str, arguments: dict, locator: dict | None, scope: dict | None = None) -> dict:
        if locator is None or method == "cancel_execution":
            return self._dispatch(method, arguments, scope=scope)
        try:
            binding = tasks.operation_binding(self.app.state, tasks.operation_key(locator))
        except Fault as index_error:
            if method not in FILE_METHODS | EXECUTION_METHODS:
                raise
            try:
                self.app.task_receipts.read({"locator": locator})
            except Fault:
                raise index_error from None
            return self._dispatch(method, arguments, scope=scope, supervised=method in EXECUTION_METHODS)
        if binding is None:
            return self._dispatch(method, arguments, scope=scope)
        if method in FILE_METHODS | EXECUTION_METHODS:
            try:
                self.app.task_receipts.read({"locator": locator})
            except Fault:
                pass
            else:
                # Native immutable-ID reconciliation cannot create another effect.
                return self._dispatch(method, arguments, scope=scope, supervised=method in EXECUTION_METHODS)
        raise Fault("task_context_required", "This original effect was supervised; retain task_context before a new consequence.")

    def run(self, method: str, arguments: dict, context: dict | None, *, scope: dict | None = None) -> dict:
        if context is None and method == "cancel_execution":
            # Preserve the existing original-ID stop route even while a task
            # admission identity is busy or its canonical evidence is unavailable.
            return self._dispatch(method, arguments, scope=scope)
        if context is not None:
            context = records.normalize_context(context)
            arguments = self._normalize(method, arguments, context)
        locator = self._locator(method, arguments, context)
        if context is not None and locator is None:
            raise Fault("task_operation_conflict", "Supervised effects require a durable original operation identity.")
        if locator is None:
            return self._dispatch(method, arguments, scope=scope)
        key = tasks.operation_key(locator)
        # These coordinate one effect identity, not task progress or a running
        # worker's lifetime. Task locks are never held across domain/network IO.
        names = {"task-effect-" + records.digest(list(key))}
        if context is not None:
            names.add("task-operation-" + context["operation_id"])
        with ExitStack() as locks:
            try:
                for name in sorted(names):
                    locks.enter_context(lease(self.app.state / "locks" / (name + ".lock")))
            except Fault as error:
                if error.code != "resource_busy" or context is not None or method not in FILE_METHODS | EXECUTION_METHODS:
                    raise
                try:
                    self.app.task_receipts.read({"locator": locator})
                except Fault:
                    raise error from None
                return self._dispatch(method, arguments, scope=scope)
            if context is None:
                return self._legacy(method, arguments, locator, scope)
            operation_ids = [context["operation_id"]]
            if method in {"control_execution", "cancel_execution"}:
                operation_ids.append(locator["execution_id"])
            view = tasks.read(self.app.state, context["task_id"], operation_ids=operation_ids)
            if "record" not in view:
                raise Fault("task_legacy", "A legacy checkpoint cannot acquire supervision through task_context.")
            record = view["record"]
            operation = next((item for item in record["operations"] if item["id"] == context["operation_id"]), None)
            if operation is not None:
                if (tasks.operation_key(operation["locator"]) != key or operation["locator"].get("control_seq") != locator.get("control_seq")
                        or operation["request_sha256"] != self._fingerprint(method, arguments, operation)):
                    raise Fault("task_operation_conflict", "The original operation ID belongs to different intent; do not replace or replay it.")
                expected = {operation["id"]: records.digest(operation)}
                observed = self.app.task_receipts.read(operation)
                result, enriched = task_receipts.observation(operation, observed)
                current = self._publish(record["task_id"], [(operation["id"], result, enriched)], expected=expected)
                return {**observed, "task": _reference(current), "task_operation_replayed": False}
            transitions.guards(record, context["task_revision"], context["plan_revision"], context["continuation_epoch"], allow_observations=True)
            if method != "cancel_execution":
                binding = tasks.operation_binding(self.app.state, key, context["operation_id"])
                if binding is not None and binding["task_id"] != context["task_id"]:
                    raise Fault("task_operation_conflict", "This original effect belongs to another task; no new association was admitted.")
            if method in EXECUTION_METHODS | FILE_METHODS:
                try:
                    self.app.task_receipts.read({"locator": locator})
                except Fault as error:
                    if error.code not in {"execution_not_found", "change_not_found"}:
                        raise
                else:
                    raise Fault("task_operation_conflict", "An existing unbound domain operation cannot be adopted as newly admitted task evidence.")
            try:
                self._scope(record, method, arguments, locator)
            except Fault as error:
                if error.code not in {"context_mismatch", "task_scope_denied"}:
                    raise
                evidence = {**(error.evidence or {}), "task_id": record["task_id"], "operation_id": context["operation_id"],
                    "resume_prompt": f"Read task {record['task_id']} and its original operation results. "
                                     "Inspect the intended workspace/worktree, identity, HEAD and grants; "
                                     "revise the original task scope only within current authority before new work. Keep task_context; "
                                     "do not switch to an unbound call or replay original effects."}
                raise Fault(error.code, str(error), evidence, effect=error.effect) from error
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
            target = None
            effect_guard = None
            with ExitStack() as admission_lease:
                if method in FILE_METHODS | {"start_execution"}:
                    config = self.app.config
                    if method == "start_execution":
                        root = Path(arguments["cwd"]).resolve(strict=True)
                    else:
                        config = workspace.select_worktree(config, arguments["machine"], arguments["workspace_name"], arguments.get("worktree", ""))
                        root = workspace.root_for(config, arguments["machine"], arguments["workspace_name"])
                    target = workspace.writable_target(root)
                    observed_root = workspace.context(root)
                    execution.check_workspace_leases(config, self.app.state, scope=root)
                    key = hashlib.sha256(workspace.lock_root(config, arguments["machine"], root, observed_root).encode()).hexdigest()
                    try:
                        for path, shared in workspace.writer_path_locks(observed_root):
                            path_key = hashlib.sha256(path.encode()).hexdigest()
                            admission_lease.enter_context(lease(self.app.state / "locks" /
                                                                f"writer-path-{path_key}.lock", shared=shared))
                        admission_lease.enter_context(lease(self.app.state / "locks" / f"workspace-{key}.lock"))
                    except Fault as error:
                        raise Fault(error.code, str(error), error.evidence, effect="known_no_effect") from error
                    if workspace.writable_target(root) != target:
                        raise Fault("context_mismatch", "The selected target changed before admission.", effect="known_no_effect")
                admitted = tasks.update(self.app.state, record["task_id"],
                    lambda value, now: self._admit(value, operation, context, method, now),
                    expected_revision=context["task_revision"],
                    expected_plan_revision=context["plan_revision"], expected_epoch=context["continuation_epoch"],
                    allow_observations=True, operation_ids=operation_ids, reserve_disk=method != "cancel_execution",
                    writer_target=target)
            if target is not None:
                def effect_guard(root):
                    if workspace.writable_target(root) != target:
                        raise Fault("context_mismatch", "The admitted physical target changed before dispatch.", effect="known_no_effect")
                    tasks.check_writer(self.app.state, target, record["task_id"], required=True)

            operation = next(item for item in admitted["record"]["operations"] if item["id"] == context["operation_id"])
            expected = {operation["id"]: records.digest(operation)}
            try:
                observed = self._dispatch(method, arguments, scope=scope, supervised=True, effect_guard=effect_guard)
            except (Fault, OSError) as error:
                code = error.code if isinstance(error, Fault) else "domain_io_unknown"
                owner_effect = getattr(error, "effect", None)
                # Creation errors may arrive after dispatch or without a remote
                # phase marker; their code alone cannot prove a pre-effect failure.
                no_effect = owner_effect == "known_no_effect" or (owner_effect is None
                    and method != "create_worktree"
                    and (code in NO_EFFECT or method in FILE_METHODS and code in PRE_EFFECT_REQUEST_FAILURES))
                try:
                    observed = self.app.task_receipts.read(operation)
                    result, enriched = task_receipts.observation(operation, observed)
                except (Fault, OSError):
                    unstarted = code == "resource_busy" and method in EXECUTION_METHODS and self.app.task_receipts.unstarted_local(operation)
                    result, enriched = task_receipts.receipt_result("failed" if no_effect or unstarted else "unknown",
                                                summary=f"Domain failure: {code}."), locator
                phase = {"running": "active", "partial": "effect_partial", "unknown": "effect_unknown"}.get(result["status"], "effect_verified")
                if no_effect and result["status"] == "failed":
                    phase = "known_no_effect"
                result = dict(result, failure_code=code, effect=phase)
                self._publish(record["task_id"], [(operation["id"], result, enriched)], expected=expected)
                raise
            result, enriched = task_receipts.observation(operation, observed)
            current = self._publish(record["task_id"], [(operation["id"], result, enriched)], expected=expected)
            return {**observed, "task": _reference(current)}

    def _scope(self, record: dict, method: str, arguments: dict, locator: dict) -> None:
        contract = record["contract"]
        stopping = method == "cancel_execution"
        if method not in contract["tools"] and not (stopping and record["control"]["stop_intent"] == "cancel"):
            raise Fault("task_scope_denied", "This tool is outside the accepted task contract.")
        machine = arguments["machine"]
        self.app.config.machine(machine, write=method in EXECUTION_METHODS or
                                method == "control_execution" and arguments["action"] in {"write_stdin", "resize"})
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
            selection = accepted.get("worktree", "")
            if method in FILE_METHODS | {"create_worktree"} and (
                    arguments["workspace_name"] != name or arguments.get("worktree", "") != selection):
                continue
            selected_arguments = {"worktree": selection} if selection else {}
            if method == "create_worktree":
                observed = self.app.machines.inspect_workspace(machine, name, "worktrees",
                                                                **selected_arguments)["context"]
            else:
                observed = self.app.machines.read_workspace_file(machine, name, "AGENTS.md", start_line=1,
                                                                 line_count=1, **selected_arguments)["context"]
            if method == "start_execution":
                cwd, root = PurePosixPath(arguments["cwd"]), PurePosixPath(observed["cwd"])
                if ".." in cwd.parts or not cwd.is_relative_to(root):
                    continue
                selected = self.app.machines.inspect_workspace(machine, name, "files", str(cwd.relative_to(root)),
                                                               limit=1, **selected_arguments)
                if not PurePosixPath(selected["path"]).is_relative_to(PurePosixPath(observed["cwd"])):
                    raise Fault("task_scope_denied", "The command cwd resolves outside the accepted task workspace.")
            if observed.get("workspace_identity") != accepted["workspace_identity"] or (
                    method != "create_worktree" and (observed.get("head_sha") or None) != accepted["head_sha"]):
                raise Fault("context_mismatch", "The accepted task workspace or HEAD changed; revise the affected plan before new work.")
            if method == "create_worktree":
                if arguments["expected_workspace_identity"] != accepted["workspace_identity"]:
                    raise Fault("context_mismatch", "Worktree creation must retain the accepted source workspace identity.")
                # The domain verifies the fixed base and destination grant. Creating
                # a tree never enrolls that destination as another task target.
                return
            if method in FILE_METHODS:
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
            if not same_live:
                raise Fault("task_effect_unresolved", "Reconcile the original outstanding effect before admitting overlapping work.")
        operation = dict(operation, admitted_at_ms=now_ms)
        record["operations"].append(operation)
        if not stopping:
            stage["state"] = "running"
            record["control"].update(state="running", pending_decision=None)
            record["handoff"] = None
        limit = records.MAX_ACTIVE_OPERATIONS - (0 if stopping else 1 if method == "control_execution" else 2)
        transitions.reserve(record, operation_limit=limit, new_operation=operation)

    def reconcile(self, view: dict) -> dict:
        record = view["record"]
        if record["terminal"] is not None:
            return view
        changes, expected = [], {}
        for operation in record["operations"]:
            if not records.pending(operation):
                continue
            expected[operation["id"]] = records.digest(operation)
            try:
                observed = self.app.task_receipts.read(operation)
            except Fault as error:
                result = operation.get("result") or {}
                if (error.code == "execution_not_found" and result.get("status") == "unknown"
                        and result.get("summary") == "Domain failure: resource_busy."
                        and self.app.task_receipts.unstarted_local(operation)):
                    changes.append((operation["id"],
                        task_receipts.receipt_result("failed", summary="Original resource_busy confirmed before local launch; no execution created."),
                        operation["locator"]))
                continue
            except OSError:
                continue
            result, locator = task_receipts.observation(operation, observed)
            changes.append((operation["id"], result, locator))
        return self._publish(record["task_id"], changes,
            operation_ids=[item["id"] for item in record["operations"]], expected=expected) if changes else view

    def _publish(self, identity: str, observations: list[tuple[str, dict, dict]], *, operation_ids=(), expected=None) -> dict:
        try:
            view = tasks.update(self.app.state, identity,
                lambda value, now: task_observation.apply_observations(value, now, observations, expected=expected), observation=True,
                operation_ids=[*operation_ids, *(item[0] for item in observations)])
        except (Fault, OSError) as error:
            raise Fault("task_result_unverified", "Original domain work may have finished, but task evidence publication was not verified. Read the same task and original operation; do not repeat its effect.",
                        {"task_id": identity, "operation_ids": [item[0] for item in observations]}) from error
        if view["status"] == "saved":
            self.app.task_observer.notify()
        return view
