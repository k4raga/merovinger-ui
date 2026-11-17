"""Bounded owned EngineUi surface registry, not OS/desktop enumeration."""
from dataclasses import dataclass
from uuid import uuid4
from agent_native.ui import EngineUi
from agent_native.kernel.coordinator import Phase
from .frames import CaptureError, _owner


@dataclass(frozen=True, slots=True)
class SurfaceCoverage:
    surface_id: str
    kind: str
    included: bool
    status: str
    parent_surface: str | None = None


class SurfaceRegistry:
    """Only actual managed EngineUi roots of one Coordinator are renderable.

    Optional unsupported declarations describe host-known missing surfaces; they
    do not claim discovery/capture of platform candidate windows. Unknown actual
    child/top-level surfaces cannot become renderable by supplying their address.
    """
    def __init__(self, coordinator, *, max_surfaces=8):
        _owner()
        if type(max_surfaces) is not int or max_surfaces < 1:
            raise ValueError("A positive surface quota is required")
        self.coordinator, self.max_surfaces = coordinator, max_surfaces
        self._entries, self._unsupported = {}, {}

    def _idle(self):
        _owner()
        if self.coordinator.phase != Phase.IDLE:
            raise CaptureError("surface_registry_busy")

    def register(self, engine):
        self._idle()
        if type(engine) is not EngineUi or engine.coordinator is not self.coordinator or engine.closed:
            raise CaptureError("unsupported_surface")
        if engine.surface_id in self._entries:
            if self._entries[engine.surface_id] is engine:
                return engine.surface_id
            raise CaptureError("surface_identity_collision")
        if len(self._entries) + len(self._unsupported) >= self.max_surfaces:
            raise CaptureError("surface_quota_exceeded")
        self._entries[engine.surface_id] = engine
        return engine.surface_id

    def unregister(self, surface_id):
        self._idle()
        self._entries.pop(surface_id, None)
        self._unsupported = {k:v for k,v in self._unsupported.items() if v.parent_surface != surface_id}

    def declare_unsupported(self, parent_surface, kind):
        self._idle()
        if parent_surface not in self._entries or kind not in ("popup", "tooltip", "ime_candidate", "native", "gpu", "video"):
            raise CaptureError("unsupported_surface")
        if len(self._entries) + len(self._unsupported) >= self.max_surfaces:
            raise CaptureError("surface_quota_exceeded")
        token = "omitted-" + uuid4().hex
        self._unsupported[token] = SurfaceCoverage(token,kind,False,"declared_unsupported",parent_surface)
        return token

    def remove_unsupported(self, surface_id):
        self._idle()
        self._unsupported.pop(surface_id, None)

    def _select(self, surface_ids=None, required_surface_ids=()):
        _owner()
        ids = tuple(self._entries) if surface_ids is None else tuple(surface_ids)
        required = tuple(required_surface_ids)
        if (not ids or any(type(v) is not str for v in (*ids,*required))
                or len(ids) != len(set(ids)) or len(required) != len(set(required))):
            raise CaptureError("invalid_surface_selection")
        if any(v not in self._entries for v in ids):
            raise CaptureError("unsupported_surface")
        coverage = tuple(SurfaceCoverage(v,"managed-content",False,"selected") for v in ids)
        coverage += tuple(SurfaceCoverage(v+":diagnostic-chrome","diagnostic-chrome",False,
            "outside_capture_target",v) for v in ids)
        coverage += tuple(v for v in self._unsupported.values() if v.parent_surface in ids)
        missing = set(required) - set(ids)
        if missing:
            raise CaptureError("required_surface_unavailable",coverage=coverage)
        engines = tuple(self._entries[v] for v in ids)
        if any(e.closed or e.coordinator is not self.coordinator or e.surface_id != v for v,e in zip(ids,engines)):
            raise CaptureError("surface_expired",coverage=coverage)
        return engines, coverage
