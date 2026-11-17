"""Context/frame export guard, separate from C1 and effect I/O control."""
from contextlib import contextmanager
from dataclasses import dataclass, field

from agent_native.state import Patch, Update, deep_freeze, thaw, strict_equal
from agent_native.state.tracking import ReadSet
from agent_native.kernel import PreparedChange, Status
from agent_native.kernel.coordinator import BusyPolicy, CoordinatorBusy, Phase
from .bindings import resource_record, identity
from agent_native.kernel._grant_authority import GrantIntegrityError
from .policy import ContextUnavailable, ContextPlan


@dataclass(frozen=True, slots=True)
class PlanConsumption:
    """Engine-issued minimal C5 handoff, not a complete request state machine.

    C5 can combine this prepare's roots/task bookkeeping Patch with its own
    request transition in ONE coordinator transaction. It must preserve these
    expected task values and perform its own request admission checks.
    """
    plan: ContextPlan
    patch: Patch
    expected_task: object
    retained_reads: ReadSet
    _gate: object = field(repr=False, compare=False)

    def prepare(self, managed):
        with self._gate.coordinator.read() as cut:
            self._gate.validate(self.plan, cut=cut)
        return PreparedChange(self.patch)


class ContextExportGate:
    """Demand refresh and gate explicit context/frame export, never I/O control.

    Generic Coordinator.capture intentionally stays usable for effect control.
    The host must use this gate for context/C4 export; no pixels, provider sends,
    leases or full outgoing-envelope implementation are claimed here.
    """
    def __init__(self, coordinator, index):
        if coordinator.priority_keys != index.required_keys:
            raise ValueError("Pass the index's one required_keys tuple at SDK bootstrap")
        self.coordinator, self.index = coordinator, index
        # Standalone host startup/accepted restore must validate the full graph
        # once. Historical inactive rows remain data; live use is checked later.
        try:
            with coordinator.read() as cut:
                index.authority.validate_graph(cut.view)
        except GrantIntegrityError as error:
            raise ContextUnavailable() from error

    def refresh(self):
        """One memory-only attempt; a committed tick does not imply ready roots."""
        return self.coordinator.submit("context-refresh", lambda view: PreparedChange(Patch()),
                                       memory_only=True, busy=BusyPolicy.REJECT)

    def current(self, request, *, refresh=True):
        try:
            with self.coordinator.read() as cut:
                try:
                    self.index.require_ready(cut)
                    ready = True
                except ContextUnavailable:
                    ready = False
            if not ready and refresh:
                result = self.refresh()
                if result.status != Status.COMMITTED:
                    raise ContextUnavailable()
            with self.coordinator.capture("context-plan") as cut:
                return self.index.plan(cut, request)
        except CoordinatorBusy as error:
            raise ContextUnavailable() from error

    def validate(self, plan, *, cut=None, destination=None):
        """Validate against CURRENT guarded authority on every call path.

        An optional issued cut is a coherence assertion, never an authority
        substitute. Historical issuance alone cannot authorize current export.
        """
        if self.coordinator.phase == Phase.IDLE:
            with self.coordinator.capture("context-validate") as current:
                return self._validate_current(plan, current, cut, destination)
        if self.coordinator.phase not in (Phase.CAPTURING, Phase.PREPARING):
            raise ContextUnavailable()
        # Internal caller already owns a semantic guard (capture/consume prepare).
        with self.coordinator.read() as current:
            return self._validate_current(plan, current, cut, destination)

    def _validate_current(self, plan, current, supplied, destination):
        if type(plan) is not ContextPlan or plan._issuer is not self.index._issuer:
            raise ContextUnavailable()
        if destination is not None and destination != plan.policy.request.destination:
            raise ContextUnavailable()
        if supplied is not None:
            if (not self.coordinator.validate_cut(supplied)
                    or supplied.identity != current.identity
                    or supplied.derived_revision != current.derived_revision
                    or any(supplied.derived_entries.get(key) is None
                        or supplied.derived_entries[key].version != current.derived_entries[key].version
                        for key in self.index.required_keys)):
                raise ContextUnavailable()
        self.index.require_ready(current)
        versions = {key: entry.version for key, entry in current.derived_entries.items()}
        if (plan.cut != current.identity or plan._derived_revision != current.derived_revision
                or plan.policy._issuer is not self.index._issuer
                or not plan.policy.allowed
                or not plan.read_set.matches(current.view, query_versions=versions)
                or not plan.policy.read_set.matches(current.view, query_versions=versions)):
            raise ContextUnavailable()
        live = self.index.policy(current, plan.policy.request)
        if not live.allowed or live.task_generation != plan.policy.task_generation:
            raise ContextUnavailable()
        return True

    @contextmanager
    def capture(self, plan, *, destination=None):
        """Serialize final live validation with consumer frame preparation."""
        with self.coordinator.capture("context-export") as cut:
            self.validate(plan, cut=cut, destination=destination)
            yield cut

    def prepare_consume(self, plan, *, request_id=None):
        """Validate old tokens and derive actual consumed identities only.

        request_id is optional trusted C5 bookkeeping, not model input. This
        helper does not create/admit a RequestAdmission or call a provider.
        Seal must use the resulting single coherent commit, not the old cut.
        """
        if request_id is not None and (type(request_id) is not str or not request_id):
            raise TypeError("Use an exact trusted request ID")
        with self.coordinator.read() as cut:
            self.validate(plan, cut=cut)
            task = plan.policy.request.task
            expected = thaw(cut.view.resolve(task).values)
            roots = {identity(v) for v in expected["working_resources"]}
            roots.update(plan.root_identities)
            expected["working_resources"] = tuple(resource_record(v) for v in sorted(roots, key=repr))
            changes = {"working_resources": expected["working_resources"]}
            if request_id is not None:
                expected["active_request"] = request_id
                changes["active_request"] = request_id
            # Own TaskSession record/working field is the ONLY dependency changed
            # by this helper. All other task fields are checked by expected_task.
            events = tuple(e for e in plan.read_set.events if not (
                e.key == task.key and (e.kind == "record" or
                    (e.kind == "field" and e.field in changes))))
            retained = ReadSet(plan.read_set.epoch, plan.read_set.schema_manifest,
                               events, (), plan.read_set.policy_revision,
                               unresolved=plan.read_set.unresolved)
            return PlanConsumption(plan, Patch(updates=(Update(task, changes),)),
                                   deep_freeze(expected), retained, self)

    def seal_consumed(self, proof):
        """Return a NEW plan on the resulting cut; never silently rebase stale input."""
        if type(proof) is not PlanConsumption or proof._gate is not self:
            raise ContextUnavailable()
        with self.coordinator.read() as cut:
            old = proof.plan.cut
            if (cut.identity.epoch != old.epoch or cut.identity.schema_manifest != old.schema_manifest
                    or cut.identity.sequence != old.sequence + 1
                    or not proof.retained_reads.matches(cut.view)
                    or not strict_equal(cut.view.resolve(proof.plan.policy.request.task).values,
                                        proof.expected_task)):
                raise ContextUnavailable()
            plan = self.index.plan(cut, proof.plan.policy.request)
            self.validate(plan, cut=cut)
            return plan
