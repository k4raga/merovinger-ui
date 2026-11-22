"""Generic UI-session command lifecycle over the existing C2 kernel.

Only the GUI pump advances this state machine. The private task/grant/request
bookkeeping belongs to C1/C2, not to an application callback or second engine.
"""
from uuid import uuid4
from collections.abc import Mapping

from agent_native.schema import Ref, CollectionRef
from agent_native.kernel import Status, ResolvedCommandPermissions
from agent_native.kernel.commands import AdmissionError
from agent_native.kernel._grant_authority import GrantIntegrityError
from agent_native.kernel.coordinator import Phase
from .declarations import INPUT


def wire(value, event_value):
    if value is INPUT:
        return wire(event_value, None)
    if type(value) is Ref:
        return value.key.as_dict()
    if type(value) is CollectionRef:
        return {"owner": value.owner.as_dict(), "field": value.field}
    if isinstance(value, Mapping):
        return {k: wire(v, event_value) for k, v in value.items()}
    if type(value) in (tuple, list):
        return [wire(v, event_value) for v in value]
    if type(value) in (str, int, float, bool, type(None)):
        return value
    raise TypeError("Unsupported command argument")


class UiSessionBroker:
    """Issue SAME-profile UI grants under explicit trusted kernel host policy.

    commands are registered identities, not authored permission declarations.
    Frozen issuance permissions are only the expected scope; actual C1 proof is
    required before adoption after ACK or definitive external reconciliation.
    Action-free views issue a real read grant with an empty command scope.
    """
    def __init__(self, engine, resources, commands):
        engine.assert_owner()
        self.engine, self.kernel = engine, engine.kernel
        self.host = self.kernel.authenticated(engine.principal)
        self.origin = self.kernel.authenticated(engine.principal, channel="ui")
        self.resources, self.commands = tuple(resources), tuple(commands)
        self.permissions = None
        self.task = self.grant = self.basis = self.delivery = None
        self.pending_grant = None
        self.stage, self.expected, self.pending = "new", None, None
        self.last_result = None
        self.last_displayed_basis = None
        self._terminal = None
        self._invocation = None

    @property
    def ready(self):
        return self.stage == "ready"

    @property
    def holding(self):
        return self.stage in ("action_grant", "request", "delivery", "closing", "closing_view")

    def _wait(self, stage, submission):
        self.engine.assert_owner()
        self.stage, self.expected = stage, submission.operation_id
        self._terminal = None
        if submission.status in (Status.AWAITING_DB, Status.RECONCILING):
            self.engine.pump.driver.turn.watch_operation(self.engine.pump.registration,
                submission.operation_id, self._accept_outcome)
            if submission.status == Status.RECONCILING and self._invocation is not None:
                self._invocation._publish("unknown", submission.error, final=False)
        elif submission.status == Status.COMMITTED:
            self._terminal = submission
        else:
            self._finish("failed", submission.error)

    def start(self):
        self.engine.assert_owner()
        if self.stage != "new":
            raise RuntimeError("UI session already started")
        task = self.kernel.create_task(self.host)
        self.task = task.ref
        self._wait("task", task.submission)

    def _accept_outcome(self, result):
        self.engine.assert_owner()
        if result.operation_id != self.expected:
            return
        if result.status in (Status.COMMITTED, Status.FAILED):
            self._terminal = result
        elif result.status == Status.RECONCILING and self._invocation is not None:
            self._invocation._publish("unknown", result.error, final=False)

    def advance(self, outcomes):
        """Compatibility one-step drive; terminal DTOs are retained, never authority."""
        self.engine.assert_owner()
        for result in outcomes:
            self._accept_outcome(result)
        return self.engine.pump.driver.turn.dispatch(self.engine.pump.registration,self.step_once)

    def step_once(self):
        self.engine.assert_owner()
        if self.engine.coordinator.phase != Phase.IDLE:
            return None
        if self.stage == "new":
            self.start()
            return None
        if self.stage == "closing_view":
            self.engine.complete_close()
            self.stage, self.expected = "closed", None
            return None
        terminal = self._terminal
        if terminal is None:
            # Positive C1 identities/scope, not Idle or journal absence, can
            # prove an external reconciliation published the expected stage.
            if self.engine.coordinator.phase!=Phase.IDLE:
                return
            if self.stage=="delivery":
                receipt=self.kernel.receipt(self.origin,self.delivery)
                if receipt.receipt is not None or receipt.outcome == "failed_no_effect":
                    self._finish_delivery(receipt, lookup=True)
                elif self._invocation is not None:
                    self._invocation._publish("unknown", receipt.error or receipt.outcome, final=False)
                return
            if not self._positive_current():
                return
        if terminal is not None and terminal.status == Status.FAILED:
            self._finish("failed_no_effect", terminal.error)
            return
        # Even a correlated COMMITTED DTO is not authority: inspect actual C1.
        if terminal is not None and self.stage in ("task", "grant", "action_grant", "request", "closing"):
            if not self._positive_current():
                self._finish("conflict", "session_scope_mismatch")
                return
        if self.stage == "task":
            if self.commands:
                self._issue_grant("grant", self.resources)
            else:
                self._issue_grant("grant", self.resources)
        elif self.stage == "grant":
            self._adopt_grant()
            self.stage, self.expected = "ready", None
            self.engine.attach_session(self.task)
        elif self.stage == "action_grant":
            self._adopt_grant()
            self._open_pending_request()
        elif self.stage == "request":
            action, value = self.pending
            descriptor = self.engine.command_descriptor(action.command)
            try:
                self.delivery = self.kernel.delivery_context(self.origin, self.basis, self.grant)
            except AdmissionError as error:
                self._finish("conflict",error.code)
                return
            try:
                arguments = {k: wire(v, value) for k, v in action.arguments.items()}
            except TypeError:
                self._finish("failed", "invalid_arguments")
                return
            proposal = {"command": descriptor.definition_id, "schema_revision": descriptor.revision,
                "schema_fingerprint": descriptor.fingerprint, "basis": self.basis.handle,
                "arguments": arguments}
            result = self.kernel.deliver(self.origin, self.delivery, proposal)
            if result.submission is not None and result.submission.status in (Status.AWAITING_DB, Status.RECONCILING):
                self._wait("delivery", result.submission)
            else:
                self._finish_delivery(result)
        elif self.stage == "closing":
            self.stage, self.expected, self._terminal = "closing_view", None, None
        elif self.stage == "delivery":
            self._finish_delivery(self.kernel.receipt(self.origin, self.delivery), lookup=True)

    def _positive_current(self):
        """Correlated canonical creation/state proof after definitive reconcile.

        A missing/different row never means rollback or permission to resend.
        UUID entity identities, task generation and exact grant/request scope
        must match the host-issued operation we are already waiting for.
        """
        self.engine.assert_owner()
        from agent_native.schema import EntityKey
        from agent_native.context import identity
        with self.engine.coordinator.read() as cut:
            view=cut.view
            if self.task is None or not view.exists(self.task):
                return False
            task=view.resolve(self.task).values
            if task["principal"]!=self.engine.principal or task["generation"]!=1:
                return False
            if self.stage=="closing":
                return (task["state"]=="completed" and task["active_request"] is None
                        and not task["working_resources"])
            if task["state"]!="active":
                return False
            if self.stage=="task":
                return task["active_request"] is None and not task["working_resources"]
            if self.stage in ("grant","action_grant"):
                if self.pending_grant is None:
                    return False
                grant_ref,scope=self.pending_grant
                if (type(scope) is not ResolvedCommandPermissions or scope.commands != self.commands
                        or not view.exists(grant_ref)):
                    return False
                grant=view.resolve(grant_ref).values
                actual={identity(value) for value in grant["resources"]}
                expected=set(scope.resources)
                expected.update(Ref(value.owner) for value in scope.resources if type(value) is CollectionRef)
                return (grant["principal"]==self.engine.principal and grant["task"]==self.task
                    and grant["task_generation"]==1 and grant["owner_activity"] is None
                    and grant["kind"]=="authority" and actual==expected
                    and len(grant["resources"])==len(actual)
                    and tuple(grant["actions"])==scope.actions and tuple(grant["create_types"])==scope.create_types
                    and not grant["revoked"] and grant["parent"] is None and grant["plan_id"] is None
                    and grant["allow_activity_transfer"] is scope.allow_activity_transfer
                    and not grant["created_resources"]
                    and grant["source_invocation"] is None)
            if self.stage=="request" and self.basis is not None:
                basis=self.basis
                ref=Ref(EntityKey(self.kernel.system.RequestAdmission.__entity_type__,basis.request_id,1))
                if not view.exists(ref) or basis.task!=self.task or basis.task_generation!=1:
                    return False
                request=view.resolve(ref).values
                return (task["active_request"]==basis.request_id and request["task"]==self.task
                    and request["task_generation"]==basis.task_generation and request["request_id"]==basis.request_id
                    and request["basis_handle"]==basis.handle and request["basis_kind"]=="ui"
                    and tuple(request["call_slots"])==("0",) and request["state"]=="open"
                    and request["approval_id"] is None and request["invocation_id"] is None)
            return False

    def invoke(self, action, value=None, *, _invocation=None):
        self.engine.assert_owner()
        coor = self.engine.coordinator
        if not self.ready or coor.phase != Phase.IDLE:
            return self.engine.refuse("busy")
        if action is None or self.grant is None:
            return self.engine.refuse("command_not_granted")
        try:
            valid=coor.validate_cut(self.engine.displayed)
        except Exception:
            valid=False
        if not valid:
            return self.engine.refuse("display_expired")
        with coor.read() as current:
            if not self.engine.matches_cut(current,self.engine.displayed):
                self.engine.refresh()
                return self.engine.refuse("display_conflict")
        # The main-thread call stack cannot yield between equality and issue.
        request_id = uuid4().hex if _invocation is None else _invocation.invocation_id
        try:
            self.basis = self.kernel.issue_basis(self.host, self.task, request_id, kind="ui")
        except AdmissionError as error:
            return self.engine.refuse(error.code)
        with coor.read() as current:
            exact=(self.engine.matches_cut(self.basis.cut,self.engine.displayed)
                   and self.engine.matches_cut(current,self.basis.cut))
        if not exact:
            self.kernel.release_basis(self.basis.handle)
            self.basis = None
            self.engine.refresh()
            return self.engine.refuse("display_conflict")
        self.last_displayed_basis = self.basis.cut.identity
        self.pending = (action, value)
        self._invocation = _invocation
        self.stage = "request"  # Hold BEFORE synchronous submission/notifications.
        try:
            desired=self.engine.command_resources(self.basis.cut)
        except GrantIntegrityError:
            self._finish("denied", "activity_backlink_unverified")
            return self.engine.refuse("activity_backlink_unverified")
        if desired != self.resources:
            self._issue_grant("action_grant", desired)
        else:
            self._open_pending_request()
        if self.stage not in ("action_grant", "request"):
            return self.engine.refuse(self.last_result[1] or self.last_result[0])
        self.engine.set_domain_busy(True)
        self.engine.set_outcome("accepted_command")
        return "accepted"

    def close(self):
        self.engine.assert_owner()
        if not self.ready and self.stage!="failed":
            return self.engine.refuse("close_busy")
        with self.engine.coordinator.read() as cut:
            exists=self.task is not None and cut.view.exists(self.task)
        if not exists:
            self.stage="closing_view"
            return "closing"
        self.stage="closing"
        ended=self.kernel.end_task(self.host,self.task)
        self._wait("closing",ended.submission)
        self.engine.set_domain_busy(True)
        return "closing"

    def _issue_grant(self, stage, resources):
        """Resolve only for NEW issuance; ACK/reconcile never reruns policy."""
        self.engine.assert_owner()
        try:
            if self.commands:
                result = self.kernel.issue_command_grant(self.host, self.task, channel="ui",
                    commands=self.commands, resources=resources)
            else:
                result = self.kernel.issue_read_grant(self.host, self.task, resources=resources)
        except AdmissionError as error:
            self._finish("denied", error.code)
            return
        self.pending_grant = (result.ref, result.permissions)
        self._wait(stage, result.submission)

    def _adopt_grant(self):
        self.engine.assert_owner()
        self.grant, self.permissions = self.pending_grant
        self.resources = self.permissions.resources
        self.pending_grant = None

    def _open_pending_request(self):
        self.engine.assert_owner()
        try:
            opened=self.kernel.open_request(self.host,self.basis)
        except AdmissionError as error:
            self._finish("conflict",error.code)
            return
        self._wait("request",opened.submission)

    def _finish_delivery(self, result, *, lookup=False):
        # A refusal to read a receipt does not prove the command was refused.
        no_effect = result.outcome == "failed_no_effect" or (
            not lookup and result.outcome in ("busy", "conflict", "denied", "failed"))
        self._finish(result.outcome, result.error, receipt=result.receipt, uncertain=not no_effect)

    def _finish(self, outcome, error=None, *, receipt=None, uncertain=False):
        self.engine.assert_owner()
        self.last_result = (outcome, error)
        if self.basis is not None:
            self.kernel.release_basis(self.basis.handle)
        self.basis = self.delivery = self.pending = self.expected = None
        self.pending_grant = None
        self._terminal = None
        invocation, self._invocation = self._invocation, None
        self.stage = "ready" if self.grant is not None else "failed"
        self.engine.set_domain_busy(not self.ready)
        self.engine.set_outcome(error or outcome)
        self.engine.dirty = True
        if invocation is not None:
            if receipt is not None:
                invocation._publish(receipt.outcome, error, receipt)
            elif uncertain:
                invocation._publish("unknown", error or outcome)
            else:
                invocation._publish("failed_no_effect", error or outcome)


# Compatibility import; lifetime and driving live in the shared host adapter.
from .host import GuiPump
