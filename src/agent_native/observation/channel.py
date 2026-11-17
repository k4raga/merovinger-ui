"""Demand-driven C4 export with current C2 authority and issued provenance.

Host lifecycle is main-thread/pump based. After close() call pump() until drained:
coroutine/request cancellation is not evidence that a CPU worker has stopped.
Provider serialization is separate from TEST envelopes; this owner never sends HTTP.
"""
from dataclasses import dataclass,fields,is_dataclass
from time import monotonic_ns
from math import ceil
from uuid import uuid4
import json
from queue import SimpleQueue, Empty
from weakref import WeakValueDictionary
from types import SimpleNamespace
from collections.abc import Mapping

from agent_native.context import ContextRequest,ContextUnavailable
from agent_native.kernel.commands import CommandKernel
from agent_native.kernel.coordinator import Phase
from agent_native.schema import Ref
from agent_native.state import deep_freeze,thaw,strict_equal,Patch
from ._export_types import ExportError,ExportLimits,_owner,_json_bound,_proof_bound
from ._export_runtime import _runtime
from .assets import AssetHandle
from .envelope import FragmentHandle,SealedEnvelope
from .frames import _move_raw,CaptureError
from .export_policy import _authorize_pixels,_encode_approved,_png_bound


@dataclass(frozen=True,slots=True)
class Destination:
    """Nonsecret exact provider/account/configuration identity selected by host."""
    identifier: str
    provider: str
    account: str
    configuration: str
    def __post_init__(self):
        if any(type(v) is not str or not v for v in (
                self.identifier,self.provider,self.account,self.configuration)):
            raise TypeError("Use exact nonempty destination identities")


@dataclass(frozen=True,slots=True)
class Observation:
    observation_id: str
    context: FragmentHandle
    assets: tuple[AssetHandle,...]
    visual_status: str
    def close(self):
        self.context.close()
        for asset in self.assets:
            asset.close()


@dataclass(frozen=True, slots=True)
class ModelImage:
    """An approved PNG at the observation's exact capture cut.

    Current conservative policy rejects the whole surface for denied paint
    dependencies; this does not promise per-region pixel masking.
    """
    png: bytes
    metadata: object
    stamp: object


class ModelPayload:
    """Owned host handoff; close releases managed bytes and asset leases.

    Receiving bytes is a trusted Python boundary. Copies retained by a host
    cannot subsequently be revoked; this object does not send or count a model
    request. Authorization is checked at materialization, before any handoff.
    """
    def __init__(self, broker, context_json, images, observation_id, cut, visual_status, charge, records):
        self._broker, self._charge, self._records = broker, charge, records
        self.context_json, self.images = context_json, images
        self.observation_id, self.cut = observation_id, cut
        self.visual_status = visual_status
        self.closed = False

    def close(self):
        _owner()
        if not self.closed:
            self.closed = True
            self.context_json, self.images = b"", ()
            for record in self._records:
                self._broker._rt.assets.unpin(record)
            self._records = ()
            self._charge.close()
            self._broker._model_payloads.discard(self)
            self._broker._rt.pump()

    def __enter__(self):
        if self.closed:
            raise ExportError("model_payload_closed")
        return self

    def __exit__(self, *error):
        self.close()


_DEMAND_KEY,_RECEIPT_KEY = object(),object()


@dataclass(frozen=True,slots=True,weakref_slot=True,init=False)
class DemandHandle:
    _broker: object
    _token: str
    def __init__(self,broker,token,*,_key=None):
        if _key is not _DEMAND_KEY:
            raise TypeError("Use ObservationChannel")
        object.__setattr__(self,"_broker",broker)
        object.__setattr__(self,"_token",token)
    def result(self):
        """Take one result; pending never performs hidden event-loop pumping."""
        return self._broker._take(self)
    def cancel(self):
        self._broker._cancel(self)


@dataclass(frozen=True,slots=True,weakref_slot=True,init=False)
class ReceiptHandle:
    _broker: object
    _token: str
    def __init__(self,broker,token,*,_key=None):
        if _key is not _RECEIPT_KEY:
            raise TypeError("Use ExportBroker.receipt")
        object.__setattr__(self,"_broker",broker)
        object.__setattr__(self,"_token",token)


class ExportBroker:
    """One authenticated destination-scoped session over shared runtime budgets.

    Constructors/intent/output/receipt factories are trusted SDK host seams, not
    model tool parsers. Tools expose only closed current/next/inspect/crop calls.
    Trusted Python is not sandboxed. Direct provider serialization is fixed and
    fully accounted by seal_direct; the existing seal/consume_wire API is TEST.
    """
    def __init__(self,kernel,index,gate,capture,origin,request,destination,*,
                 limits=None,clock_ns=monotonic_ns,literals=(),program_runtime=None):
        _owner()
        if (type(kernel) is not CommandKernel or type(request) is not ContextRequest
                or type(destination) is not Destination or gate.index is not index
                or gate.coordinator is not kernel.coordinator
                or capture.coordinator is not kernel.coordinator
                or destination.identifier != request.destination):
            raise TypeError("Use one engine and exact destination configuration")
        kernel._auth(origin)
        if origin.principal != request.actor:
            raise ExportError()
        self.kernel,self.index,self.gate,self.capture = kernel,index,gate,capture
        self.coordinator,self.origin,self.request,self.destination = kernel.coordinator,origin,request,destination
        self.limits = ExportLimits() if limits is None else limits
        if type(self.limits) is not ExportLimits:
            raise TypeError("Use ExportLimits")
        self._closed = False
        self._memory_contexts = {}
        self._model_payloads = set()
        self._program_runtime = program_runtime
        if program_runtime is not None:
            from agent_native.programming.runtime import ProgramRuntime
            if (type(program_runtime) is not ProgramRuntime or program_runtime.kernel is not kernel
                    or program_runtime.origin.principal != request.actor):
                raise TypeError("Use the same engine's trusted program runtime")
        self._focus=request.focus
        self._demands,self._demand_handles = {},WeakValueDictionary()
        self._receipts,self._receipt_handles = {},WeakValueDictionary()
        self._literals = dict(literals)
        if (len(self._literals)!=len(tuple(literals)) or any(type(k)is not str or type(v)is not str
                for k,v in self._literals.items())):
            raise TypeError("Use fixed SDK literal declarations")
        plan = gate.current(request)
        if not plan.policy.allowed:
            raise ExportError()
        self._scope = (request.actor,request.task,plan.policy.task_generation,request.grant,destination,uuid4().hex)
        self._rt = _runtime(self.coordinator,self.limits,clock_ns)
        if len(self._rt.sessions) >= self.limits.max_demands:
            raise ExportError("export_quota_exceeded")
        self._rt.sessions[id(self)] = self

    def _auth(self):
        _owner()
        if self._closed:
            raise ExportError("export_session_closed")
        try:
            self.kernel._auth(self.origin)
        except Exception:
            raise ExportError() from None
        if (self.origin.principal != self.request.actor
                or self.request.actor!=self._scope[0] or self.request.task!=self._scope[1]
                or self.request.grant!=self._scope[3] or self.destination!=self._scope[4]
                or self.request.destination!=self.destination.identifier or self.request.focus!=self._focus):
            raise ExportError()

    def _validate_current(self,cut):
        self._auth()
        policy = self.index.policy(cut,self.request)
        if not policy.allowed or policy.task_generation != self._scope[2]:
            raise ExportError()
        self.index.require_ready(cut)
        return policy

    def _expiry(self):
        return self._rt.budget.now()+self.limits.ttl_ns

    def _traces(self,cut,traces,*,retarget=False):
        self._validate_current(cut)
        with self.index.projection(cut,self.request) as projector:
            for trace in traces:
                decision = projector.retarget(trace) if (retarget or trace.request_key[-1]!=self.request.destination) else projector.recheck(trace)
                if not decision.allowed:
                    raise ExportError()
        return True

    def _fragment(self,kind,value,traces=(),assets=(),*,expires=None):
        return self._rt.fragments.add(kind,value,traces,assets,self._scope,
            self._expiry() if expires is None else min(expires,self._expiry()))

    def literal(self,identifier):
        self._auth()
        if identifier not in self._literals:
            raise ExportError()
        with self.coordinator.capture("export-literal") as cut:
            self._validate_current(cut)
            return self._fragment("sdk_literal",self._literals[identifier])

    def user_intent(self,user_origin,text):
        """SDK-only explicit user input, never called by model tool arguments."""
        self._auth()
        try:
            self.kernel._auth(user_origin)
        except Exception:
            raise ExportError() from None
        if (user_origin.principal != self.request.actor or user_origin.channel not in ("host","ui")
                or type(text) is not str):
            raise ExportError()
        with self.coordinator.capture("export-user-intent") as cut:
            self._validate_current(cut)
            return self._fragment("user_intent",text)

    def _memory_session(self,cut):
        d=self.kernel.system
        refs=[r for r in cut.view.entities(d.RunnerSession.__entity_type__)
            if cut.view.field(r,"task")==self.request.task
            and cut.view.field(r,"principal")==self.request.actor]
        if len(refs)!=1:raise ExportError("history_unavailable")
        return refs[0]

    def prepare_memory(self,profile,*,previous=None,history_byte_budget=None,compaction_prefix_end=None,allow_compaction=True):
        """SDK byte planner on one original cut, before any trial request writes.

        Retrying reuses that original private snapshot. It never retags it as a
        newer cut, samples replacement values, or manufactures token evidence.
        Final history factories/current checks and complete packet caps remain.
        Optional compaction is planning policy only: a retry may preserve/narrow
        that permission, never widen it or reserve/refund runner budget.
        """
        if type(allow_compaction) is not bool:
            raise TypeError("Use exact boolean allow_compaction")
        from .history import MemoryContext,policy_digest
        from agent_native.runner.memory.runtime import MemoryRuntime
        from agent_native.runner.memory.visible import VisibleMemoryPolicy
        from agent_native.runner.memory import ModelPin
        from agent_native.kernel._input_lineage import native_digest
        self._auth()
        policy=VisibleMemoryPolicy.for_installed_profile(profile)
        if previous is not None:
            if (type(previous) is not MemoryContext or previous.broker is not self or previous.closed
                    or previous.policy.config_digest!=policy.config_digest):
                raise ExportError("history_unavailable")
            context=previous
            if allow_compaction and not context.preparation.allow_compaction:
                raise ExportError("history_retry_invalid")
            if history_byte_budget is not None and history_byte_budget>=context.preparation.history_byte_budget:
                raise ExportError("history_retry_invalid")
            retry_budget=context.preparation.history_byte_budget if history_byte_budget is None else history_byte_budget
            working=self._rt.budget.reserve("working",8*context.charge.size)
            try:
                context.summary_preflight=None
                context.preparation=MemoryRuntime(self.kernel).prepare_visible(context.snapshot,policy=policy,
                    summarizer_pin=context.pin,summarizer_context_digest=context.context_digest,
                    history_byte_budget=retry_budget,compaction_prefix_end=compaction_prefix_end,
                    allow_compaction=allow_compaction)
                return context
            finally:working.close()
        if len(self._memory_contexts)>=self.limits.max_demands:raise ExportError("export_quota_exceeded")
        retained=working=None
        try:
            with self.coordinator.capture("memory-plan") as cut:
                self._validate_current(cut)
                session=self._memory_session(cut);conversation=cut.view.field(session,"conversation")
                logical=cut.view.field(conversation,"used")["bytes"]
                bound=logical+2*1024*1024+65536
                retained=self._rt.budget.reserve("retained",bound)
                working=self._rt.budget.reserve("working",8*bound)
                pin=ModelPin(profile.provider_id,profile.model,profile.account_id,profile.config_digest,None,policy.serializer_digest)
                context_digest=native_digest(("agent-native.summarizer-context/1",policy.config_digest))
                snapshot=MemoryRuntime(self.kernel).snapshot_from_cut(cut,conversation,
                    policy_digest=policy_digest(self,cut,conversation))
                prepared=MemoryRuntime(self.kernel).prepare_visible(snapshot,policy=policy,
                    summarizer_pin=pin,summarizer_context_digest=context_digest,
                    history_byte_budget=history_byte_budget,compaction_prefix_end=compaction_prefix_end,
                    allow_compaction=allow_compaction)
                context=MemoryContext(self,snapshot,policy,pin,context_digest,prepared,retained)
                self._memory_contexts[id(context)]=context;retained=None
                return context
        finally:
            if working is not None:working.close()
            if retained is not None:retained.close()

    def preflight_summarize(self,profile,context,*,output_reservation):
        """Exact complete summary packet preflight with zero C1 trial writes."""
        from .history import MemoryContext,summary_literal,original_union
        from agent_native.runner.direct import serialize_installed_request
        from hashlib import sha256
        self._auth()
        if (type(context) is not MemoryContext or context.broker is not self or context.closed
                or context.preparation.purpose!="summarize"):
            raise ExportError("history_unavailable")
        data=context.preparation.serialized_history
        bound=32768+32*len(data)+_json_bound(profile.model)
        if bound>self.limits.max_envelope_bytes:raise ExportError("export_quota_exceeded")
        working=self._rt.budget.reserve("working",8*bound+8*(2*1024*1024+65536))
        try:
            with self.coordinator.capture("summary-packet-preflight") as cut:
                self._validate_current(cut)
                original_union(self,cut,context.snapshot.conversation,context.preparation.selected_source_keys,context.snapshot.summary_ref,require_closed=True)
                raw=serialize_installed_request(profile,[{"kind":"sdk_literal","content":summary_literal()},
                    {"kind":"summarize","content":data.decode("utf-8")}],[],output_reservation=output_reservation)
                if len(raw)>bound:raise ExportError("export_quota_exceeded")
                context.summary_preflight=(context.preparation.candidate_digest,sha256(raw).hexdigest(),len(raw),output_reservation)
                return len(raw)
        finally:working.close()

    def _history_binding(self,cut,sealed,context):
        from .history import MemoryContext,HistoryBinding,original_union
        from agent_native.kernel._input_lineage import native_digest
        if (type(context) is not MemoryContext or context.broker is not self or context.closed
                or self._memory_contexts.get(id(context)) is not context):
            raise ExportError("history_unavailable")
        preparation=context.preparation
        purpose=sealed.preparation.spec.purpose
        if preparation.status not in ("limited_visible_input","need_compaction"):
            raise ExportError("history_unavailable")
        if (purpose=="summarize")!=(preparation.purpose=="summarize"):
            raise ExportError("history_purpose_mismatch")
        if purpose=="summarize" and sealed.preparation.spec.compaction_demand!=preparation.demand:
            raise ExportError("history_demand_mismatch")
        values=self._prepared_request(sealed,cut)
        conversation=cut.view.field(values["session"],"conversation")
        current_summary=cut.view.field(conversation,"current_summary_id")
        expected_summary=None if context.snapshot.summary_ref is None else context.snapshot.summary_ref.key.entity_id
        if (conversation!=preparation.conversation
                or cut.view.field(conversation,"head_sequence")!=context.snapshot.window.head_sequence
                or cut.view.field(conversation,"prefix_revision")!=context.snapshot.window.prefix_revision
                or current_summary!=expected_summary):raise ExportError("history_changed")
        claims,events,summary,reads,digest=original_union(self,cut,conversation,
            preparation.selected_source_keys,context.snapshot.summary_ref,require_closed=purpose=="summarize")
        if digest!=preparation.policy_digest:raise ExportError("history_changed")
        return HistoryBinding(preparation.selected_source_keys,context.snapshot.summary_ref,claims,events,
            summary,reads,digest,native_digest(preparation.serialized_history.decode("utf-8")),
            preparation.candidate_digest,context.policy.config_digest,preparation.serializer_digest,purpose,
            preparation.cut_epoch,preparation.cut_sequence)

    def _recheck_history(self,cut,record,sealed):
        from .history import original_union
        from agent_native.kernel._input_lineage import native_digest
        binding=record.prepared.get("history")
        if binding is None:return
        working=self._rt.budget.reserve("working",8*(2*1024*1024+65536))
        try:
            conversation=cut.view.field(sealed.preparation.spec.session,"conversation")
            claims,events,summary,reads,digest=original_union(self,cut,conversation,binding.source_keys,binding.summary_ref,require_closed=binding.purpose=="summarize")
            if (native_digest((claims,events,summary))!=native_digest((binding.claims,binding.selected_events,binding.summary)) or digest!=binding.policy_digest):
                raise ExportError("history_changed")
        finally:working.close()

    def authorize_history(self,cut,request_ref,conversation_ref,lineage_ref):
        """Genuine current C4 predicate used by existing summary CAS."""
        from .history import CurrentHistory,HistoryAuthorization,original_union,summary_literal
        from agent_native.kernel._input_lineage import native_digest
        from agent_native.kernel._conversation import decode_compaction_demand
        self._auth()
        working=self._rt.budget.reserve("working",8*(2*1024*1024+65536))
        checker=None
        try:
            checker=CurrentHistory(self,cut,conversation_ref,request_ref=request_ref)
            if cut.view.field(request_ref,"input_lineage")!=lineage_ref:
                raise ExportError("history_unavailable")
            lineage=checker.lineage(lineage_ref)
            if lineage["original_task"]!=self.request.task:
                raise ExportError("history_unavailable")
            if cut.view.field(request_ref,"purpose")=="summarize":
                attempt=cut.view.resolve(self.kernel._ref(self.kernel.system.CompactionAttempt,request_ref.key.entity_id)).values
                demand=decode_compaction_demand(thaw(attempt["demand"]))
                events=tuple(self.kernel._ref(self.kernel.system.ConversationEvent,key.event_id) for key in demand.source_keys)
                from .lineage import resource
                if tuple(resource(ref) for ref in events)!=lineage["selected_events"]:
                    raise ExportError("history_unavailable")
                parent=None if attempt["parent_summary_id"] is None else self.kernel._ref(self.kernel.system.ConversationSummary,attempt["parent_summary_id"])
                claims,_,summary,reads,digest=original_union(self,cut,conversation_ref,demand.source_keys,parent,require_closed=True)
                checker.reads.include(reads)
                expected={native_digest(claim) for claim in claims}
                literal=native_digest(summary_literal())
                extra=[claim for claim in lineage["claims"] if native_digest(claim) not in expected]
                if (summary!=lineage["summary"] or any(claim["kind"]!="sdk_source" or claim["source"] is not None
                        or claim["sdk_kind"]!="engine_literal" or claim["content_digest"]!=literal for claim in extra)
                        or not expected.issubset({native_digest(claim) for claim in lineage["claims"]})):
                    raise ExportError("history_unavailable")
            return HistoryAuthorization(True,checker.reads.read_set,checker.digest)
        finally:
            if checker is not None:checker.close()
            working.close()

    def _program_offer(self, sealed):
        """Live bound SDK component plus actual host-issued install authority."""
        runtime = self._program_runtime
        return (runtime is not None and sealed.preparation.spec.purpose == "act"
                and runtime.available and runtime.can_install(self.request.task, self.request.grant))

    def _check_program_offer(self, record, sealed):
        if record.prepared.get("sdk_program_offered", False) != self._program_offer(sealed):
            raise ExportError("sdk_capability_changed")

    def _recheck_skills(self,cut,record,sealed):
        if sealed.preparation.spec.purpose=="summarize":
            if record.prepared.get("skills") is not None:
                raise ExportError("skills_changed")
            return
        from .skills import recheck
        recheck(self,cut,sealed,record.prepared.get("skills"))

    def _new_demand(self,kind,*,surfaces=None,selector=None,receipt=None,condition=None,after_frame=None,deadline_ns=None):
        self._auth()
        total = sum(len(b._demands) for b in self._rt.sessions.values())
        if total >= self.limits.max_demands:
            raise ExportError("export_quota_exceeded")
        now = self._rt.budget.now()
        deadline = now+self.limits.ttl_ns if deadline_ns is None else deadline_ns
        if type(deadline)is not int or deadline<=now:
            raise ExportError("export_deadline")
        token = "demand-"+uuid4().hex
        demand = SimpleNamespace(token=token,kind=kind,surfaces=None if surfaces is None else tuple(surfaces),
            selector=selector,receipt=receipt,condition=condition,after_frame=after_frame,deadline=deadline,
            state="pending",result=None,error=None,job=None,attempts=0)
        handle = DemandHandle(self,token,_key=_DEMAND_KEY)
        self._demand_handles[id(handle)] = handle
        self._demands[token] = demand
        return handle

    def current(self,surface_ids=None,*,deadline_ns=None):
        return self._new_demand("current",surfaces=surface_ids,deadline_ns=deadline_ns)

    def prepared(self, sealed, surface_ids=None, *, deadline_ns=None):
        """Capture actual SEALED consumed plan; never rebase an older frame.

        The observation ID was chosen before prepare and persisted by C1. An
        intervening publication makes this demand fail instead of getting a new
        current plan with the old request identity.
        """
        self._auth()
        with self.coordinator.capture("export-prepared-demand") as cut:
            self._prepared_request(sealed, cut)
            self.gate.validate(sealed.plan, cut=cut)
        handle = self._new_demand("prepared", surfaces=surface_ids,
            deadline_ns=min(sealed.preparation.reservation.deadline_ns,
                self._expiry() if deadline_ns is None else deadline_ns))
        demand = self._demands[handle._token]
        demand.prepared, demand.native = sealed, None
        return handle

    def _prepared_request(self, sealed, cut):
        from agent_native.kernel._agent_requests import SealedAgentRequest
        if (type(sealed) is not SealedAgentRequest or sealed.preparation.sealed is not sealed
                or sealed.plan.policy.request != self.request
                or sealed.preparation.reservation.task != self.request.task):
            raise ExportError("prepared_observation_mismatch")
        spec = sealed.preparation.spec
        values = cut.view.resolve(sealed.request).values
        if (values["observation_id"] != spec.observation_id
                or values["provider_id"] != self.destination.provider
                or values["destination_id"] != self.destination.identifier
                or values["config_digest"] != self.destination.configuration
                or self._rt.budget.now() >= values["deadline_ns"]):
            raise ExportError("prepared_observation_mismatch")
        self._validate_current(cut)
        return values

    def next(self,after_frame,surface_ids=None,*,deadline_ns=None):
        self._auth()
        if type(after_frame)is not AssetHandle:
            raise ExportError()
        self._rt.assets.read(after_frame,self._scope)
        return self._new_demand("next",surfaces=surface_ids,after_frame=after_frame,deadline_ns=deadline_ns)

    def inspect(self,selector,*,deadline_ns=None):
        from agent_native.context._projection import Selector
        if type(selector)is not Selector:
            raise TypeError("Use a closed inspect Selector")
        return self._new_demand("inspect",selector=selector,deadline_ns=deadline_ns)

    def receipt(self,delivery):
        self._auth()
        result = self.kernel.receipt(self.origin,delivery)
        if (result.receipt is None or result.receipt.invocation is None
                or delivery.task!=self.request.task):
            raise ExportError()
        token="receipt-"+uuid4().hex
        if len(self._receipts)>=self.limits.max_fragments:
            raise ExportError("export_quota_exceeded")
        handle=ReceiptHandle(self,token,_key=_RECEIPT_KEY)
        self._receipt_handles[id(handle)]=handle
        self._receipts[token]=(delivery,result.receipt.invocation,self._expiry())
        return handle

    def _receipt(self,handle):
        if (type(handle)is not ReceiptHandle or handle._broker is not self
                or self._receipt_handles.get(id(handle))is not handle):
            raise ExportError()
        record=self._receipts.get(handle._token)
        if record is None or self._rt.budget.now()>=record[2]:
            raise ExportError()
        result=self.kernel.receipt(self.origin,record[0])
        if result.receipt is None or result.receipt.invocation!=record[1]:
            raise ExportError()
        return record[1]

    def after(self,receipt,condition="local_commit",surface_ids=None,*,deadline_ns=None):
        self._auth()
        if condition not in ("local_commit","effect_confirmed","effect_terminal"):
            raise ValueError("Use an explicit receipt condition")
        self._receipt(receipt)
        return self._new_demand("after",surfaces=surface_ids,receipt=receipt,condition=condition,deadline_ns=deadline_ns)

    def _condition(self,demand,cut):
        if demand.kind!="after":
            return True
        invocation=self._receipt(demand.receipt)
        if demand.condition=="local_commit":
            return True
        values=cut.view.resolve(invocation).values
        states=[]
        for ident in values["effect_ids"]:
            ref=self.kernel._ref(self.kernel.system.EffectIntent,ident)
            states.append(cut.view.field(ref,"state"))
        if demand.condition=="effect_confirmed":
            return bool(states) and all(s=="confirmed" for s in states)
        return bool(states) and all(s in ("confirmed","failed_no_effect","cancelled_before_effect") for s in states)

    def _get_demand(self,handle):
        _owner()
        if (type(handle)is not DemandHandle or handle._broker is not self
                or self._demand_handles.get(id(handle))is not handle):
            raise ExportError("observation_unavailable")
        demand=self._demands.get(handle._token)
        if demand is None:
            raise ExportError("observation_unavailable")
        return demand

    def _take(self,handle):
        demand=self._get_demand(handle)
        if demand.state in ("pending","working"):
            raise ExportError("observation_pending")
        self._demands.pop(demand.token,None)
        self._demand_handles.pop(id(handle),None)
        if demand.state!="ready":
            raise ExportError(demand.error or "observation_unavailable")
        self._auth()
        if type(demand.result)is AssetHandle:
            with self.coordinator.capture("export-crop-result") as cut:
                record=self._rt.assets.read(demand.result,self._scope)
                self._traces(cut,record.traces)
            return demand.result
        # Retained current authorization, not stale fresh-plan equality.
        with self.coordinator.capture("export-observation-result") as cut:
            fragment=self._rt.fragments.get(demand.result.context,self._scope)
            self._traces(cut,fragment.traces)
            for asset in demand.result.assets:
                record=self._rt.assets.read(asset,self._scope)
                self._traces(cut,record.traces)
        return demand.result

    def materialize(self, observation):
        """One production, provider-neutral context/PNG handoff to trusted host.

        Only an original issued observation is accepted. Current policy,
        provenance, scope and expiry are rechecked together before serialization.
        No TEST envelope, raw capture, provider SDK or Runner is involved.
        """
        self._auth()
        if type(observation) is not Observation:
            raise TypeError("Use an original Observation")
        records, retained, working = [], None, None
        try:
            with self.coordinator.capture("export-model-materialize") as cut:
                fragment = self._rt.fragments.get(observation.context, self._scope)
                if (getattr(fragment, "observation", None) is not observation
                        or fragment.materialized):
                    raise ExportError("observation_unavailable")
                self._traces(cut, fragment.traces)
                for asset in observation.assets:
                    record = self._rt.assets.pin(asset, self._scope)
                    records.append(record)
                    self._traces(cut, record.traces)
                    if (record.stamp.cut != fragment.observation_cut
                            or record.stamp.derived_revision != fragment.observation_revision):
                        raise ExportError("observation_unavailable")
                bound = _json_bound(fragment.value)
                self._rt.wires.check_slot()
                if bound + sum(len(r.encoded) for r in records) > self.limits.max_envelope_bytes:
                    raise ExportError("export_quota_exceeded")
                retained = self._rt.budget.reserve("retained", bound)
                working = self._rt.budget.reserve("working", 3 * bound)
                encoded = json.dumps(thaw(fragment.value), ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode("utf-8")
                if len(encoded) > bound:
                    raise ExportError("export_quota_exceeded")
                # Encoding may consume time: expiry is checked again at handoff.
                self._rt.fragments.get(observation.context, self._scope)
                for asset in observation.assets:
                    self._rt.assets.read(asset, self._scope)
                retained.resize(len(encoded))
                images = tuple(ModelImage(r.encoded, r.metadata, r.stamp) for r in records)
                payload = ModelPayload(self, encoded, images, observation.observation_id,
                                       fragment.observation_cut, observation.visual_status, retained, tuple(records))
                self._model_payloads.add(payload)
                fragment.materialized = True
                retained, records = None, []
            observation.close()
            return payload
        finally:
            if working is not None:
                working.close()
            if retained is not None:
                retained.close()
            for record in records:
                self._rt.assets.unpin(record)

    def _cancel(self,handle):
        demand=self._get_demand(handle)
        if demand.state=="ready":
            demand.result.close(); demand.result=None
        demand.state,demand.error="cancelled","observation_cancelled"

    def pump(self):
        """Owner-thread host tick; usable after close solely to drain real jobs."""
        _owner()
        self._rt.pump()
        now=self._rt.budget.now()
        for token,record in tuple(self._receipts.items()):
            if now>=record[2]:
                self._receipts.pop(token,None)
                for ident,handle in tuple(self._receipt_handles.items()):
                    if handle._token==token:
                        self._receipt_handles.pop(ident,None)
        if not self._closed:
            for demand in tuple(self._demands.values()):
                if demand.state=="pending":
                    if self._rt.budget.now()>=demand.deadline:
                        demand.state,demand.error="failed","export_deadline"
                    elif self.coordinator.phase==Phase.IDLE:
                        try:
                            self._begin(demand)
                        except ExportError as error:
                            demand.state="failed"
                            demand.error=error.code if error.code in("export_quota_exceeded","export_deadline")else"observation_unavailable"
                        except CaptureError as error:
                            if error.code=="capture_revision_changed" and demand.attempts<3:
                                continue
                            demand.state,demand.error="failed","observation_unavailable"
                        except Exception:
                            demand.state,demand.error="failed","observation_unavailable"
        return self.drained

    @property
    def drained(self):
        _owner()
        return not any(j.broker is self for j in self._rt.jobs.values())

    def measurements(self):
        _owner()
        self._rt.sweep()
        return self._rt.budget.measurements()

    def close(self):
        _owner()
        if not self._closed:
            for context in tuple(self._memory_contexts.values()):context.close()
            for payload in tuple(self._model_payloads):payload.close()
            self._closed=True
            for demand in self._demands.values():
                if demand.state=="ready":
                    demand.result.close();demand.result=None
                demand.state,demand.error="cancelled","observation_cancelled"
            self._demands.clear();self._demand_handles.clear()
            self._receipts.clear();self._receipt_handles.clear()
            self._rt.sessions.pop(id(self),None)
            for record in tuple(self._rt.wires.records.values()):
                if record.scope==self._scope:
                    record.retired=True
            for record in tuple(self._rt.fragments.records.values()):
                if record.scope==self._scope:
                    record.retired=True
            for record in tuple(self._rt.assets.records.values()):
                if record.scope==self._scope:
                    record.retired=True
            self._rt.pump()
        return self.drained

    def _publish_observation(self,demand,plan,asset=None,visual="unavailable"):
        if plan.projection_trace is None:
            raise ExportError()
        fragment=self._fragment("context",plan.payload,(plan.projection_trace,),
            expires=demand.deadline)
        observation_id = (demand.prepared.preparation.spec.observation_id if demand.kind == "prepared"
            else "observation-" + uuid4().hex)
        demand.result=Observation(observation_id,fragment,() if asset is None else (asset,),visual)
        record = self._rt.fragments.get(fragment, self._scope)
        record.observation = demand.result
        record.observation_cut = plan.cut
        record.observation_revision = plan._derived_revision
        record.materialized = False
        if demand.kind == "prepared":
            record = self._rt.fragments.get(fragment, self._scope)
            record.prepared = (demand.prepared, demand.result, demand.native)
        demand.state="ready"

    def _begin(self,demand):
        self._rt.preparing+=1
        try:
            return self._begin_impl(demand)
        finally:
            self._rt.preparing-=1
            self._rt._release_if_drained()

    def _begin_impl(self,demand):
        self._auth()
        if len(self._rt.jobs)>=self.limits.max_inflight_jobs:
            return
        if demand.kind=="crop":
            return self._start_crop(demand)
        plan = demand.prepared.plan if demand.kind == "prepared" else self.gate.current(self.request)
        with self.gate.capture(plan) as cut:
            if not self._condition(demand,cut):
                return
            if demand.kind=="inspect":
                with self.index.projection(cut,self.request) as projector:
                    inspected=projector.inspect(demand.selector)
                self.gate.validate(inspected,cut=cut)
                self._publish_observation(demand,inspected,visual="not_requested")
                return
        # Preflight is strictly outside semantic capture guard.
        focus_view=self.coordinator.read()
        try:
            view=focus_view.view.field(self.request.focus,"view")
        finally:
            focus_view.close()
        engines=tuple(e for e in self.capture.surfaces._entries.values() if e.view_ref==view)
        ids=tuple(e.surface_id for e in engines) if demand.surfaces is None else demand.surfaces
        if len(ids)!=1 or any(not any(e.surface_id==v for e in engines) for v in ids):
            raise ExportError()
        engine=next(e for e in engines if e.surface_id==ids[0])
        engine.refresh()
        snapshot=engine.prepare_capture()
        if demand.kind == "prepared":
            demand.native = snapshot
        if demand.kind=="next":
            old=self._rt.assets.read(demand.after_frame,self._scope)
            if (old.stamp.cut==plan.cut and old.stamp.layout_epoch==snapshot.layout_epoch
                    and old.stamp.realization_fingerprint==snapshot.realization_fingerprint):
                return
        moved=batch=output=retained=working=slot=None
        submitted=False
        try:
            slot=self._rt.assets.reserve_slot()
            with self.gate.capture(plan) as cut:
                demand.attempts+=1
                batch=self.capture.capture_in_guard(cut,ids,deadline_ns=demand.deadline)
                frame=batch.frames[0]
                try:
                    decision=_authorize_pixels(self.index,cut,self.request,frame._resource)
                except ExportError:
                    batch.close();batch=None
                    self._publish_observation(demand,plan)
                    return
                if self._closed or demand.state!="pending":
                    raise ExportError()
                moved=_move_raw(frame)
                self._rt.budget.captures+=1
            resource=moved._resource
            width,height=resource.stamp.pixel_size
            bound=_png_bound(width,height)
            output=moved._reserve_output(resource.image.sizeInBytes()+2*bound)
            retained=self._rt.budget.reserve("retained",bound+_proof_bound((plan.projection_trace,*decision.traces))+1024*len(resource.mappings))
            working=self._rt.budget.reserve("working",resource.image.sizeInBytes()+2*bound)
            mailbox,input_mailbox=SimpleQueue(),SimpleQueue()
            input_mailbox.put((resource.image,bound,mailbox))
            token="job-"+uuid4().hex
            job=SimpleNamespace(token=token,broker=self,demand=demand,plan=plan,moved=moved,
                output=output,retained=retained,working=working,mailbox=mailbox,future=None,
                stamp=resource.stamp,mappings=resource.mappings,
                kind="capture",source_record=None,slot=slot,crop=None,traces=(),expires=min(demand.deadline,resource.reservation.expires))
            # Trace objects contain values; preserve identity rather than hashing.
            job.traces=(plan.projection_trace,*decision.traces)
            from .export_policy import _cpu_entry
            job.future=self._rt.executor.submit(_cpu_entry,input_mailbox)
            self._rt.jobs[token]=job
            demand.job,demand.state=job,"working"
            self._rt.budget.encodes+=1
            submitted=True
        finally:
            if batch is not None:
                batch.close()
            if not submitted:
                if slot is not None:
                    slot.close()
                if moved is not None:
                    moved.close()
                if output is not None:
                    output.close()
                if retained is not None:
                    retained.close()
                if working is not None:
                    working.close()

    def _finish_job(self,job):
        """Called only after CPU function returned and dropped native aliases."""
        _owner()
        completion=asset=None
        try:
            if job.future.exception() is not None:
                from .export_policy import _scrub_failure
                _scrub_failure(job.future.exception())
                raise ExportError()
            completion=job.mailbox.get_nowait()
            demand=job.demand
            if (not completion.ok or self._closed or demand.state!="working"
                    or self._rt.budget.now()>=job.expires):
                raise ExportError()
            from contextlib import nullcontext
            guard=self.gate.capture(job.plan) if job.kind=="capture" else self.coordinator.capture("export-crop-complete")
            with guard as cut:
                self._traces(cut,job.traces)
                asset=self._rt.assets.add(completion.encoded,job.stamp,job.mappings,job.traces,
                    self._scope,job.expires,charge=job.retained,crop=job.crop,slot=job.slot)
                job.retained=None
                if job.kind=="capture":
                    self._publish_observation(demand,job.plan,asset,"available")
                else:
                    demand.result,demand.state=asset,"ready"
            asset=None
        except BaseException:
            if asset is not None:
                asset.close()
            if job.demand.state=="working":
                job.demand.state,job.demand.error="failed","observation_unavailable"
        finally:
            # Completion contains only encoded approved bytes. Drop it before
            # releasing its charge on a failure/cancel; Future itself holdsNone.
            completion=asset=None
            job.future=None
            if job.moved is not None:
                job.moved.close();job.moved=None
            if job.output is not None:
                job.output.close();job.output=None
            if job.source_record is not None:
                self._rt.assets.unpin(job.source_record);job.source_record=None
            job.working.close();job.working=None
            if job.retained is not None:
                job.retained.close();job.retained=None
            job.slot.close()
            job.demand.job=None

    def seal(self,fragments,*,assets=()):
        self._auth()
        fragments,assets=tuple(fragments),tuple(assets)
        records=tuple(self._rt.fragments.get(h,self._scope) for h in fragments)
        handles=list(assets)
        traces=[]
        expires=self._expiry()
        for record in records:
            traces.extend(record.traces)
            handles.extend(record.assets)
            expires=min(expires,record.expires)
        unique=[]
        for handle in handles:
            if not any(handle is prior for prior in unique):
                unique.append(handle)
        with self.coordinator.capture("export-seal") as cut:
            self._traces(cut,traces)
            for handle in unique:
                record=self._rt.assets.read(handle,self._scope)
                self._traces(cut,record.traces)
                traces.extend(record.traces)
                expires=min(expires,record.expires)
            return self._rt.wires.seal(self._scope,self.destination.identifier,records,unique,traces,expires)

    def _direct_query_proof(self, cut, read_sets):
        """Retain only relevant typed query closure under a charged node bound."""
        queries, pending = {}, [dep.key for reads in read_sets for dep in reads.queries]
        while pending:
            key = pending.pop()
            if key in queries:
                continue
            if len(queries) >= 4096 or key not in cut.derived_entries:
                raise ExportError("export_quota_exceeded")
            entry = cut.derived_entries[key]
            queries[key] = entry
            pending.extend(dep.key for dep in entry.read_set.queries)
        nodes = 0
        def bound(value, depth=0):
            nonlocal nodes
            nodes += 1
            if nodes > 65536 or depth > 48:
                raise ExportError("export_quota_exceeded")
            if isinstance(value, Mapping):
                return 128 + sum(bound(k, depth+1) + bound(v, depth+1) for k, v in value.items())
            if type(value) in (tuple, list):
                return 128 + sum(bound(v, depth+1) for v in value)
            from agent_native.state import FrozenList
            if type(value) is FrozenList:
                return 128 + sum(bound(v, depth+1) for v in value)
            if is_dataclass(value):
                return 256 + sum(bound(getattr(value, f.name), depth+1) for f in fields(value))
            return 32 + _json_bound(value, projected=True)
        return queries, sum(1024 + bound(entry.value) + 256 * len(entry.read_set.events)
            for entry in queries.values())

    def seal_direct(self, sealed, observation, profile, *, fragments=(), assets=(), memory=None):
        """Seal the exact approved adapter packet on the original prepared cut.

        Only the HTTP profile exposes its complete final network request body;
        Codex packet accounting covers visible application input.
        """
        from agent_native.runner.direct import installed_profile, schema_digest
        self._auth()
        if type(observation) is not Observation or not installed_profile(profile):
            raise ExportError("prepared_observation_mismatch")
        context = self._rt.fragments.get(observation.context, self._scope)
        issued = getattr(context, "prepared", None)
        if issued is None or issued[0] is not sealed or issued[1] is not observation:
            raise ExportError("prepared_observation_mismatch")
        records = (context, *(self._rt.fragments.get(h, self._scope) for h in tuple(fragments)))
        if any(record.kind == "context" for record in records[1:]):
            raise ExportError("prepared_observation_mismatch")
        handles = list(observation.assets) + list(assets)
        traces, expires = [], self._expiry()
        for record in records:
            traces.extend(record.traces); handles.extend(record.assets)
            expires = min(expires, record.expires)
        unique = []
        for handle in handles:
            if not any(handle is prior for prior in unique):
                unique.append(handle)
        capability_handle = None
        history_handles=[];history_working=None;skills_binding=None;skills_working=None
        try:
            with self.coordinator.capture("export-direct-seal") as cut:
                values = self._prepared_request(sealed, cut)
                self.gate.validate(sealed.plan, cut=cut)
                from agent_native.runner.program_operations import program_capability_text
                capability_text = program_capability_text()
                if any(row.kind == "sdk_literal" and row.value == capability_text for row in records):
                    raise ExportError("reserved_sdk_capability_literal")
                offered = self._program_offer(sealed)
                if offered:
                    capability_handle = self._fragment("sdk_literal", capability_text)
                    records += (self._rt.fragments.get(capability_handle, self._scope),)
                from .skills import prepare as prepare_skills
                from .skills import bound_runtime
                if bound_runtime(self.kernel,self.request.actor) is not None:
                    skills_working=self._rt.budget.reserve("working",16*1024*1024)
                skills_binding=prepare_skills(self,cut,sealed)
                if skills_binding is not None:
                    skill_handle=self._fragment("skills",skills_binding.content)
                    history_handles.append(skill_handle)
                    records += (self._rt.fragments.get(skill_handle,self._scope),)
                history=None
                if memory is not None:
                    from .history import history_literal
                    from .lineage import MAX_LINEAGE_BYTES
                    history_working=self._rt.budget.reserve("working",8*MAX_LINEAGE_BYTES)
                    history=self._history_binding(cut,sealed,memory)
                    from agent_native.runner.memory.visible import VisibleMemoryPolicy
                    if memory.policy.config_digest!=VisibleMemoryPolicy.for_installed_profile(profile).config_digest:
                        raise ExportError("history_changed")
                    for kind,text in (("history",memory.preparation.serialized_history.decode("utf-8")),
                                      ("sdk_literal",history_literal())):
                        handle=self._fragment(kind,text);history_handles.append(handle)
                        records += (self._rt.fragments.get(handle,self._scope),)
                if (profile.provider_id != self.destination.provider or profile.destination_id != self.destination.identifier
                        or profile.account_id != self.destination.account or profile.config_digest != self.destination.configuration
                        or values["transport_version"] != profile.transport_version
                        or values["schema_digest"] != schema_digest(thaw(sealed.plan.payload["schemas"]))):
                    raise ExportError("provider_identity_mismatch")
                self._traces(cut, traces)
                for handle in unique:
                    record = self._rt.assets.read(handle, self._scope)
                    self._traces(cut, record.traces)
                    traces.extend(record.traces); expires = min(expires, record.expires)
                session_values = cut.view.resolve(values["session"]).values
                conversation = session_values["conversation"]
                before_records = {ref.key: cut.view.resolve(ref)
                    for ref in (sealed.request, values["session"], conversation)}
                before = {key: row.values for key, row in before_records.items()}
                from agent_native.persistence.codec import encode_value
                before_record_sizes = {key: len(encode_value(row)) for key, row in before_records.items()}
                queries, query_proof_size = self._direct_query_proof(cut,
                    (sealed.plan.read_set, sealed.plan.policy.read_set, *(trace.read_set for trace in traces
                        if trace.original_cut == sealed.plan.cut)))
                prepared = {"sealed": sealed, "native": issued[2], "observation": observation,
                    "before_records": before, "before_record_sizes": before_record_sizes,
                    "queries": queries, "sdk_program_offered": offered, "history": history, "skills": skills_binding,
                    "registry_schema_digest":values["schema_digest"],
                    "policy_revision": cut.view.policy_revision, "index_revision": cut.view.index_revision,
                    "fresh_asset_tokens": tuple(a._token for a in observation.assets),
                    "proof_size": 4096 + sum(_json_bound(v, projected=True) for v in before.values())
                        + query_proof_size}
                if skills_binding is not None:
                    from agent_native.kernel import _command_values as v
                    prepared["proof_size"] += len(encode_value((skills_binding.content,skills_binding.claims,
                        skills_binding.pins,v.reads_data(skills_binding.read_set))))+65536
                if history is not None:
                    from agent_native.kernel import _command_values as v
                    prepared["proof_size"] += len(encode_value((history.claims,history.selected_events,history.summary,
                        v.reads_data(history.read_set))))+65536
                return self._rt.wires.seal_direct(self._scope, self.destination.identifier, records,
                    unique, traces, min(expires, values["deadline_ns"]), profile=profile,
                    observation_id=observation.observation_id,
                    output_reservation=values["reservation"]["output_tokens"], prepared=prepared)

        finally:
            if capability_handle is not None:
                capability_handle.close()
            for handle in history_handles:handle.close()
            if history_working is not None:history_working.close()
            if skills_working is not None:skills_working.close()

    def seal_summarize(self,sealed,profile,memory):
        """Separate actual input: fixed SDK instruction + parent/consumed groups.

        Genuine private PlanConsumption still guards admission. Its UI state,
        latest user intent and pixels are neither serialized nor claimed inputs.
        """
        from .history import summary_literal
        from .lineage import MAX_LINEAGE_BYTES
        from agent_native.runner.memory.visible import VisibleMemoryPolicy
        from agent_native.runner.direct import installed_profile
        from agent_native.persistence.codec import encode_value
        from agent_native.kernel import _command_values as v
        self._auth()
        if sealed.preparation.spec.purpose!="summarize" or not installed_profile(profile):
            raise ExportError("history_purpose_mismatch")
        handles=[];working=self._rt.budget.reserve("working",8*MAX_LINEAGE_BYTES)
        try:
            with self.coordinator.capture("export-summarize-seal") as cut:
                values=self._prepared_request(sealed,cut);self.gate.validate(sealed.plan,cut=cut)
                history=self._history_binding(cut,sealed,memory)
                if (memory.policy.config_digest!=VisibleMemoryPolicy.for_installed_profile(profile).config_digest
                        or profile.config_digest!=values["config_digest"]
                        or profile.transport_version!=values["transport_version"]):
                    raise ExportError("provider_identity_mismatch")
                for kind,text in (("sdk_literal",summary_literal()),
                    ("summarize",memory.preparation.serialized_history.decode("utf-8"))):
                    handles.append(self._fragment(kind,text))
                records=tuple(self._rt.fragments.get(h,self._scope) for h in handles)
                session=values["session"];conversation=cut.view.field(session,"conversation")
                before_records={r.key:cut.view.resolve(r) for r in (sealed.request,session,conversation)}
                before={key:row.values for key,row in before_records.items()}
                queries,query_size=self._direct_query_proof(cut,(sealed.plan.read_set,sealed.plan.policy.read_set))
                prepared={"sealed":sealed,"native":None,"observation":None,
                    "before_records":before,"before_record_sizes":{key:len(encode_value(row)) for key,row in before_records.items()},
                    "queries":queries,"sdk_program_offered":False,"history":history,
                    "registry_schema_digest":values["schema_digest"],"policy_revision":cut.view.policy_revision,
                    "index_revision":cut.view.index_revision,"fresh_asset_tokens":(),
                    "proof_size":4096+sum(_json_bound(x,projected=True) for x in before.values())+query_size
                        +len(encode_value((history.claims,history.selected_events,history.summary,v.reads_data(history.read_set))))+65536}
                result=self._rt.wires.seal_direct(self._scope,self.destination.identifier,records,(),(),
                    min(self._expiry(),values["deadline_ns"]),profile=profile,
                    observation_id=values["observation_id"],output_reservation=values["reservation"]["output_tokens"],prepared=prepared)
                if memory.summary_preflight!=(memory.preparation.candidate_digest,self._rt.wires.get(result,self._scope).metadata.fingerprint,
                        self._rt.wires.get(result,self._scope).metadata.byte_count,values["reservation"]["output_tokens"]):
                    result.close();raise ExportError("history_changed")
                return result
        finally:
            for handle in handles:handle.close()
            working.close()

    def validate_direct(self, envelope, sealed):
        """Pre-arm exact-source validation; no bytes leave the owner."""
        self._auth()
        record = self._rt.wires.get(envelope, self._scope)
        if record.prepared is None or record.prepared["sealed"] is not sealed or record.consumed:
            raise ExportError("prepared_observation_mismatch")
        with self.coordinator.capture("export-direct-validate") as cut:
            self._prepared_request(sealed, cut)
            self.gate.validate(sealed.plan, cut=cut)
            self._check_program_offer(record, sealed)
            self._recheck_history(cut,record,sealed)
            self._recheck_skills(cut,record,sealed)
            self._traces(cut, record.traces)
        return record.metadata

    def build_input_lineage(self, envelope, sealed):
        """Actual consumed candidate sources, before SAME atomic C1 arm.

        The durable draft is DATA. This method alone grants neither SEND nor
        historical export. Actual trace checks and original own-arm witness
        remain mandatory. C4 retains its charge until the actual wire cleanup.
        """
        from .lineage import collect_input_lineage, MAX_LINEAGE_BYTES
        from agent_native.persistence.codec import encode_value
        self._auth()
        record = self._rt.wires.get(envelope, self._scope)
        if record.prepared is None or record.prepared["sealed"] is not sealed or record.consumed:
            raise ExportError("prepared_observation_mismatch")
        with self.coordinator.capture("export-input-lineage") as cut:
            self._prepared_request(sealed, cut)
            self.gate.validate(sealed.plan, cut=cut)
            self._check_program_offer(record, sealed)
            self._recheck_history(cut,record,sealed)
            self._recheck_skills(cut,record,sealed)
            self._traces(cut, record.traces)
            cached = record.prepared.get("input_lineage")
            if cached is not None:
                working = self._rt.budget.reserve("working", 8 * MAX_LINEAGE_BYTES)
                try:
                    return thaw(cached)
                finally:
                    working.close()
            retained = self._rt.budget.reserve("retained", MAX_LINEAGE_BYTES)
            working = None
            try:
                working = self._rt.budget.reserve("working",
                    8 * MAX_LINEAGE_BYTES + 4 * _proof_bound(record.traces))
                draft = collect_input_lineage(self, record, sealed, cut)
                encoded = encode_value(draft)
                retained.resize(len(encoded))
                record.prepared["input_lineage"] = deep_freeze(draft)
                record.lineage_charge = retained
                return draft
            except BaseException:
                retained.close()
                raise
            finally:
                if working is not None:
                    working.close()

    def _arm_reads_match(self, reads, cut, changes, queries, seen=None, *, own_index=None):
        """Discount ONLY witnessed own changed control fields/records.

        All other SYS/domain versions, policy metadata and typed query cache
        incarnations/dependencies retain exact comparisons. No global SYS mask.
        """
        if (not reads.complete or reads.epoch != cut.identity.epoch
                or reads.schema_manifest != cut.identity.schema_manifest
                or reads.policy_revision is not None and reads.policy_revision != cut.view.policy_revision
                or reads.index_revision is not None and reads.index_revision not in (cut.view.index_revision, own_index)
                or reads.sequence is not None):
            return False
        for event in reads.events:
            if cut.view.version(event) == event.version:
                continue
            own = changes.get(event.key)
            if own is None or not (event.kind == "record" or event.kind == "field" and event.field in own):
                return False
        seen = set() if seen is None else seen
        for dep in reads.queries:
            old, current = queries.get(dep.key), cut.derived_entries.get(dep.key)
            if old is None or current is None or old.version != dep.generation or current.status != "current":
                return False
            if current.version == dep.generation:
                continue
            if (current.version.cache_incarnation != dep.generation.cache_incarnation
                    or not strict_equal(old.value, current.value) or dep.key in seen):
                return False
            seen.add(dep.key)
            if not self._arm_reads_match(old.read_set, cut, changes, queries, seen, own_index=own_index):
                return False
            seen.remove(dep.key)
        return True

    def _direct_arm_check(self, record, sealed, witness, cut):
        data = record.prepared
        if data is not None:
            self._check_program_offer(record, sealed)
            self._recheck_history(cut,record,sealed)
            self._recheck_skills(cut,record,sealed)
        if (data is None or data["sealed"] is not sealed or witness.request != sealed.request
                or witness.before_cut != sealed.plan.cut or witness.after_cut != cut.identity
                or cut.identity.epoch != sealed.plan.cut.epoch
                or cut.identity.schema_manifest != sealed.plan.cut.schema_manifest
                or cut.identity.sequence != sealed.plan.cut.sequence + 1
                or witness.terminal.identity != cut.identity
                or type(witness.patch) is not Patch or witness.patch.deletes
                or cut.view.policy_revision != data["policy_revision"]
                # Coordinator advances the derived index revision on EVERY
                # publication. Accept precisely the witnessed own successor,
                # never an arbitrary revision gap or a stale query incarnation.
                or cut.view.index_revision != data["index_revision"] + 1):
            raise ExportError("observation_sources_changed")
        before = data["before_records"]
        request_key = sealed.request.key
        session_key = before[request_key]["session"].key
        conversation_key = before[session_key]["conversation"].key
        allowed = {request_key: {"state", "accounting", "charged", "wire_digest", "wire_bytes", "frame_count", "reason", "input_lineage"},
            session_key: {"phase", "reserved", "charged", "reason"},
            conversation_key: {"used", "reserved"}}
        # SYS6 arm adds ONLY its own immutable lineage and terminal reservation.
        # Check exact new rows, not a blanket allowance for private SYS writes.
        from agent_native.kernel._input_lineage import native_digest
        from agent_native.persistence.codec import encode_value
        from agent_native.kernel._capacity import RESPONSE_RESERVE, RESPONSE_EVENT_RESERVE
        from .lineage import resource
        d = self.kernel.system
        lineage_ref = self.kernel._ref(d.RequestInputLineage, sealed.request.key.entity_id)
        reservation_ref = self.kernel._ref(d.RuntimeStorageReservation,
            native_digest(("model-response", sealed.request)))
        draft = data.get("input_lineage")
        if draft is None:
            raise ExportError("observation_lineage_missing")
        session_before = before[session_key]
        expected_lineage = dict(thaw(draft), request=resource(sealed.request),
            conversation=Ref(conversation_key), original_task=session_before["task"],
            task_generation=session_before["task_generation"], principal=session_before["principal"])
        expected_lineage["digest"] = native_digest(expected_lineage)
        expected_reservation = {"principal": session_before["principal"],
            "owner": resource(sealed.request), "bytes": RESPONSE_RESERVE, "state": "reserved"}
        creates = {}
        for created in witness.patch.creates:
            key = (created.entity_type, created.entity_id)
            if key in creates or created.partition != "durable":
                raise ExportError("observation_sources_changed")
            creates[key] = created.values
        expected_creates = {(lineage_ref.key.entity_type, lineage_ref.key.entity_id): expected_lineage,
            (reservation_ref.key.entity_type, reservation_ref.key.entity_id): expected_reservation}
        if set(creates) != set(expected_creates) or any(not strict_equal(
                deep_freeze(creates[key]), deep_freeze(expected_creates[key])) for key in creates):
            raise ExportError("observation_sources_changed")
        for ref, values in ((lineage_ref, expected_lineage), (reservation_ref, expected_reservation)):
            actual = cut.view.resolve(ref)
            if actual.partition != "durable" or actual.version != cut.identity.sequence or not strict_equal(actual.values, deep_freeze(values)):
                raise ExportError("observation_sources_changed")
        changes = {}
        for update in witness.patch.updates:
            if update.ref.key not in allowed or update.ref.key in changes or not set(update.changes) <= allowed[update.ref.key]:
                raise ExportError("observation_sources_changed")
            changes[update.ref.key] = update.changes
        if set(changes) != set(allowed):
            raise ExportError("observation_sources_changed")
        for key, old in before.items():
            expected = thaw(old); expected.update(thaw(deep_freeze(changes[key])))
            if not strict_equal(deep_freeze(expected), cut.view.resolve(Ref(key)).values):
                raise ExportError("observation_sources_changed")
        conversation_before = before[conversation_key]
        conversation_after = cut.view.resolve(Ref(conversation_key)).values
        expected_used = thaw(conversation_before["used"])
        expected_used["bytes"] += (len(encode_value(cut.view.resolve(lineage_ref)))
            + len(encode_value(cut.view.resolve(Ref(session_key)))) - data["before_record_sizes"][session_key])
        expected_reserved = thaw(conversation_before["reserved"])
        expected_reserved["bytes"] += RESPONSE_RESERVE
        expected_reserved["events"] += RESPONSE_EVENT_RESERVE
        if (not strict_equal(conversation_after["used"], deep_freeze(expected_used))
                or not strict_equal(conversation_after["reserved"], deep_freeze(expected_reserved))):
            raise ExportError("observation_sources_changed")
        current = cut.view.resolve(sealed.request).values
        if (current["input_lineage"] != lineage_ref or current["state"] != "armed" or current["accounting"] != "armed"
                or current["wire_digest"] != record.metadata.fingerprint
                or current["wire_bytes"] != record.metadata.byte_count
                or current["frame_count"] != record.metadata.image_count):
            raise ExportError("observation_sources_changed")
        # Historical reauthorization is NOT freshness. Enforce retained source
        # dependencies as well as unchanged actually exported projection.
        for reads in (sealed.plan.read_set, sealed.plan.policy.read_set,
                      *(trace.read_set for trace in record.traces if trace.original_cut == sealed.plan.cut)):
            if not self._arm_reads_match(reads, cut, changes, data["queries"], own_index=data["index_revision"]):
                raise ExportError("observation_sources_changed")
        fresh = self.index.plan(cut, self.request)
        if not strict_equal(fresh.payload, sealed.plan.payload):
            raise ExportError("observation_sources_changed")
        for trace in record.traces:
            if trace.original_cut != sealed.plan.cut:
                continue
            for sample in trace.samples:
                own = changes.get(getattr(sample.source, "key", None), {})
                if sample.name in own:
                    # Even an own control delta cannot change exported content.
                    raise ExportError("observation_sources_changed")
        native = data["native"]
        if sealed.preparation.spec.purpose=="summarize":
            if native is not None or record.assets or record.traces or data.get("history") is None:
                raise ExportError("history_purpose_mismatch")
            return
        if native is None or not record.assets:
            raise ExportError("observation_unavailable")
        engine = self.capture.surfaces._entries.get(native.surface_id)
        if engine is None:
            raise ExportError("observation_sources_changed")
        from agent_native.ui.engine import UiUnavailable
        try:
            actual = engine.realization_snapshot(cut)
        except UiUnavailable:
            raise ExportError("observation_sources_changed") from None
        for field in ("view", "surface_id", "surface_generation", "layout_epoch", "mappings", "presentation",
                      "realization_fingerprint", "surface_kind", "omitted_categories"):
            if not strict_equal(getattr(native, field), getattr(actual, field)):
                raise ExportError("observation_sources_changed")
        # Original stamp/PNG remain N. Compare geometry/DPR from live mappings
        # and root, without stamping the historical frame with N+1.
        for asset in record.assets:
            if asset is record.assets[0]:
                stamp = asset.stamp
                root = engine.capture_root
                if (stamp.view != actual.view or stamp.surface_id != actual.surface_id
                        or stamp.surface_generation != actual.surface_generation
                        or stamp.layout_epoch != actual.layout_epoch
                        or stamp.realization_fingerprint != actual.realization_fingerprint
                        or stamp.dpr != root.devicePixelRatioF()
                        or stamp.logical_size != (root.width(), root.height())
                        or stamp.pixel_size != (ceil(root.width()*stamp.dpr), ceil(root.height()*stamp.dpr))):
                    raise ExportError("observation_sources_changed")

    def consume_direct(self, envelope, sealed, arm_witness):
        """Final one-shot live handoff; transfer pin survives expiry/close."""
        self._auth()
        record = self._rt.wires.get(envelope, self._scope)
        if record.consumed:
            raise ExportError("envelope_already_consumed")
        # Resolve actual durable terminal BEFORE taking the capture guard.
        terminal = self.coordinator.terminal_cut(arm_witness.terminal)
        terminal.close()
        with self.coordinator.capture("export-direct-consume") as cut:
            self._prepared_request(sealed, cut)
            self._traces(cut, record.traces)
            self._direct_arm_check(record, sealed, arm_witness, cut)
            for asset in record.assets:
                if asset.retired or self._rt.budget.now() >= asset.expires:
                    raise ExportError("asset_unavailable")
            import hashlib
            if hashlib.sha256(record.wire).hexdigest() != record.metadata.fingerprint:
                raise ExportError()
            return self._rt.wires.transfer(record)

    def consume_wire(self,envelope):
        """One final live TEST transport handoff; no actual network performed."""
        self._auth()
        record=self._rt.wires.get(envelope,self._scope)
        if record.metadata.transport != "self_contained_test_json/1":
            raise ExportError("test_transport_only")
        if record.consumed:
            raise ExportError("envelope_already_consumed")
        with self.coordinator.capture("export-wire-consume") as cut:
            self._traces(cut,record.traces)
            for asset in record.assets:
                if asset.retired or self._rt.budget.now()>=asset.expires:
                    raise ExportError()
            import hashlib
            if hashlib.sha256(record.wire).hexdigest()!=record.metadata.fingerprint:
                raise ExportError()
            record.consumed=True
            return record.wire

    def model_output(self,envelope,text,*,kind="model_quote"):
        """Trusted C5 completion seam: union of ACTUALLY consumed input claims."""
        self._auth()
        record=self._rt.wires.get(envelope,self._scope)
        if not record.consumed or type(text)is not str or kind not in ("model_quote","summary"):
            raise ExportError()
        with self.coordinator.capture("export-model-output") as cut:
            self._traces(cut,record.traces)
            return self._fragment(kind,text,record.traces,expires=record.expires)

    def historical(self,asset):
        self._auth()
        if type(asset)is str:
            self._rt.assets.sweep()
            record=next((r for r in self._rt.assets.records.values()
                if r.scope==self._scope and r.stamp.frame_id==asset and r.crop is None and not r.retired),None)
            if record is None:
                raise ExportError("asset_unavailable")
            asset=self._rt.assets.reissue(record)
        record=self._rt.assets.read(asset,self._scope)
        with self.coordinator.capture("export-history") as cut:
            self._traces(cut,record.traces)
            return asset

    def retarget(self,source,handle):
        """Explicit trusted destination-only reprojection; retain information/TTL."""
        self._auth()
        if type(source)is not ExportBroker:
            raise ExportError()
        source._auth()
        if (source.kernel is not self.kernel
                or source.index is not self.index or source._rt is not self._rt
                or self._scope[:4]!=source._scope[:4]):
            raise ExportError()
        with self.coordinator.capture("export-retarget") as cut:
            if type(handle)is FragmentHandle:
                record=source._rt.fragments.get(handle,source._scope)
                self._traces(cut,record.traces,retarget=True)
                new_assets=tuple(self._retarget_asset(source,a,cut) for a in record.assets)
                return self._fragment(record.kind,record.value,record.traces,new_assets,expires=record.expires)
            if type(handle)is AssetHandle:
                return self._retarget_asset(source,handle,cut)
            raise ExportError()

    def _retarget_asset(self,source,handle,cut):
        record=source._rt.assets.read(handle,source._scope)
        self._traces(cut,record.traces,retarget=True)
        # Exact retained encoded variant, original stamp/mapping and TTL ceiling.
        # Never expose richer decision.value or recover omitted pixels from C1.
        return self._rt.assets.add(record.encoded,record.stamp,record.mappings,record.traces,
            self._scope,min(record.expires,self._expiry()),crop=record.crop)

    def crop(self,asset,visual_id,*,deadline_ns=None):
        self._auth()
        if type(visual_id)is not str:
            raise ExportError()
        with self.coordinator.capture("export-crop-admit") as cut:
            record=self._rt.assets.read(asset,self._scope)
            self._traces(cut,record.traces)
            if not any(v.visual_id==visual_id for v in record.metadata.visual_instances):
                raise ExportError()
        handle=self._new_demand("crop",deadline_ns=deadline_ns)
        demand=self._demands[handle._token]
        demand.crop_source,demand.visual_id=asset,visual_id
        return handle

    def _start_crop(self,demand):
        record=retained=working=slot=None
        submitted=False
        try:
            slot=self._rt.assets.reserve_slot()
            with self.coordinator.capture("export-crop-start") as cut:
                source=self._rt.assets.read(demand.crop_source,self._scope)
                self._traces(cut,source.traces)
                visual=next((v for v in source.metadata.visual_instances if v.visual_id==demand.visual_id),None)
                if visual is None:
                    raise ExportError()
                region=visual.bounds
                bound=_png_bound(region[2],region[3])
                retained=self._rt.budget.reserve("retained",bound+_proof_bound(source.traces)+1024*len(source.mappings))
                working=self._rt.budget.reserve("working",
                    source.metadata.width*source.metadata.height*4+region[2]*region[3]*8+2*bound)
                record=self._rt.assets.pin(demand.crop_source,self._scope)
            origin=(0,0) if record.crop is None else record.crop[:2]
            crop=(origin[0]+region[0],origin[1]+region[1],region[2],region[3])
            mailbox,input_mailbox=SimpleQueue(),SimpleQueue()
            input_mailbox.put((record.encoded,region,(record.metadata.width,record.metadata.height),bound,mailbox))
            token="job-"+uuid4().hex
            job=SimpleNamespace(token=token,broker=self,demand=demand,plan=None,moved=None,output=None,
                retained=retained,working=working,mailbox=mailbox,future=None,
                stamp=record.stamp,mappings=record.mappings,traces=record.traces,
                kind="crop",source_record=record,slot=slot,crop=crop,expires=min(demand.deadline,record.expires))
            from .export_policy import _crop_entry
            job.future=self._rt.executor.submit(_crop_entry,input_mailbox)
            self._rt.jobs[token]=job
            demand.job,demand.state=job,"working"
            self._rt.budget.encodes+=1
            submitted=True
        finally:
            if not submitted:
                if slot is not None:
                    slot.close()
                if record is not None:
                    self._rt.assets.unpin(record)
                if retained is not None:
                    retained.close()
                if working is not None:
                    working.close()


# One implementation/authority: the channel is the destination-scoped broker.
ObservationChannel = ExportBroker
