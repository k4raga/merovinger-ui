"""Nonblocking SQLite FIFO and independently issued original I/O observations.

Owned host registration captures fixed DTO headers/immutable semantic-map
bindings; it does not traverse, encode or fingerprint the graph. Canonical full
fingerprints are first formed on the SQLite worker, before SQL/observer exposure.
Accepted C1 Record/FrozenMap internals remain immutable under this boundary.

At most 32 owned attempts are retained (default min(queue_limit, 32)). UNKNOWN
keeps at most two existing codec-limited canonical payloads per slot: snapshot
and bundle, each <=64 MiB, plus fixed headers. Definitive observations drop full
graph bindings/bytes before exposure and retain compact originals until explicit
bookkeeping release. This is neither C4/RSS accounting nor a C1 prepare receipt.
"""
from concurrent.futures import Future, InvalidStateError
from inspect import isawaitable, iscoroutine, iscoroutinefunction
from queue import Queue, Full, Empty
from threading import Lock, Thread, get_ident
from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4
from weakref import ref

from agent_native.kernel import DurableSubmission, SubmitDisposition, DBOutcome, DurableOutcome, CutIdentity
from agent_native.state import SnapshotData, FrozenMap, FrozenList
from .storage import (SQLiteDatabase, ShutdownResult, submission_fingerprint,
                      JournalResolution, BootstrapImage, CommittedBundle)
from .codec import MAPS, encode_snapshot, decode_snapshot, encode_value, decode_value


def _resolve(future, value=None, error=None):
    # Cancellation of an observer never interrupts SQL or kills the worker.
    try:
        if error is None:
            future.set_result(value)
        else:
            future.set_exception(error)
        return True
    except InvalidStateError:
        return False


def _sync_result(value):
    if isawaitable(value):
        if iscoroutine(value):
            value.close()
        raise TypeError("Worker callbacks must be synchronous")


def _request_identity(submission):
    try:
        return submission.transaction_id, submission_fingerprint(submission)
    except Exception:
        return submission.transaction_id, None


# New owned API: original I/O observations, NOT C1 prepare ownership receipts.


class OriginalOutcomeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require_original(condition, code):
    if not condition:
        raise OriginalOutcomeError(code)


def _same_original(actual, original):
    if type(actual) is not type(original):
        return False
    if type(original) is tuple:
        return len(actual) == len(original) and all(
            _same_original(a, b) for a, b in zip(actual, original))
    if type(original) in (str, int, type(None)):
        return actual == original
    return actual is original


def _primitive_original(value, depth=0):
    _require_original(depth <= 32, "malformed")
    if type(value) is tuple:
        return tuple(_primitive_original(item, depth + 1) for item in value)
    _require_original(type(value) in (str, int, type(None)), "malformed")
    return value


def _cut_original(value):
    _require_original(type(value) is CutIdentity, "malformed")
    try:
        result = (value.epoch, value.sequence, value.schema_manifest,
                  value.policy_revision, value.index_revision)
        _require_original(type(result[0]) is str and type(result[2]) is tuple
            and all(type(x) is int and x >= 0 for x in (result[1], result[3], result[4])), "malformed")
        return _primitive_original(result)
    except AttributeError:
        raise OriginalOutcomeError("malformed") from None


class _OpaqueOriginal:
    def __new__(cls, *args, **kwargs):
        raise TypeError("Original outcome handles are issued")


@dataclass(frozen=True, slots=True, init=False)
class OwnedSubmission(_OpaqueOriginal):
    handle: str
    _issuer: object


@dataclass(frozen=True, slots=True, init=False)
class IssuedSQLiteOutcome(_OpaqueOriginal):
    handle: str
    attempt: str
    _issuer: object


@dataclass(frozen=True, slots=True)
class SQLiteObservation:
    """DATA only. This is not a C1 receipt, release permit or SEND authority."""
    port_id: str
    worker_id: str
    runtime_epoch: str
    attempt: str
    operation_id: str
    transaction_id: str
    base: tuple
    target: tuple
    fingerprint: str
    kind: str
    durable_revision: int | None
    journal_revision: int | None
    reason: str


class _OriginalPort:
    def __init__(self, port, limit):
        self.weak = ref(port, lambda _, key=id(port): _ORIGINAL_PORTS.pop(key, None))
        self.owner, self.issuer, self.port_id = get_ident(), object(), uuid4().hex
        self.path, self.registries, self.epoch = port._path, port._registries, port._epoch
        self.queue, self.lock, self.hook = port._queue, port._lock, port._hook
        self.limit, self.thread, self.worker_id = limit, None, None
        self.database = None
        self.rows, self.transactions = {}, {}
        self.released = [None] * limit


_ORIGINAL_PORTS = {}


def _original_port(port, *, worker=False):
    _require_original(type(port) is SQLiteDurablePort, "foreign")
    record = _ORIGINAL_PORTS.get(id(port))
    _require_original(record is not None and record.weak() is port, "foreign")
    _require_original(get_ident() == (record.thread.ident if worker and record.thread is not None else record.owner),
                      "wrong_thread")
    _require_original(port._queue is record.queue and port._lock is record.lock
        and port._path is record.path and port._registries is record.registries
        and type(port._epoch) is str and port._epoch == record.epoch
        and port._thread is record.thread and port._hook is record.hook, "stale")
    return record


def _owned_row(record, token):
    row = record.rows.get(id(token))
    try:
        valid = (type(token) is OwnedSubmission and row is not None and row["token"] is token
            and _same_original((token.handle, token._issuer), row["signature"]))
    except AttributeError:
        valid = False
    _require_original(valid, "foreign")
    return row


def _original_bindings(submission, epoch):
    """O(number of snapshot maps), no graph traversal/encoding on host thread.

    Snapshot maps/Record values are C1 accepted immutable semantic storage.
    Capture their original bindings, not mutable envelope/identity DTO slots.
    Mutating private accepted Record/FrozenMap internals is outside this seam.
    """
    _require_original(type(submission) is DurableSubmission, "malformed")
    try:
        tx, op = submission.transaction_id, submission.operation_id
        _require_original(type(tx) is str and bool(tx) and type(op) is str and bool(op), "malformed")
        base, target = _cut_original(submission.base), _cut_original(submission.target)
        _require_original(base[0] == epoch and target[0] == epoch, "foreign")
        snapshot = submission.snapshot
        _require_original(type(snapshot) is SnapshotData, "malformed")
        identity = (snapshot.epoch, snapshot.sequence, snapshot.schema_manifest,
                    snapshot.policy_revision, snapshot.index_revision)
        _require_original(_same_original(identity, target), "malformed")
        maps = {name: getattr(snapshot, name) for name in MAPS}
        _require_original(all(type(value) is MappingProxyType for value in maps.values()), "malformed")
        bundle = submission.kernel_bundle
        _require_original(type(bundle) in (FrozenMap, FrozenList, tuple, str, int, bool, float, type(None)),
                          "malformed")
        private_snapshot = SnapshotData(target[0], target[1], **maps,
            schema_manifest=target[2], policy_revision=target[3], index_revision=target[4])
        return tx, op, base, target, private_snapshot, bundle
    except AttributeError:
        raise OriginalOutcomeError("malformed") from None


def _materialize_original(row):
    # Only the SQLite worker traverses/encodes. Canonical bytes survive UNKNOWN;
    # decode private DTOs from those same originals for commit AND readback.
    if row["encoded"] is None:
        tx, op, base, target, snapshot, bundle = row["bindings"]
        row["encoded"] = (tx, op, base, target, encode_snapshot(snapshot), encode_value(bundle))
        row["bindings"] = row["source"] = None
    tx, op, base, target, snapshot, bundle = row["encoded"]
    private = DurableSubmission(tx, op, CutIdentity(*base), CutIdentity(*target),
                                decode_snapshot(snapshot), decode_value(bundle))
    fingerprint = submission_fingerprint(private)
    if row["fingerprint"] is None:
        row["fingerprint"] = fingerprint
    _require_original(type(fingerprint) is str and fingerprint == row["fingerprint"], "stale")
    return private


def _publish_original(record, row, kind, revision, journal_revision, reason):
    _require_original(_original_port(record.weak(), worker=True) is record, "foreign")
    _require_original(record.thread is not None and get_ident() == record.thread.ident
                      and record.database is not None and record.database() is not None, "wrong_thread")
    proof = object.__new__(IssuedSQLiteOutcome)
    signature = (uuid4().hex, row["signature"][0], record.issuer)
    for name, value in zip(("handle", "attempt", "_issuer"), signature):
        object.__setattr__(proof, name, value)
    tx, op, base, target = row["header"]
    observation = (record.port_id, record.worker_id, record.epoch, row["signature"][0],
        op, tx, base, target, row["fingerprint"], kind, revision, journal_revision, reason)
    row["proof"], row["proof_signature"], row["observation"] = proof, signature, observation
    row["proof_future"] = None
    if kind != "unknown":
        # Keep compact originals until explicit receipt join/release, not a
        # full graph/history behind every completed observation.
        row["encoded"] = row["bindings"] = row["source"] = None
    return proof


def _verified_original(record, row, resolution):
    """Consume only the actual worker lookup result before observer exposure."""
    tx, op, base, target = row["header"]
    if (type(resolution) is not JournalResolution or type(resolution.outcome) is not DurableOutcome
            or type(resolution.expected_fingerprint) is not str
            or resolution.expected_fingerprint != row["fingerprint"]):
        return _publish_original(record, row, "unknown", None, None, "unverified_readback")
    outcome = resolution.outcome
    if (type(outcome.transaction_id) is not str or outcome.transaction_id != tx
            or not _same_original(_cut_original(outcome.target), target)):
        return _publish_original(record, row, "unknown", None, None, "unverified_readback")
    image = resolution.image
    if outcome.outcome is DBOutcome.UNKNOWN or type(image) is not BootstrapImage:
        return _publish_original(record, row, "unknown", None, None, "sql_outcome_unknown")
    _require_original(type(image.durable_revision) is int and image.durable_revision >= 0
                      and type(image.journal) is tuple, "malformed")
    matches = []
    for entry in image.journal:
        _require_original(type(entry) is CommittedBundle and type(entry.transaction_id) is str, "malformed")
        if entry.transaction_id == tx:
            matches.append(entry)
    if outcome.outcome is DBOutcome.ROLLED_BACK and not matches:
        return _publish_original(record, row, "absent", image.durable_revision, None, "verified_absence")
    if outcome.outcome is DBOutcome.COMMITTED and len(matches) == 1:
        entry = matches[0]
        if (type(entry.operation_id) is str and entry.operation_id == op
                and type(entry.fingerprint) is str and entry.fingerprint == row["fingerprint"]
                and _same_original(_cut_original(entry.base), base)
                and _same_original(_cut_original(entry.target), target)
                and type(entry.durable_revision) is int
                and 0 < entry.durable_revision <= image.durable_revision):
            return _publish_original(record, row, "committed", image.durable_revision,
                                     entry.durable_revision, "verified_journal")
    return _publish_original(record, row, "unknown", None, None, "unverified_readback")


class SQLiteDurablePort:
    def __init__(self, path, registries, *, runtime_epoch, outcome_sink=None, hook=None, queue_limit=32, owned_limit=None):
        if type(queue_limit) is not int or queue_limit < 1:
            raise ValueError("Positive queue limit required")
        if type(runtime_epoch) is not str or not runtime_epoch:
            raise ValueError("Runtime epoch required")
        for callback in (outcome_sink, hook):
            if callback is not None and (not callable(callback) or iscoroutinefunction(callback)):
                raise TypeError("Use synchronous trusted worker callbacks")
        self._path, self._registries = path, tuple(registries)
        self._epoch, self._hook = runtime_epoch, hook
        self._sink = outcome_sink
        self._queue = Queue(queue_limit)
        self._lock = Lock()
        self._started = self._ready = self._closing = self._failed = False
        self._start_result, self._close_result = Future(), Future()
        self._thread = None
        # One latest immutable delivery per outstanding coordinator operation.
        # The definitive journal is authoritative if the host sink fails/drops Ack.
        self._last_delivery = None
        self._accepted_submission = None
        if owned_limit is None:
            owned_limit = min(queue_limit, 32)
        if type(owned_limit) is not int or not 1 <= owned_limit <= 32:
            raise ValueError("Owned attempt limit must be an exact integer in 1..32")
        _ORIGINAL_PORTS[id(self)] = _OriginalPort(self, owned_limit)


    def reserve_owned(self):
        """Reserve finite observation bookkeeping before enqueue/SQL; no I/O proof."""
        record = _original_port(self)
        with record.lock:
            _require_original(not self._closing and not self._failed, "closed")
            used = {row["slot"] for row in record.rows.values()}
            slot = next((index for index in range(record.limit) if index not in used), None)
            _require_original(slot is not None, "capacity")
            token = object.__new__(OwnedSubmission)
            signature = (uuid4().hex, record.issuer)
            for name, value in zip(("handle", "_issuer"), signature):
                object.__setattr__(token, name, value)
            record.rows[id(token)] = {"token": token, "signature": signature, "slot": slot,
                "source": None, "bindings": None, "header": None, "encoded": None,
                "fingerprint": None, "state": "reserved", "reason": None,
                "future": None, "proof_future": None, "lookup_pending": False, "proof": None,
                "proof_signature": None, "observation": None}
        return token

    def register_owned(self, submission, *, reservation=None):
        """Pin fixed original bindings without traversing/serializing the graph.

        C1 accepted semantic maps/values remain immutable. Caller-visible
        submission/snapshot/cut slots are not later sources of originals.
        """
        record = _original_port(self)
        token = self.reserve_owned() if reservation is None else reservation
        try:
            bindings = _original_bindings(submission, record.epoch)
            with record.lock:
                row = _owned_row(record, token)
                _require_original(row["state"] == "reserved", "already_registered")
                tx = bindings[0]
                _require_original(tx not in record.transactions and not any(
                    old is not None and old["transaction"] == tx for old in record.released), "collision")
                row.update(source=submission, bindings=bindings, header=bindings[:4], state="registered")
                record.transactions[tx] = row
            return token
        except BaseException:
            if reservation is None:
                with record.lock:
                    record.rows.pop(id(token), None)
            raise

    def submit_owned(self, attempt):
        """Exactly one enqueue attempt. NOT_SUBMITTED is a real original branch."""
        record = _original_port(self)
        with record.lock:
            row = _owned_row(record, attempt)
            _require_original(row["state"] == "registered", "already_submitted")
            reason = ("not_ready" if not self._ready else "closing" if self._closing
                      else "worker_failed" if self._failed else "missing_sink" if self._sink is None else None)
            row["state"] = "submitted"
            if reason is None:
                try:
                    record.queue.put_nowait(("owned_submit", row, None))
                except Full:
                    reason = "queue_full"
            if reason is not None:
                row.update(state="not_submitted", reason=reason)
                return SubmitDisposition.NOT_SUBMITTED
            return SubmitDisposition.ACCEPTED

    def lookup_owned(self, attempt):
        """One pending FIFO lookup; Future is a notification, never authority.

        Explicit retry after UNKNOWN/error is a new readback, never a submit.
        A cancelled/poisoned Future cannot replace the independently pinned proof.
        """
        record = _original_port(self)
        immediate = error = None
        with record.lock:
            row = _owned_row(record, attempt)
            _require_original(row["state"] not in ("reserved", "registered"), "not_submitted")
            if row["lookup_pending"]:
                return row["future"]
            if row["observation"] is not None and row["observation"][9] != "unknown":
                old = row["proof_future"]
                if old is not None and not old.cancelled():
                    return old
                future = Future()
                row["future"] = future
                immediate = row["proof"]
            else:
                future = Future()
                row["future"] = future
                if not self._ready or self._closing or self._failed:
                    error = OriginalOutcomeError("unavailable")
                else:
                    try:
                        record.queue.put_nowait(("owned_lookup", row, future))
                        row["lookup_pending"] = True
                    except Full:
                        error = OriginalOutcomeError("queue_full")
        if error is not None:
            _resolve(future, error=error)
        elif immediate is not None:
            if _resolve(future, immediate):
                with record.lock:
                    row["proof_future"] = future
        return future

    def owned_outcome(self, attempt):
        """Read registry-issued actual observation; do not trust Future payload."""
        record = _original_port(self)
        with record.lock:
            return _owned_row(record, attempt)["proof"]

    def validate_owned_outcome(self, attempt, proof):
        """Return detached original I/O DATA, not C1 ownership or a release permit."""
        record = _original_port(self)
        with record.lock:
            row = _owned_row(record, attempt)
            try:
                valid = (type(proof) is IssuedSQLiteOutcome and row["proof"] is proof
                    and _same_original((proof.handle, proof.attempt, proof._issuer), row["proof_signature"]))
            except AttributeError:
                valid = False
            _require_original(valid, "foreign")
            return SQLiteObservation(*row["observation"])

    def release_owned(self, attempt, proof=None):
        """Retire only observation bookkeeping, never worker/C1 accounting.

        Empty bookkeeping credit may be returned without proof. Registered
        attempts require the current definitive original observation and no
        pending readback. UNKNOWN remains retained, including after close.
        """
        record = _original_port(self)
        with record.lock:
            if id(attempt) not in record.rows:
                for old in record.released:
                    if old is not None and old["attempt"] is attempt and old["proof"] is proof:
                        _require_original(_same_original((attempt.handle, attempt._issuer), old["signature"]),
                                          "foreign")
                        if proof is not None:
                            _require_original(_same_original((proof.handle, proof.attempt, proof._issuer),
                                                             old["proof_signature"]), "foreign")
                        return False
                raise OriginalOutcomeError("foreign")
            row = _owned_row(record, attempt)
            _require_original(not row["lookup_pending"], "pending")
            if row["state"] != "reserved":
                try:
                    valid = (type(proof) is IssuedSQLiteOutcome and proof is row["proof"]
                        and _same_original((proof.handle, proof.attempt, proof._issuer), row["proof_signature"]))
                except AttributeError:
                    valid = False
                _require_original(valid, "foreign")
                _require_original(row["observation"][9] in ("committed", "absent", "not_submitted"), "unresolved")
            else:
                _require_original(proof is None, "foreign")
            record.released[row["slot"]] = {"attempt": attempt, "proof": proof,
                "signature": row["signature"], "proof_signature": row["proof_signature"],
                "transaction": None if row["header"] is None else row["header"][0]}
            if row["header"] is not None:
                record.transactions.pop(row["header"][0], None)
            record.rows.pop(id(attempt))
        return True

    def _owned_execute(self, database, record, row, *, lookup):
        """Worker-private actual source branch; publish originals before observers."""
        _require_original(_original_port(self, worker=True) is record
            and type(database) is SQLiteDatabase and record.database is not None
            and record.database() is database, "foreign")
        if lookup and row["observation"] is not None and row["observation"][9] != "unknown":
            return row["proof"], None
        if row["encoded"] is None:
            self._hit("before_owned_encode", row["header"][1])
        private = _materialize_original(row)
        outcome = None
        if not lookup:
            outcome = database.commit(private)
        if row["state"] == "not_submitted":
            with record.lock:
                proof = _publish_original(record, row, "not_submitted", None, None, row["reason"])
        else:
            resolution = database.lookup(private)
            with record.lock:
                proof = _verified_original(record, row, resolution)
                row["state"] = "done"
        return proof, outcome

    def bind_outcome_sink(self, sink):
        if not callable(sink) or iscoroutinefunction(sink):
            raise TypeError("Immutable outcome sink required")
        with self._lock:
            if self._sink is not None:
                raise RuntimeError("Outcome sink is already bound")
            self._sink = sink

    def start(self):
        with self._lock:
            if self._closing and not self._started:
                _resolve(self._start_result, error=RuntimeError("Worker was closed before start"))
                return self._start_result
            if not self._started:
                self._started = True
                self._thread = Thread(target=self._run, name="agent-native-sqlite", daemon=True)
                _ORIGINAL_PORTS[id(self)].thread = self._thread
                self._thread.start()
        return self._start_result

    def submit(self, submission):
        if type(submission) is not DurableSubmission:
            raise TypeError("Typed immutable durable submission required")
        tx = submission.transaction_id
        if type(tx) is not str:
            raise TypeError("Exact transaction identity required")
        original = _ORIGINAL_PORTS[id(self)]
        with self._lock:
            if tx in original.transactions or any(
                    old is not None and old["transaction"] == tx for old in original.released):
                raise OriginalOutcomeError("tracked_attempt")
            if not self._ready or self._closing or self._failed or self._sink is None:
                return SubmitDisposition.NOT_SUBMITTED
            try:
                self._queue.put_nowait(("submit", submission, None))
                self._accepted_submission = submission
            except Full:
                return SubmitDisposition.NOT_SUBMITTED
        return SubmitDisposition.ACCEPTED

    def accepted_submission(self, transaction_id):
        """Read-only original envelope for lost Ack while C2 still awaits its outcome.

        One coordinator has one outstanding durable operation. This is a retained
        immutable reference, not a graph copy, writer grant or a journal proof.
        """
        if type(transaction_id) is not str or not transaction_id:
            raise TypeError("Transaction identity required")
        with self._lock:
            submission = self._accepted_submission
            if submission is None or submission.transaction_id != transaction_id:
                raise LookupError("No retained accepted submission for this transaction")
            return submission

    def _request(self, name, value=None):
        result = Future()
        with self._lock:
            if not self._ready or self._closing or self._failed:
                _resolve(result, error=RuntimeError("Database worker is not ready"))
            else:
                try:
                    self._queue.put_nowait((name, value, result))
                except Full:
                    _resolve(result, error=RuntimeError("Database request queue is full"))
        return result

    def lookup(self, submission):
        # Same FIFO as submit. Full content correlation prevents same-cut kernel collisions.
        if type(submission) is not DurableSubmission:
            raise TypeError("Lookup requires the full immutable expected submission")
        return self._request("lookup", submission)

    def journal(self):
        return self._request("journal")

    def load(self):
        return self._request("load")

    def close(self):
        with self._lock:
            self._closing = True
            if not self._started:
                _resolve(self._close_result, ShutdownResult(True, "never_started"))
        return self._close_result

    def _hit(self, name, operation_id):
        if self._hook is not None:
            _sync_result(self._hook(name, operation_id))

    def _deliver(self, outcome, operation_id):
        with self._lock:
            self._last_delivery = outcome
        self._hit("before_commit_ack", operation_id)
        # Sink only offers immutable mailbox data. GUI schedules owner pump itself.
        _sync_result(self._sink(outcome))
        self._hit("after_commit_ack", operation_id)

    def _run(self):
        database = None
        try:
            # Ordinary subclass ports retain their existing untracked API.
            # Owned issuance remains exact-type and cannot be inherited.
            original = (_original_port(self, worker=True) if type(self) is SQLiteDurablePort
                        else _ORIGINAL_PORTS[id(self)])
            database = SQLiteDatabase(original.path, original.registries, hook=original.hook)
            database.bind_runtime(original.epoch)
            image = database.load()
            original.database = ref(database)
            original.worker_id = uuid4().hex
            with self._lock:
                self._ready = True
            _resolve(self._start_result, image)
            unresolved = set()
            while True:
                with self._lock:
                    closing = self._closing
                if closing and self._queue.empty():
                    break
                try:
                    name, value, result = self._queue.get(timeout=0.025)
                except Empty:
                    continue
                try:
                    if name in ("owned_submit", "owned_lookup"):
                        row = value
                        with original.lock:
                            active = original.rows.get(id(row["token"])) is row
                        if not active:
                            if result is not None:
                                _resolve(result, error=OriginalOutcomeError("foreign"))
                            continue
                        try:
                            proof, owned_delivery = self._owned_execute(database, original, row,
                                lookup=name == "owned_lookup")
                            with original.lock:
                                observation = row["observation"]
                                row["lookup_pending"] = False if name == "owned_lookup" else row["lookup_pending"]
                            # Attempt identity is stable even if encoding failed
                            # before the first full fingerprint was available.
                            identity = ("owned", original.port_id, row["signature"][0])
                            if observation[9] == "unknown":
                                unresolved.add(identity)
                            else:
                                unresolved.discard(identity)
                            if result is not None and _resolve(result, proof):
                                with original.lock:
                                    row["proof_future"] = result
                            if owned_delivery is not None:
                                try:
                                    self._deliver(owned_delivery, row["header"][1])
                                except Exception:
                                    pass
                        except Exception:
                            with original.lock:
                                row["lookup_pending"] = False if name == "owned_lookup" else row["lookup_pending"]
                            unresolved.add(("owned", original.port_id, row["signature"][0]))
                            if result is not None:
                                _resolve(result, error=OriginalOutcomeError("unavailable"))
                    elif name == "submit":
                        outcome = database.commit(value)
                        if outcome.outcome == DBOutcome.UNKNOWN:
                            unresolved.add(_request_identity(value))
                        else:
                            unresolved.discard(_request_identity(value))
                        try:
                            self._deliver(outcome, value.operation_id)
                        except Exception:
                            # Delivery failure is not a rollback; journal lookup remains available.
                            pass
                    elif name == "lookup":
                        resolution = database.lookup(value)
                        if resolution.outcome.outcome != DBOutcome.UNKNOWN:
                            unresolved.discard((value.transaction_id, resolution.expected_fingerprint))
                        _resolve(result, resolution)
                    elif name == "load":
                        _resolve(result, database.load())
                    elif name == "journal":
                        _resolve(result, database.journal())
                    else:
                        raise RuntimeError("Unknown internal worker message")
                except Exception as error:
                    if result is not None:
                        _resolve(result, error=error)
                    else:
                        unresolved.add(_request_identity(value))
                        self._deliver(DurableOutcome(value.transaction_id, value.target,
                                      DBOutcome.UNKNOWN, "worker_operation_failed"), value.operation_id)
                finally:
                    self._queue.task_done()
            database.close()
            database = None
            _resolve(self._close_result, ShutdownResult(not unresolved, "unknown_transactions" if unresolved else None))
        except BaseException as error:
            with self._lock:
                self._failed = True
            if not self._start_result.done():
                _resolve(self._start_result, error=error)
            if database is not None:
                try:
                    database.close()
                except Exception:
                    pass
            if not self._close_result.done():
                _resolve(self._close_result, ShutdownResult(False, type(error).__name__))
            while True:
                try:
                    name, value, result = self._queue.get_nowait()
                except Empty:
                    break
                if name == "owned_lookup":
                    with self._lock:
                        value["lookup_pending"] = False
                if result is not None and not result.done():
                    _resolve(result, error=RuntimeError("Database worker terminated"))
                self._queue.task_done()
        finally:
            with self._lock:
                self._ready = False
