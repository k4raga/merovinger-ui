"""Frozen original-source DATA for the existing checked C2 preparation.

The producer reads only fixed SDK original records on a private current cut. The
handler keeps its old domain basis; no current reader/issuer/handle is returned.
This is not a bearer capability or an application context/source callback.
"""
from dataclasses import dataclass
from typing import Literal
from agent_native.schema import Ref
from agent_native.state import FrozenMap,deep_freeze,thaw,strict_equal
from agent_native.state.tracking import ReadTracker
from ._input_lineage import native_bytes,native_digest,resource_ref

SOURCE_MAX_NATIVE_BYTES=2*1024*1024

@dataclass(frozen=True,slots=True)
class PrepareSourceFacts:
    kind: Literal['agent_response','skill_control']
    source: Ref
    request: Ref
    input_lineage: Ref | None
    lineage_data: FrozenMap | None
    input_fingerprint: str
    response_fingerprint: str | None
    owner: str
    source_digest: str

    @property
    def claims(self):return () if self.lineage_data is None else self.lineage_data['claims']
    @property
    def original_cut(self):return None if self.lineage_data is None else self.lineage_data['original_cut']
    @property
    def selected_events(self):return () if self.lineage_data is None else self.lineage_data['selected_events']
    @property
    def summary(self):return None if self.lineage_data is None else self.lineage_data['summary']
    @property
    def skill_pins(self):return () if self.lineage_data is None else self.lineage_data['skill_pins']

    def as_data(self):
        """Detached native DATA for the SDK companion, never reconstructed authority."""
        return {name:thaw(getattr(self,name)) for name in self.__dataclass_fields__}

@dataclass(frozen=True,slots=True)
class PrepareOriginFacts:
    principal: str
    task: Ref
    request: Ref
    grant: Ref
    basis_kind: str
    source_facts: PrepareSourceFacts | None=None

    def as_data(self):
        return {'principal':self.principal,'task':self.task,'request':self.request,
            'grant':self.grant,'basis_kind':self.basis_kind,
            'source_facts':None if self.source_facts is None else self.source_facts.as_data()}


def _fail(code):
    from .commands import AdmissionError
    raise AdmissionError(code)


def _bounded_source(data):
    """Count the complete native projection, never truncate a source union."""
    item=dict(data);item['source_digest']=native_digest(data)
    if len(native_bytes(item))>SOURCE_MAX_NATIVE_BYTES:_fail('prepare_source_limit')
    item['lineage_data']=None if item['lineage_data'] is None else deep_freeze(item['lineage_data'])
    return PrepareSourceFacts(**item)


def _agent_source(kernel,view,delivery,basis,admission):
    if not kernel.system.supports_agent_runtime:return None
    d=kernel.system;request=kernel._ref(d.ModelRequest,delivery.request_id)
    if not view.exists(request):return None
    # Read immutable/proof-once fields individually; ordinary approval changes
    # to RequestAdmission.state must not stale an otherwise identical source.
    session=view.field(request,'session')
    lineage=view.field(request,'input_lineage')
    input_fp=view.field(request,'wire_digest');response_fp=view.field(request,'response_fingerprint')
    if (view.field(request,'admission')!=admission or view.field(request,'purpose')!='act'
            or view.field(request,'task_generation')!=basis.task_generation
            or view.field(session,'task')!=delivery.task or view.field(session,'principal')!=delivery.principal
            or view.field(session,'task_generation')!=basis.task_generation
            or type(lineage) is not Ref or input_fp is None or response_fp is None):
        _fail('prepare_source_unavailable')
    response=kernel._ref(d.AcceptedModelResponse,delivery.request_id)
    if (not view.exists(response) or view.field(response,'request')!=request
            or view.field(response,'fingerprint')!=response_fp or view.field(response,'accepted_as')!='active'
            or view.field(response,'task_generation')!=basis.task_generation):
        _fail('prepare_source_unavailable')
    original=thaw(view.resolve(lineage).values)
    content=dict(original);digest=content.pop('digest')
    if (native_digest(content)!=digest or resource_ref(original['request'])!=request
            or original['original_task']!=delivery.task or original['task_generation']!=basis.task_generation
            or original['principal']!=delivery.principal
            or original['conversation']!=view.field(session,'conversation')):
        _fail('prepare_source_mismatch')
    return _bounded_source({'kind':'agent_response','source':response,'request':request,
        'input_lineage':lineage,'lineage_data':original,'input_fingerprint':input_fp,
        'response_fingerprint':response_fp,'owner':delivery.principal})


def _control_source(kernel,view,delivery,admission,command):
    # Only the original mutation catalogue is eligible. Other UI commands retain
    # source=None and acquire no unrelated skill-delivery membership dependency.
    if not kernel.system.supports_agent_runtime:return None
    from agent_native.skills import operation_specs
    operation=next((s.operation_id for s in operation_specs() if s.mutates and 'engine.'+s.operation_id==command),None)
    if operation is None:return None
    matches=[]
    for ref in view.entities(kernel.system.SkillControlDelivery.__entity_type__):
        if view.field(ref,'admission')==admission:matches.append(ref)
    if not matches:return None
    if len(matches)!=1:_fail('prepare_source_ambiguous')
    source=matches[0];original=thaw(view.resolve(source).values)
    key=tuple(original[n] for n in ('project_id','storage_id','principal','operation_id','request_id'))
    if (key[:2]!=kernel._runtime_scope or original['principal']!=delivery.principal
            or original['task']!=delivery.task or original['operation_id']!=operation
            or source.key.entity_id!=native_digest(key)
            or admission.key.entity_id!=native_digest(('skill-control',key))):
        _fail('prepare_source_mismatch')
    # Imported CLI content is owner-scoped by its real user-control source; empty
    # model claims are not a public/builtin classification selected by JSON.
    return _bounded_source({'kind':'skill_control','source':source,'request':admission,
        'input_lineage':None,'lineage_data':None,'input_fingerprint':original['input_digest'],
        'response_fingerprint':None,'owner':original['principal']})


def capture_prepare_origin(kernel,reader,delivery,basis,command):
    """Private SDK producer, called only AFTER normal delivery/grant admission.

    The resulting ReadSet is separate from old domain reads: combining current
    SDK records with old domain versions could either conflict or hide a rebase.
    No ambient context, application callback, live reader or issuer is retained.
    """
    admission=kernel._request_ref(delivery.request_id)
    with ReadTracker(reader) as tracked:
        view=tracked.view
        principal=view.field(delivery.task,'principal')
        if (principal!=delivery.principal or view.field(delivery.task,'generation')!=basis.task_generation
                or view.field(admission,'task')!=delivery.task
                or view.field(admission,'task_generation')!=basis.task_generation
                or view.field(admission,'basis_kind')!=basis.kind
                or view.field(admission,'basis_handle')!=basis.handle
                or delivery.call_slot not in view.field(admission,'call_slots')
                or view.field(delivery.grant,'principal')!=principal):
            _fail('prepare_origin_mismatch')
        source=(_agent_source(kernel,view,delivery,basis,admission) if basis.kind=='agent'
            else _control_source(kernel,view,delivery,admission,command) if basis.kind=='ui' else None)
        result=PrepareOriginFacts(principal,delivery.task,admission,delivery.grant,basis.kind,source)
    if not tracked.read_set.complete:_fail('prepare_source_incomplete')
    return result,tracked.read_set


def check_prepare_origin(kernel,data,reader):
    """Recheck exact original DATA/dependencies without running the app handler."""
    from . import _command_values as v
    reads=v.read_set(data['source_reads'])
    if not reads.complete or not reads.matches(reader):_fail('prepare_source_changed')
    delivery=kernel._delivery_data(data);basis=kernel._basis(data['proposal']['basis'],delivery.principal)
    current,_=capture_prepare_origin(kernel,reader,delivery,basis,data['proposal']['command'])
    if not strict_equal(current.as_data(),data['prepare_origin']):_fail('prepare_source_changed')
