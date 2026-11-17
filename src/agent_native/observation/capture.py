"""Controlled QWidget render over actual CURRENT C1/C2 and applied C3 cut.

No desktop/foreign screenshot, event-loop yield, nested apply, encoder or policy
engine. Raw capture is trusted/transient. C4 export MUST separately use the live
ContextExportGate and full 017 policy path; a raw frame is not export authority.
"""
import math
import traceback
from contextlib import contextmanager
from time import monotonic_ns
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from agent_native.kernel.coordinator import Phase, PublishedCut, CoordinatorBusy
from .frames import (CaptureLimits, CaptureError, FrameExpired, FrameStamp,
    CapturedMapping, CaptureBatch, OwnedRawCapture, _RawBudget, _RawResource, _RAW_KEY, _owner)
from .surfaces import SurfaceRegistry, SurfaceCoverage


def _cache_versions(cut):
    return tuple(sorted(((key,entry.version) for key,entry in cut.derived_entries.items()),key=lambda x:repr(x[0])))


def _physical(rect, dpr, width, height, guard=0):
    x,y,w,h = rect
    if w <= 0 or h <= 0:
        return (0,0,0,0)
    left,top = max(0,math.floor(x*dpr)-guard),max(0,math.floor(y*dpr)-guard)
    right,bottom = min(width,math.ceil((x+w)*dpr)+guard),min(height,math.ceil((y+h)*dpr)+guard)
    return left,top,max(0,right-left),max(0,bottom-top)


class CaptureService:
    """Owner-thread capture and private bounded actual-pair retention.

    capture() performs at most three IDLE preflight/attempts. capture_in_guard() performs NO layout
    work; it requires the caller's real CAPTURING guard and current exact cut.
    History only returns a retained handle, never re-renders current widgets.
    Close handles/service explicitly. Quotas are SDK buffer accounting, not RSS.
    """
    def __init__(self, coordinator, surfaces, *, limits=None, clock_ns=monotonic_ns):
        _owner()
        if type(surfaces) is not SurfaceRegistry or surfaces.coordinator is not coordinator:
            raise ValueError("Use the same owned surface registry/coordinator")
        if limits is not None and type(limits) is not CaptureLimits:
            raise TypeError("Use CaptureLimits")
        self.coordinator, self.surfaces = coordinator, surfaces
        self.limits = CaptureLimits() if limits is None else limits
        self._budget = _RawBudget(self.limits,clock_ns)
        existing = getattr(coordinator,"_owned_widget_capture_service",None)
        if existing is not None:
            raise CaptureError("capture_runtime_already_owned")
        self._history, self._sequence, self._closed = {}, 0, False
        self._attempt_active = False
        self._budget.when_empty = self._release_runtime
        self.capture_count = 0
        coordinator._owned_widget_capture_service = self

    def _check(self):
        _owner()
        if self._closed:
            raise CaptureError("capture_service_closed")
        self._budget.sweep()
        for handle in tuple(self._history.values()):
            try:
                handle._check()
            except FrameExpired:
                pass

    def _current(self, cut):
        if self.coordinator.phase != Phase.CAPTURING:
            raise CaptureError("capture_guard_required")
        try:
            if type(cut) is not PublishedCut or not self.coordinator.validate_cut(cut):
                raise CaptureError("capture_cut_unavailable")
            with self.coordinator.read() as current:
                if (cut.identity != current.identity or cut.derived_revision != current.derived_revision
                        or cut.derived_entries is not current.derived_entries
                        or _cache_versions(cut) != _cache_versions(current)):
                    raise CaptureError("capture_cut_not_current")
        except CaptureError:
            raise
        except Exception as error:
            raise CaptureError("capture_cut_unavailable") from error

    @contextmanager
    def _attempt(self):
        # Entry guard precedes even sweep/history mutations. It spans preflight,
        # all retries, paint, postchecks and cleanup, not only the Qt render call.
        _owner()
        if self._attempt_active:
            raise CaptureError("capture_busy")
        self._attempt_active = True
        try:
            yield
        finally:
            self._attempt_active = False
            self._release_runtime()

    def capture(self, surface_ids=None, *, required_surface_ids=(), deadline_ns=None):
        """Owned preflight/render; recursive public entries are rejected."""
        with self._attempt():
            return self._capture(surface_ids,required_surface_ids=required_surface_ids,deadline_ns=deadline_ns)

    def capture_in_guard(self, cut, surface_ids=None, *, required_surface_ids=(), deadline_ns=None):
        """One external-guard attempt; CAPTURING alone is not render reentry."""
        with self._attempt():
            return self._capture_in_guard(cut,surface_ids,required_surface_ids=required_surface_ids,deadline_ns=deadline_ns)

    def _capture(self, surface_ids=None, *, required_surface_ids=(), deadline_ns=None):
        self._check()
        if self.coordinator.phase != Phase.IDLE:
            raise CaptureError("capture_busy")
        engines, _ = self.surfaces._select(surface_ids,required_surface_ids)
        ids = tuple(e.surface_id for e in engines)
        try:
            for attempt in range(3):
                batch = None
                for engine in engines:
                    engine.prepare_capture()
                try:
                    with self.coordinator.capture("owned-widget-capture") as cut:
                        batch = self._capture_in_guard(cut,ids,required_surface_ids=required_surface_ids,deadline_ns=deadline_ns)
                    self._check()
                    for frame in batch.frames:
                        frame._check()
                    return batch
                except BaseException as error:
                    if batch is not None:
                        batch.close()
                    if not isinstance(error,CaptureError) or error.code != "capture_revision_changed" or attempt == 2:
                        raise
                    # Lazy realization changed. Retry only after guard exit and a
                    # new preflight. Never process events/drain/rebase here.
        except CaptureError:
            raise
        except CoordinatorBusy as error:
            raise CaptureError("capture_busy") from error
        except Exception as error:
            raise CaptureError("capture_realization_unavailable") from error

    def _capture_in_guard(self, cut, surface_ids=None, *, required_surface_ids=(), deadline_ns=None):
        self._check()
        self._current(cut)
        if deadline_ns is not None and (type(deadline_ns) is not int or deadline_ns < 0):
            raise ValueError("Use a monotonic exact deadline")
        def deadline():
            if deadline_ns is not None and self._budget.now() >= deadline_ns:
                raise CaptureError("capture_deadline")
        deadline()
        engines, coverage = self.surfaces._select(surface_ids,required_surface_ids)
        reservations, retained, built = [], [], []
        images = []
        attempt_succeeded = False
        failure = None
        base_sequence = self._sequence
        try:
            self._effects(engines)
            before = tuple(e.realization_snapshot(cut) for e in engines)
            if sum(len(s.mappings) for s in before) > self.limits.max_mappings:
                raise CaptureError("capture_mapping_quota_exceeded")
            sizes = []
            for engine,snapshot in zip(engines,before):
                if snapshot.surface_kind != "managed-content" or snapshot.omitted_categories != ("diagnostic-chrome",):
                    raise CaptureError("unsupported_surface_contract")
                dpr = engine.capture_root.devicePixelRatioF()
                if type(dpr) is not float or not math.isfinite(dpr) or dpr <= 0:
                    raise CaptureError("unsupported_dpr")
                width,height = math.ceil(engine.capture_root.width()*dpr),math.ceil(engine.capture_root.height()*dpr)
                size = width*height*4
                if min(width,height) < 1 or size > self.limits.max_surface_bytes:
                    raise CaptureError("capture_surface_quota_exceeded")
                if any(m.transform != (1,0,0,1,0,0) or m.dpr != dpr for m in snapshot.mappings):
                    raise CaptureError("unsupported_mapping_transform")
                sizes.append((dpr,width,height,size))
            # Reserve ALL batch copy peaks and independent cut leases before allocation.
            for dpr,width,height,size in sizes:
                reservation = self._budget.reserve(2*size,frames=1)
                reservation.pinned = True  # Active render cannot expire/free accounting.
                reservations.append(reservation)
                lease = self.coordinator.read()
                retained.append(lease)
                self._current(lease)
            for engine,snapshot,shape,reservation in zip(engines,before,sizes,reservations):
                deadline()
                dpr,width,height,size = shape
                sampled = self._budget.now()
                detached = self._render(engine,width,height,dpr,size)
                reservation.resize(detached.sizeInBytes())
                images.append((detached,sampled,self._budget.now()))
                detached = None
            self._check()
            self._current(cut)
            if any(self._budget.now() >= r.expires for r in reservations):
                raise FrameExpired()
            # Whole-batch postcheck: render B may invalidate already captured A.
            self._effects(engines)
            after = tuple(e.realization_snapshot(cut) for e in engines)
            if before != after:
                raise CaptureError("capture_revision_changed")
            deadline()
            coverage = tuple(SurfaceCoverage(c.surface_id,c.kind,True,"captured",c.parent_surface)
                if c.status=="selected" else c for c in coverage)
            for number,(engine,snapshot,shape,reservation,lease,pixels) in enumerate(zip(engines,before,sizes,reservations,retained,images),1):
                self._current(lease)  # Includes an actual independent lease touch after render.
                dpr,width,height,size = shape
                image,sampled_ns,completed_ns = pixels
                mappings = tuple(CapturedMapping(m,
                    _physical(m.clipped_rect,dpr,width,height),
                    _physical(m.clipped_rect,dpr,width,height,math.ceil(dpr)+1)) for m in snapshot.mappings)
                stamp = FrameStamp(uuid4().hex,cut.identity,cut.derived_revision,_cache_versions(cut),
                    snapshot.view,snapshot.surface_id,snapshot.surface_generation,snapshot.layout_epoch,
                    base_sequence+number,sampled_ns,completed_ns,dpr,(engine.capture_root.width(),engine.capture_root.height()),
                    (width,height),snapshot.realization_fingerprint,presentation=snapshot.presentation)
                resource = _RawResource(image,lease,stamp,mappings,coverage,reservation,self._forget)
                handle = OwnedRawCapture(resource,_key=_RAW_KEY)
                built.append(handle)
            batch = CaptureBatch(tuple(built),coverage)
            # Prepare complete metadata before making any new handle discoverable.
            history = dict(self._history)
            for handle in built:
                handle._check()
                history[handle._resource.stamp.frame_id] = handle
            next_count = self.capture_count + len(built)
            next_sequence = base_sequence + len(built)
            self._history, self.capture_count, self._sequence = history, next_count, next_sequence
            attempt_succeeded = True
            return batch
        except BaseException as error:
            failure = error
            if isinstance(error,CaptureError) or not isinstance(error,Exception):
                raise
            raise CaptureError("capture_realization_unavailable",coverage=coverage) from error
        finally:
            # Failed helper constructor frames can retain QImage arguments.
            # Preserve traceback locations, but clear nonexecuting frame locals
            # before releasing physical-buffer accounting. Our active frame's
            # own raw aliases are cleared explicitly below.
            if failure is not None:
                traceback.clear_frames(failure.__traceback__)
            images.clear()
            image = detached = pixels = resource = handle = batch = failure = None
            if not attempt_succeeded:
                for handle in built:
                    handle.close()
                for lease in retained:
                    lease.close()
                for reservation in reservations:
                    if reservation.cleanup is not None:
                        reservation.cleanup()
                    else:
                        reservation.close()
            else:
                # All native local aliases are gone; only returned idle owners
                # retain images. They may now expire, or MOVE re-pins them.
                for reservation in reservations:
                    reservation.pinned = False

    @staticmethod
    def _render(engine,width,height,dpr,size):
        image = detached = None
        try:
            image = QImage(width,height,QImage.Format.Format_RGBA8888)
            if image.isNull() or image.sizeInBytes() > size:
                raise CaptureError("capture_allocation_failed")
            image.setDevicePixelRatio(dpr)
            image.fill(Qt.GlobalColor.transparent)
            engine.capture_root.render(image)
            if image.isNull() or image.sizeInBytes() > size:
                raise CaptureError("capture_buffer_changed")
            detached = image.copy()
            if detached.isNull() or detached.sizeInBytes() > size:
                raise CaptureError("capture_detach_failed")
            return detached
        finally:
            image = detached = None

    @staticmethod
    def _effects(engines):
        for engine in engines:
            for widget in engine.known_widgets:
                if (widget is engine.capture_root or engine.capture_root.isAncestorOf(widget)) and widget.graphicsEffect() is not None:
                    raise CaptureError("unsupported_render_effect")

    def _forget(self, frame_id):
        self._history.pop(frame_id,None)

    def historical(self, frame_id):
        self._check()
        if type(frame_id) is not str:
            raise TypeError("Use an exact frame ID")
        handle = self._history.get(frame_id)
        if handle is None:
            raise FrameExpired()
        handle._check()
        return handle

    def measurements(self):
        _owner()
        if not self._closed:
            self._check()
        return self._budget.measurements()

    def close(self):
        _owner()
        if self._closed:
            self._release_runtime()
            return
        self._closed = True
        self._budget.admitting = False
        # Intake shutdown is NOT worker completion. Pinned moved raw/output
        # allocations remain charged until the real job cleans up on owner.
        for r in tuple(self._budget.reservations.values()):
            if not r.pinned:
                if r.cleanup is not None:
                    r.cleanup()
                else:
                    r.close()
        self._history.clear()
        self._release_runtime()

    def _release_runtime(self):
        # This is only a runtime ownership marker, never semantic authority.
        if self._closed and not self._attempt_active and not self._budget.reservations:
            if getattr(self.coordinator,"_owned_widget_capture_service",None) is self:
                del self.coordinator._owned_widget_capture_service
