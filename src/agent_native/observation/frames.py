"""Immutable capture metadata and private linear raw ownership.

A raw capture is trusted transient state, never a model asset. Public handles
provide metadata/close only: no QImage, pixel, bits, bytes, encoder or callback.
The package-private move transfers the SAME resource and reservation to C4.
"""
from dataclasses import dataclass
from threading import current_thread, main_thread
from time import monotonic_ns
from uuid import uuid4


class CaptureError(RuntimeError):
    def __init__(self, code, *, coverage=()):
        super().__init__(code)
        self.code, self.coverage = code, tuple(coverage)


class FrameExpired(CaptureError):
    def __init__(self):
        super().__init__("frame_expired")


def _owner():
    if current_thread() is not main_thread():
        raise CaptureError("capture_owner_thread_required")


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    max_frames: int = 8
    max_raw_bytes: int = 128 * 1024 * 1024
    max_surface_bytes: int = 32 * 1024 * 1024
    ttl_ns: int = 10_000_000_000
    max_mappings: int = 4096
    max_reservations: int = 32

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (
                self.max_frames, self.max_raw_bytes, self.max_surface_bytes,
                self.ttl_ns, self.max_mappings, self.max_reservations)):
            raise ValueError("Capture limits must be positive exact integers")


@dataclass(frozen=True, slots=True)
class FrameStamp:
    frame_id: str
    cut: object
    derived_revision: int
    cache_versions: tuple
    view: object
    surface_id: str
    surface_generation: int
    layout_epoch: int
    frame_sequence: int
    sampled_ns: int
    completed_ns: int
    dpr: float
    logical_size: tuple[int, int]
    pixel_size: tuple[int, int]
    realization_fingerprint: str
    capture_method: str = "owned_qwidget_render"
    capture_target: str = "managed-content"
    presentation: str = "rendered_offscreen"


@dataclass(frozen=True, slots=True)
class CapturedMapping:
    node: object
    pixel_rect: tuple[int, int, int, int]
    conservative_pixel_rect: tuple[int, int, int, int]
    coordinate_space: str = "root_logical_px"


@dataclass(frozen=True, slots=True)
class CaptureMeasurements:
    active_frames: int
    reserved_bytes: int
    peak_frames: int
    peak_bytes: int


class _Reservation:
    def __init__(self, budget, size, frames, expires):
        self.budget, self.size, self.frames, self.expires = budget, size, frames, expires
        self.token, self.cleanup, self.available, self.closed = uuid4().hex, None, None, False
        self.pinned = False

    def resize(self, size):
        _owner()
        if self.closed or not 0 <= size <= self.size:
            raise CaptureError("capture_reservation_invalid")
        self.budget.bytes -= self.size - size
        self.size = size

    def close(self):
        _owner()
        if self.closed:
            return
        self.closed = True
        self.budget.bytes -= self.size
        self.budget.frames -= self.frames
        self.budget.reservations.pop(self.token, None)
        self.cleanup = self.available = None
        if not self.budget.reservations and self.budget.when_empty is not None:
            self.budget.when_empty()

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()


class _RawBudget:
    def __init__(self, limits, clock_ns=monotonic_ns):
        self.limits, self.clock_ns = limits, clock_ns
        self.reservations = {}
        self.bytes = self.frames = self.peak_bytes = self.peak_frames = 0
        self._last_now = -1
        self.admitting = True
        self.when_empty = None
        if not callable(clock_ns):
            raise TypeError("A synchronous monotonic clock is required")
        self.now()

    def now(self):
        value = self.clock_ns()
        if type(value) is not int or value < 0 or value < self._last_now:
            raise ValueError("Capture clock must be monotonic nonnegative integers")
        self._last_now = value
        return value

    def sweep(self):
        _owner()
        now = self.now()
        for r in tuple(self.reservations.values()):
            if not r.pinned and (now >= r.expires or (r.available is not None and not r.available())):
                if r.cleanup is not None:
                    r.cleanup()
                else:
                    r.close()

    def reserve(self, size, *, frames=0, expires=None):
        _owner()
        self.sweep()
        if not self.admitting:
            raise CaptureError("capture_service_closed")
        if type(size) is not int or size < 0 or type(frames) is not int or frames < 0:
            raise ValueError("Exact nonnegative reservation required")
        now = self.now()
        expires = now + self.limits.ttl_ns if expires is None else expires
        if expires <= now:
            raise FrameExpired()
        if (self.bytes + size > self.limits.max_raw_bytes or self.frames + frames > self.limits.max_frames
                or len(self.reservations) >= self.limits.max_reservations):
            raise CaptureError("capture_quota_exceeded")
        r = _Reservation(self, size, frames, expires)
        self.reservations[r.token] = r
        self.bytes += size
        self.frames += frames
        self.peak_bytes, self.peak_frames = max(self.peak_bytes, self.bytes), max(self.peak_frames, self.frames)
        return r

    def measurements(self):
        self.sweep()
        return CaptureMeasurements(self.frames, self.bytes, self.peak_frames, self.peak_bytes)


class _RawResource:
    def __init__(self, image, cut, stamp, mappings, coverage, reservation, forget):
        self.image, self.cut, self.stamp = image, cut, stamp
        self.mappings, self.coverage = tuple(mappings), tuple(coverage)
        self.reservation, self.forget, self.closed = reservation, forget, False
        reservation.cleanup = self.close
        reservation.available = lambda: self.cut.view.available

    def check(self):
        _owner()
        if self.closed:
            raise FrameExpired()
        if (self.reservation.budget.now() >= self.reservation.expires
                or not self.cut.view.available):
            if not self.reservation.pinned:
                self.close()
            raise FrameExpired()

    def close(self):
        _owner()
        if self.closed:
            return
        self.closed = True
        self.forget(self.stamp.frame_id)
        self.image = None
        try:
            self.cut.close()
        finally:
            self.reservation.close()


class OwnedRawCapture:
    """Metadata-only owner of one captured pair; context-managed and linear.

    Only CaptureService creates these handles. Retention owns a separate issued
    PublishedCut lease; the borrowed capture guard cut is never retained.
    """
    __slots__ = ("_resource", "_state")

    def __init__(self, resource, *, _key=None):
        if _key is not _RAW_KEY:
            raise TypeError("Use CaptureService")
        self._resource, self._state = resource, "open"

    def _check(self):
        _owner()
        if self._state != "open":
            raise FrameExpired()
        self._resource.check()

    @property
    def stamp(self):
        self._check()
        return self._resource.stamp

    @property
    def mappings(self):
        self._check()
        return self._resource.mappings

    @property
    def coverage(self):
        self._check()
        return self._resource.coverage

    @property
    def closed(self):
        _owner()
        return self._state != "open" or self._resource.closed

    def close(self):
        _owner()
        if self._state == "open":
            self._state = "closed"
            self._resource.close()

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *error):
        self.close()


class _MovedRawOwner:
    """Private 017 redactor ownership, not a public raw/policy bypass API."""
    __slots__ = ("_resource",)

    def __init__(self, resource):
        self._resource = resource

    def _check(self):
        self._resource.check()

    def _reserve_output(self, byte_count):
        self._check()
        reservation = self._resource.reservation.budget.reserve(
            byte_count, expires=self._resource.reservation.expires)
        reservation.pinned = True
        return reservation

    def close(self):
        self._resource.close()

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *error):
        self.close()


_RAW_KEY = object()


def _move_raw(handle):
    """C4-internal MOVE: invalidate source, preserve image/lease/quota until close.

    The recipient must use try/finally, reserve output BEFORE QImage.copy(), and
    complete its own policy gate. Expiry denies NEW access, but never frees a
    processing buffer: recipient reports actual job completion then closes on
    the owner thread. Output reservations are likewise explicitly released only
    after real allocation cleanup/transfer to the 017 asset budget. No raw bytes
    enter a public cache/history.
    """
    if type(handle) is not OwnedRawCapture:
        raise TypeError("Use an engine-issued raw handle")
    handle._check()
    resource = handle._resource
    # Allocation may fail. Until it succeeds the original handle/history remain
    # the only usable owner and retain exactly their existing accounting.
    recipient = _MovedRawOwner(resource)
    resource.reservation.pinned = True
    handle._state = "moved"
    resource.forget(resource.stamp.frame_id)
    return recipient


@dataclass(frozen=True, slots=True)
class CaptureBatch:
    frames: tuple[OwnedRawCapture, ...]
    coverage: tuple

    def close(self):
        for frame in self.frames:
            frame.close()

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()
