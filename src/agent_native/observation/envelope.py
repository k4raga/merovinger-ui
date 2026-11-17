"""Engine-issued fragments and sealed whole TEST and direct-provider envelopes.

TEST serialization remains a separate reference path. Production serialization
uses the fixed direct provider profile and retains its complete final HTTP body. Model output
inherits the union of its actually consumed input fragments, never a caller's
self-declared public label. No public arbitrary-JSON/text seal is provided.
"""
from dataclasses import dataclass
from collections.abc import Mapping
from uuid import uuid4
from weakref import WeakValueDictionary
import base64
import hashlib
import json

from agent_native.state import deep_freeze, thaw
from ._export_types import ExportError, _owner, _json_bound, _proof_bound


_FRAGMENT_KEY, _WIRE_KEY = object(), object()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class FragmentHandle:
    _store: object
    _token: str
    def __init__(self, store, token, *, _key=None):
        if _key is not _FRAGMENT_KEY:
            raise TypeError("Use ObservationChannel")
        object.__setattr__(self,"_store",store)
        object.__setattr__(self,"_token",token)

    def close(self):
        self._store.close_handle(self)


class _Fragment:
    def __init__(self, token, kind, value, traces, asset_handles, scope, expires, charge):
        self.token, self.kind, self.value = token, kind, value
        self.traces, self.assets = tuple(traces), tuple(asset_handles)
        self.scope, self.expires, self.charge = scope, expires, charge
        self.retired = False


class _FragmentStore:
    def __init__(self, budget):
        self.budget = budget
        self.records, self.handles = {}, WeakValueDictionary()
        self.closed = False

    def sweep(self):
        _owner()
        now = self.budget.now()
        for token, record in tuple(self.records.items()):
            if self.closed or record.retired or now >= record.expires:
                self.records.pop(token,None)
                record.value = None
                record.traces=record.assets=()
                for ident,handle in tuple(self.handles.items()):
                    if handle._token==record.token:
                        self.handles.pop(ident,None)
                record.charge.close()

    def add(self, kind, value, traces, assets, scope, expires):
        _owner()
        self.sweep()
        if self.closed or type(kind) is not str or len(self.records) >= self.budget.limits.max_fragments:
            raise ExportError("export_quota_exceeded")
        if expires <= self.budget.now():
            raise ExportError("fragment_unavailable")
        bound = _json_bound(value,projected=True)+_proof_bound(traces)
        if bound > self.budget.limits.max_fragment_bytes:
            raise ExportError("export_quota_exceeded")
        charge = self.budget.reserve("retained",bound)
        try:
            token = "fragment-"+uuid4().hex
            from agent_native.context.policy import public_value
            record = _Fragment(token,kind,deep_freeze(public_value(value)),traces,assets,scope,expires,charge)
            handle = FragmentHandle(self,token,_key=_FRAGMENT_KEY)
            self.handles[id(handle)] = handle
            self.records[token] = record
            return handle
        except BaseException:
            charge.close()
            raise

    def get(self, handle, scope):
        _owner()
        self.sweep()
        if (type(handle) is not FragmentHandle or handle._store is not self
                or self.handles.get(id(handle)) is not handle):
            raise ExportError("fragment_unavailable")
        record = self.records.get(handle._token)
        if record is None or record.scope != scope:
            raise ExportError("fragment_unavailable")
        return record

    def close_handle(self, handle):
        _owner()
        if type(handle)is FragmentHandle and handle._store is self and handle._token not in self.records:
            return
        if type(handle) is not FragmentHandle or handle._store is not self or self.handles.get(id(handle)) is not handle:
            raise ExportError("fragment_unavailable")
        record = self.records.get(handle._token)
        if record is not None:
            record.retired = True
        self.sweep()

    def close(self):
        _owner()
        self.closed = True
        self.sweep()


@dataclass(frozen=True, slots=True)
class WireMetadata:
    envelope_id: str
    fingerprint: str
    byte_count: int
    destination: str
    transport: str = "self_contained_test_json/1"
    observation_id: str | None = None
    image_count: int = 0
    schema_digest: str | None = None
    config_digest: str | None = None

    @property
    def visibility_scope(self):
        """Meaning of byte_count/fingerprint, not a claim of vendor token count."""
        if self.transport == "codex-exec-jsonl/1":
            return "visible_application_input"
        if self.transport == "self_contained_test_json/1":
            return "test_envelope"
        if self.transport == "openai-chat-completions-json/1":
            return "final_http_body"
        return "unknown"


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class SealedEnvelope:
    _store: object
    _token: str
    def __init__(self, store, token, *, _key=None):
        if _key is not _WIRE_KEY:
            raise TypeError("Use ExportBroker.seal")
        object.__setattr__(self,"_store",store)
        object.__setattr__(self,"_token",token)

    @property
    def metadata(self):
        record=self._store.get(self)
        if self._store.authorize is not None:
            self._store.authorize(record.scope,record.traces)
        return record.metadata

    def close(self):
        self._store.close_handle(self)


class _Wire:
    def __init__(self, token, wire, scope, destination, fragments, assets, traces, expires, charge):
        self.token, self.wire, self.scope = token, wire, scope
        self.fragments, self.assets, self.traces = tuple(fragments), tuple(assets), tuple(traces)
        self.expires, self.charge = expires, charge
        self.consumed, self.retired, self.pins = False, False, 0
        self.prepared = None
        self.lineage_charge = None
        self.metadata = WireMetadata(token,hashlib.sha256(wire).hexdigest(),len(wire),destination)


class _WireStore:
    def __init__(self, budget, assets, authorize=None, *, other_handles=None):
        self.other_handles = other_handles
        self.budget, self.assets,self.authorize = budget, assets,authorize
        self.records, self.handles = {}, WeakValueDictionary()
        self.closed = False

    def _destroy(self, record):
        self.records.pop(record.token,None)
        record.wire = None
        for asset in record.assets:
            self.assets.unpin(asset)
        record.fragments=record.traces=record.assets=()
        record.prepared=None
        if record.lineage_charge is not None:
            record.lineage_charge.close()
            record.lineage_charge = None
        for ident,handle in tuple(self.handles.items()):
            if handle._token==record.token:
                self.handles.pop(ident,None)
        record.charge.close()

    def sweep(self):
        _owner()
        now = self.budget.now()
        for record in tuple(self.records.values()):
            if self.closed or record.retired or now >= record.expires:
                record.retired = True
                if not record.pins:
                    self._destroy(record)

    def check_slot(self):
        """One shared handle ceiling for wires and live host model payloads."""
        _owner()
        self.sweep()
        other = 0 if self.other_handles is None else self.other_handles()
        if self.closed or len(self.records) + other >= self.budget.limits.max_wire_handles:
            raise ExportError("export_quota_exceeded")

    def seal(self, scope, destination, fragments, handles, traces, expires):
        """Private fixed serializer; all content-bearing parts are supplied here."""
        _owner()
        self.sweep()
        self.check_slot()
        pinned, charge, working = [], None, None
        content = payload = wire = None
        try:
            for handle in handles:
                pinned.append(self.assets.pin(handle,scope))
            # Bound content/base64 and reserve BEFORE thaw, encode or JSON allocation.
            skeleton = {"protocol":"agent-native.test-envelope/1","destination":destination,
                        "content":[],"assets":[]}
            bound = _json_bound(skeleton) + 1024
            bound += sum(item.charge.size + _json_bound(item.kind) + 64 for item in fragments)
            bound += sum(4*((len(a.encoded)+2)//3)+512 for a in pinned)
            if bound > self.budget.limits.max_envelope_bytes:
                raise ExportError("export_quota_exceeded")
            proof_size=_proof_bound(traces)
            charge = self.budget.reserve("retained",bound+proof_size)
            working = self.budget.reserve("working",3*bound)
            content = [{"kind":item.kind,"content":thaw(item.value)} for item in fragments]
            base = dict(skeleton)
            base["content"] = content
            payload = dict(base)
            payload["assets"] = [{"id":a.token,"mime":"image/png","frame_id":a.stamp.frame_id,
                "width":a.metadata.width,"height":a.metadata.height,"dpr":a.metadata.dpr,
                "target":"managed-content","data":base64.b64encode(a.encoded).decode("ascii")} for a in pinned]
            wire = json.dumps(payload,ensure_ascii=True,separators=(",",":"),sort_keys=True,allow_nan=False).encode("utf-8")
            if len(wire)>bound:
                raise ExportError("export_quota_exceeded")
            token = "envelope-"+uuid4().hex
            record = _Wire(token,wire,scope,destination,fragments,pinned,traces,expires,charge)
            handle = SealedEnvelope(self,token,_key=_WIRE_KEY)
            self.handles[id(handle)] = handle
            self.records[token] = record
            charge.resize(len(wire)+proof_size)
            self.budget.wires += 1
            content = payload = wire = base = None
            working.close()
            return handle
        except BaseException:
            content = payload = wire = None
            for asset in pinned:
                self.assets.unpin(asset)
            if charge is not None:
                charge.close()
            if working is not None:
                working.close()
            raise

    def seal_direct(self, scope, destination, fragments, handles, traces, expires,
                    *, profile, observation_id, output_reservation, prepared):
        """Exact approved adapter packet; TEST bytes never feed this path.

        HTTP exposes its final body. Codex exposes only bounded application input;
        vendor-added instructions, request wire and token ceilings remain opaque.

        Reserve text/schema/base64/final-body workspace BEFORE thaw/JSON/base64.
        Retain exact final bytes and asset pins until actual transfer cleanup.
        """
        from agent_native.runner.direct import serialize_installed_request, schema_digest
        _owner(); self.sweep()
        self.check_slot()
        pinned, charge, working = [], None, None
        parts = images = wire = None
        try:
            for handle in handles:
                pinned.append(self.assets.pin(handle, scope))
            # Context appears in text AND generated schema. Nested JSON text
            # escaping needs a second allowance. Include fixed framework routes.
            bound = 16384 + 4 * sum(_json_bound(item.value, projected=True)
                + _json_bound(item.kind) + 64 for item in fragments)
            bound += sum(4 * ((len(a.encoded) + 2) // 3) + 4096 + 1024 * len(a.metadata.visual_instances) for a in pinned)
            bound += _json_bound(profile.model)
            # One immutable generated program branch is derived only after this
            # reserve. Includes marker, copied context schema, prompt escaping and
            # output action schema. No existing global cap is increased.
            if prepared.get("sdk_program_offered", False):
                bound += 64 * 1024
            if bound > self.budget.limits.max_envelope_bytes:
                raise ExportError("export_quota_exceeded")
            # Temporary FragmentHandles may close as soon as sealing returns.
            # Retain only bounded contributor hashes from the ACTUAL approved
            # parts, not aliases to retired fragment values or raw text copies.
            contributor_bound = sum(256 + _json_bound(item.kind) for item in fragments)
            proof = _proof_bound(traces) + prepared["proof_size"] + contributor_bound
            charge = self.budget.reserve("retained", bound + proof)
            working = self.budget.reserve("working", 8 * bound)
            parts = [{"kind": item.kind, "content": thaw(item.value)} for item in fragments]
            images = [{"png": a.encoded, "frame_id": a.stamp.frame_id,
                "asset_id": a.token, "historical": a.token not in prepared["fresh_asset_tokens"],
                "source_sequence": a.stamp.cut.sequence, "width": a.metadata.width, "height": a.metadata.height,
                "dpr": a.metadata.dpr, "visual_instances": [
                    {"visual_id": v.visual_id, "bounds": list(v.bounds)} for v in a.metadata.visual_instances]}
                for a in pinned]
            wire = serialize_installed_request(profile, parts, images, output_reservation=output_reservation)
            if len(wire) > bound:
                raise ExportError("export_quota_exceeded")
            schemas = next((item["content"]["schemas"] for item in parts if item["kind"] == "context"),None)
            registry_digest=prepared["registry_schema_digest"] if schemas is None else schema_digest(schemas)
            token = "direct-envelope-" + uuid4().hex
            record = _Wire(token, wire, scope, destination, fragments, pinned, traces, expires, charge)
            record.metadata = WireMetadata(token, hashlib.sha256(wire).hexdigest(), len(wire), destination,
                profile.transport_version, observation_id, len(pinned), registry_digest, profile.config_digest)
            from agent_native.persistence.codec import encode_value
            prepared["fragment_digests"] = tuple((part["kind"],
                hashlib.sha256(encode_value(deep_freeze(part["content"]))).hexdigest()) for part in parts)
            record.prepared = prepared
            handle = SealedEnvelope(self, token, _key=_WIRE_KEY)
            self.handles[id(handle)] = handle
            self.records[token] = record
            charge.resize(len(wire) + proof)
            self.budget.wires += 1
            parts = images = wire = None
            working.close()
            return handle
        except BaseException:
            parts = images = wire = None
            for asset in pinned:
                self.assets.unpin(asset)
            if charge is not None:
                charge.close()
            if working is not None:
                working.close()
            raise

    def transfer(self, record):
        _owner()
        if record.consumed or record.retired or record.metadata.transport == "self_contained_test_json/1":
            raise ExportError("envelope_already_consumed")
        record.pins += 1
        record.consumed = True
        return WireTransfer(self, record)

    def get(self, handle, scope=None):
        _owner()
        self.sweep()
        if (type(handle) is not SealedEnvelope or handle._store is not self
                or self.handles.get(id(handle)) is not handle):
            raise ExportError("envelope_unavailable")
        record = self.records.get(handle._token)
        if record is None or (scope is not None and scope != record.scope):
            raise ExportError("envelope_unavailable")
        return record

    def close_handle(self, handle):
        _owner()
        if type(handle)is SealedEnvelope and handle._store is self and handle._token not in self.records:
            return
        record = self.get(handle)
        record.retired = True
        self.sweep()

    def close(self):
        _owner()
        self.closed = True
        self.sweep()


class WireTransfer:
    """Owner lifetime; the worker receives only ``wire`` bytes.

    Close after actual worker return and acknowledgement. Broker close/TTL
    cannot dispose bytes/assets/charge while this transfer pin remains live.
    """
    __slots__ = ("_store", "_record", "wire", "metadata", "closed")
    def __init__(self, store, record):
        self._store, self._record = store, record
        self.wire, self.metadata, self.closed = record.wire, record.metadata, False

    def close(self):
        _owner()
        if not self.closed:
            self.closed = True
            self.wire = None
            record, self._record = self._record, None
            record.pins -= 1
            record.retired = True
            self._store.sweep()
