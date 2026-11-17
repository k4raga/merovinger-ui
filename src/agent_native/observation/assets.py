"""Private retained REDACTED image assets and bounded leases.

Handles are metadata-only. Only the owning ExportBroker can read an encoded
variant, after live C2/projector authorization. No raw QImage is retained here.
"""
from dataclasses import dataclass
from uuid import uuid4
from weakref import WeakValueDictionary
import hashlib

from ._export_types import ExportError, _owner, _proof_bound


_ASSET_KEY = object()


@dataclass(frozen=True, slots=True)
class VisualInstance:
    visual_id: str
    bounds: tuple[int,int,int,int]


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    asset_id: str
    frame_id: str
    width: int
    height: int
    dpr: float
    visual_instances: tuple[VisualInstance,...] = ()
    mime: str = "image/png"
    capture_target: str = "managed-content"


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class AssetHandle:
    _store: object
    _token: str
    def __init__(self, store, token, *, _key=None):
        if _key is not _ASSET_KEY:
            raise TypeError("Use ObservationChannel")
        object.__setattr__(self,"_store",store)
        object.__setattr__(self,"_token",token)

    @property
    def metadata(self):
        return self._store._metadata(self)

    def close(self):
        self._store._close_handle(self)


class _AssetRecord:
    def __init__(self, token, encoded, stamp, mappings, traces, scope, expires, charge, *, crop=None):
        self.token, self.encoded, self.stamp = token, encoded, stamp
        self.mappings, self.traces, self.scope = tuple(mappings), tuple(traces), scope
        self.expires, self.charge, self.crop = expires, charge, crop
        self.pins, self.retired = 0, False
        self.digest = hashlib.sha256(encoded).hexdigest()
        width, height = stamp.pixel_size if crop is None else crop[2:]
        origin=(0,0) if crop is None else crop[:2]
        visuals=[]
        for mapping in mappings:
            if not mapping.node.rendered:
                continue
            x,y,w,h=mapping.conservative_pixel_rect
            left,top=max(0,x-origin[0]),max(0,y-origin[1])
            right,bottom=min(width,x+w-origin[0]),min(height,y+h-origin[1])
            if right>left and bottom>top:
                visuals.append(VisualInstance("visual-"+uuid4().hex,(left,top,right-left,bottom-top)))
        self.metadata = AssetMetadata(token,stamp.frame_id,width,height,stamp.dpr,tuple(visuals))


class _AssetSlot:
    def __init__(self,store,token):
        self.store,self.token,self.closed=store,token,False
    def close(self):
        _owner()
        if not self.closed:
            self.closed=True
            self.store.slots.pop(self.token,None)


class _AssetStore:
    def __init__(self, budget, authorize=None):
        self.budget,self.authorize = budget,authorize
        self.records, self.handles,self.slots = {}, WeakValueDictionary(),{}
        self.closed = False

    def sweep(self):
        _owner()
        now = self.budget.now()
        for record in tuple(self.records.values()):
            if self.closed or now >= record.expires:
                record.retired = True
            if record.retired and not record.pins:
                self._destroy(record)

    def _destroy(self, record):
        self.records.pop(record.token, None)
        record.encoded = None
        record.traces=record.mappings=()
        for ident,handle in tuple(self.handles.items()):
            if handle._token==record.token:
                self.handles.pop(ident,None)
        record.charge.close()

    def _record(self, handle, *, scope=None):
        _owner()
        self.sweep()
        if (type(handle) is not AssetHandle or handle._store is not self
                or self.handles.get(id(handle)) is not handle):
            raise ExportError("asset_unavailable")
        record = self.records.get(handle._token)
        if record is None or record.retired or self.budget.now() >= record.expires:
            raise ExportError("asset_unavailable")
        if scope is not None and scope != record.scope:
            raise ExportError("asset_unavailable")
        return record

    def _metadata(self, handle):
        record=self._record(handle)
        if self.authorize is not None:
            self.authorize(record.scope,record.traces)
        return record.metadata

    def reserve_slot(self):
        _owner()
        self.sweep()
        if self.closed or len(self.records)+len(self.slots)>=self.budget.limits.max_assets:
            raise ExportError("export_quota_exceeded")
        token=uuid4().hex
        slot=_AssetSlot(self,token)
        self.slots[token]=slot
        return slot

    def add(self, encoded, stamp, mappings, traces, scope, expires, *, charge=None, crop=None, slot=None):
        _owner()
        self.sweep()
        if (self.closed or type(encoded) is not bytes or not encoded.startswith(b"\x89PNG\r\n\x1a\n")
                or type(expires) is not int or expires <= self.budget.now()):
            raise ExportError("asset_unavailable")
        slot=self.reserve_slot()if slot is None else slot
        if type(slot)is not _AssetSlot or slot.store is not self or slot.closed or self.slots.get(slot.token)is not slot:
            raise ExportError("export_reservation_invalid")
        size=len(encoded)+_proof_bound(traces)+1024*len(mappings)
        owned=None
        try:
            owned = self.budget.reserve("retained",size) if charge is None else charge
            if owned.kind != "retained" or owned.size < size or owned.closed:
                raise ExportError("export_reservation_invalid")
            token = "asset-"+uuid4().hex
            record = _AssetRecord(token,encoded,stamp,mappings,traces,scope,expires,owned,crop=crop)
            handle = AssetHandle(self,token,_key=_ASSET_KEY)
            self.handles[id(handle)] = handle
            self.records[token] = record
            slot.close()
            owned.resize(size)
            return handle
        except BaseException:
            slot.close()
            if owned is not None:
                owned.close()
            raise

    def reissue(self,record):
        _owner()
        if self.records.get(record.token)is not record or record.retired or self.budget.now()>=record.expires:
            raise ExportError("asset_unavailable")
        for handle in self.handles.values():
            if handle._token==record.token:
                return handle
        handle=AssetHandle(self,record.token,_key=_ASSET_KEY)
        self.handles[id(handle)]=handle
        return handle

    def read(self, handle, scope):
        return self._record(handle,scope=scope)

    def pin(self, handle, scope):
        record = self._record(handle,scope=scope)
        record.pins += 1
        return record

    def unpin(self, record):
        _owner()
        if record.pins < 1:
            raise ExportError("asset_pin_invalid")
        record.pins -= 1
        self.sweep()

    def _close_handle(self, handle):
        _owner()
        if type(handle)is AssetHandle and handle._store is self and handle._token not in self.records:
            return
        if type(handle) is not AssetHandle or handle._store is not self or self.handles.get(id(handle)) is not handle:
            raise ExportError("asset_unavailable")
        record = self.records.get(handle._token)
        if record is not None:
            record.retired = True
        self.sweep()

    def close(self):
        _owner()
        self.closed = True
        self.sweep()
