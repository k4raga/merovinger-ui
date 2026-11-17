"""SDK-owned snapshot/lease admission. No external Python reference revocation."""
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4


class ReadViewUnavailable(RuntimeError):
    pass


class SnapshotQuotaExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    max_snapshots: int = 16
    max_leases: int = 64
    max_stages: int = 2
    max_retained_bytes: int = 64 * 1024 * 1024
    lease_ttl_ns: int = 60_000_000_000
    retention_ns: int = 10_000_000_000

    def __post_init__(self):
        for name in ("max_snapshots", "max_leases", "max_stages", "max_retained_bytes", "lease_ttl_ns"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a strict positive integer")
        if type(self.retention_ns) is not int or self.retention_ns < 0:
            raise ValueError("retention_ns must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SnapshotMeasurements:
    snapshots: int
    leases: int
    stages: int
    retained_bytes: int
    peak_snapshots: int
    peak_leases: int
    peak_retained_bytes: int
    expired_leases: int
    evicted_snapshots: int


@dataclass(slots=True)
class _Entry:
    data: object
    size: int
    retired_at: int | None = None


class _SnapshotPool:
    """All mutations guarded for worker reads/releases; semantic writes stay owner-only.

    Bytes are a conservative sum of logical snapshot sizes (shared nodes count again),
    not RSS. Current data has an SDK pin. Stages reserve a snapshot and publish lease,
    without a TTL while waiting for a durable Ack. Lease TTL begins on publication.
    Retention is a best-effort cache for unleased historical cuts.
    """
    def __init__(self, initial, size, limits, clock_ns):
        self.limits, self.clock_ns = limits, clock_ns
        self.lock = RLock()
        self.entries = {initial.sequence: _Entry(initial, size)}
        self.current = initial.sequence
        self.stages = {}
        self.leases = {}
        self.expired = self.evicted = 0
        self.peaks = [1, 0, size]
        self._last_now = -1
        self._now()
        if size > limits.max_retained_bytes:
            raise SnapshotQuotaExceeded("Initial snapshot exceeds byte quota")

    def _now(self):
        now = self.clock_ns()
        if type(now) is not int or now < 0 or now < self._last_now:
            raise ValueError("clock_ns must return monotonic non-negative exact integers")
        self._last_now = now
        return now

    def _clean(self, now):
        for token, (_, expires) in tuple(self.leases.items()):
            if now >= expires:
                del self.leases[token]
                self.expired += 1
        pinned = {sequence for sequence, _ in self.leases.values()}
        for sequence, entry in tuple(self.entries.items()):
            if (sequence != self.current and sequence not in pinned
                    and now >= entry.retired_at + self.limits.retention_ns):
                del self.entries[sequence]
                self.evicted += 1

    def _counts(self):
        return (len(self.entries) + len(self.stages),
                len(self.leases) + len(self.stages),
                sum(entry.size for entry in self.entries.values())
                + sum(entry.size for entry in self.stages.values()))

    def _peak(self):
        counts = self._counts()
        self.peaks = [max(a, b) for a, b in zip(self.peaks, counts)]

    def reserve(self, token, data, size):
        with self.lock:
            self._clean(self._now())
            if len(self.stages) >= self.limits.max_stages:
                raise SnapshotQuotaExceeded("Prepared stage quota exceeded")
            snapshots, leases, retained = self._counts()
            # Capacity may evict only unleased historical cuts; current/stages are pinned.
            pinned = {sequence for sequence, _ in self.leases.values()}
            candidates = [sequence for sequence in sorted(self.entries)
                          if sequence != self.current and sequence not in pinned]
            while candidates and (snapshots + 1 > self.limits.max_snapshots
                                  or retained + size > self.limits.max_retained_bytes):
                sequence = candidates.pop(0)
                retained -= self.entries.pop(sequence).size
                snapshots -= 1
                self.evicted += 1
            if (snapshots + 1 > self.limits.max_snapshots
                    or leases + 1 > self.limits.max_leases
                    or retained + size > self.limits.max_retained_bytes):
                raise SnapshotQuotaExceeded("Snapshot, lease or byte quota exceeded")
            self.stages[token] = _Entry(data, size)
            self._peak()

    def discard(self, token):
        with self.lock:
            self.stages.pop(token, None)

    def publish(self, token):
        with self.lock:
            now = self._now()
            entry = self.stages.pop(token)
            self.entries[self.current].retired_at = now
            self.current = entry.data.sequence
            self.entries[self.current] = entry
            lease = uuid4().hex
            expires = now + self.limits.lease_ttl_ns
            self.leases[lease] = (self.current, expires)
            self._clean(now)
            self._peak()
            return lease, expires

    def acquire(self, sequence=None):
        with self.lock:
            now = self._now()
            self._clean(now)
            sequence = self.current if sequence is None else sequence
            if type(sequence) is not int or sequence < 0:
                raise ValueError("Snapshot sequence must be an exact non-negative integer")
            if sequence not in self.entries:
                raise ReadViewUnavailable("Snapshot is no longer retained")
            if self._counts()[1] >= self.limits.max_leases:
                raise SnapshotQuotaExceeded("ReadView lease quota exceeded")
            token = uuid4().hex
            expires = now + self.limits.lease_ttl_ns
            self.leases[token] = (sequence, expires)
            self._peak()
            return token, expires

    def get_staged(self, token):
        with self.lock:
            entry = self.stages.get(token)
            if entry is None or entry.data.sequence != self.current + 1:
                raise ReadViewUnavailable("Staged cut was consumed, discarded or became stale")
            return entry.data

    def get(self, token):
        with self.lock:
            self._clean(self._now())
            lease = self.leases.get(token)
            if lease is None:
                raise ReadViewUnavailable("ReadView expired or was released")
            entry = self.entries.get(lease[0])
            if entry is None:
                raise ReadViewUnavailable("Snapshot is unavailable")
            return entry.data

    def release(self, token):
        with self.lock:
            self.leases.pop(token, None)
            self._clean(self._now())

    def measurements(self):
        with self.lock:
            self._clean(self._now())
            snapshots, leases, retained = self._counts()
            return SnapshotMeasurements(snapshots, leases - len(self.stages), len(self.stages),
                                        retained, *self.peaks, self.expired, self.evicted)
