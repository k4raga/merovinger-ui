"""Single worker-owned SQLite connection and atomic accepted bundle journal."""
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from threading import get_ident
from types import MappingProxyType

from agent_native.kernel import CutIdentity, DurableSubmission, DurableOutcome, DBOutcome
from agent_native.state import SnapshotData
from .codec import (MAPS, StorageFormatError, check_snapshot, _checked_snapshot_bytes, durable_projection, encode_snapshot,
                    encode_value, decode_value, encode_cut, decode_cut, digest)

APPLICATION_ID = 0x41474E31
LAYOUT_VERSION = 2
MIGRATIONS = (
    ("CREATE TABLE storage_meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, cut BLOB, snapshot_sha TEXT, txid TEXT)",
     "INSERT INTO storage_meta VALUES (1,0,NULL,NULL,NULL)",
     "CREATE TABLE graph (name TEXT NOT NULL, key BLOB NOT NULL, value BLOB NOT NULL, PRIMARY KEY(name,key)) WITHOUT ROWID",
     "CREATE TABLE transactions (txid TEXT PRIMARY KEY, operation_id TEXT NOT NULL, base BLOB NOT NULL, target BLOB NOT NULL, fingerprint TEXT NOT NULL, snapshot_sha TEXT NOT NULL, bundle_sha TEXT NOT NULL, revision INTEGER NOT NULL UNIQUE)",
     "CREATE TABLE storage_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"),
    ("CREATE TABLE kernel_bundles (txid TEXT PRIMARY KEY REFERENCES transactions(txid), format TEXT NOT NULL, payload BLOB NOT NULL)",
     "INSERT INTO kernel_bundles SELECT txid, 'agent-native.kernel-bundle/1', X'5b226e225d' FROM transactions"),
)


class OwnershipUnavailable(RuntimeError):
    pass


class StaleSubmission(StorageFormatError):
    pass


@dataclass(frozen=True, slots=True)
class CommittedBundle:
    transaction_id: str
    operation_id: str
    base: CutIdentity
    target: CutIdentity
    fingerprint: str
    durable_revision: int
    format: str
    payload: object


@dataclass(frozen=True, slots=True)
class BootstrapImage:
    snapshot: SnapshotData | None
    durable_revision: int
    last_transaction_id: str | None
    journal: tuple[CommittedBundle, ...] = ()


@dataclass(frozen=True, slots=True)
class JournalResolution:
    outcome: DurableOutcome
    image: BootstrapImage | None
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    resolved: bool
    detail: str | None = None


def _fingerprint(submission, snapshot_sha, bundle_sha):
    return digest(encode_value((submission.transaction_id, submission.operation_id,
                  decode_value(encode_cut(submission.base)), decode_value(encode_cut(submission.target)),
                  snapshot_sha, bundle_sha)))


def submission_fingerprint(submission):
    if type(submission) is not DurableSubmission:
        raise TypeError("Expected the full immutable durable submission")
    return _fingerprint(submission, digest(encode_snapshot(durable_projection(submission.snapshot))),
                        digest(encode_value(submission.kernel_bundle)))


class SQLiteDatabase:
    """Worker implementation detail. Never exposes its connection to the host.

    No author callbacks, domain handlers, effect network or event-loop pumping.
    All public methods reject another thread, even when SQLite is serialized.
    """
    def __init__(self, path, registries, *, hook=None, timeout=0.15):
        self._owner = get_ident()
        self._registries = tuple(registries)
        self._hook = hook
        self._runtime_epoch = None
        self._revision = 0
        self._connection = None
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Use an absolute project-owned DB path")
        try:
            self._connection = sqlite3.connect(path, autocommit=True, check_same_thread=True, timeout=timeout)
            c = self._connection
            c.execute("PRAGMA locking_mode=EXCLUSIVE")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            appid = c.execute("PRAGMA application_id").fetchone()[0]
            tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version > LAYOUT_VERSION or version < 0:
                raise StorageFormatError("Unsupported storage layout version")
            if appid != APPLICATION_ID and (appid != 0 or tables or version):
                raise StorageFormatError("Database is not an Agent Native storage file")
            if c.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise StorageFormatError("DELETE journal mode unavailable")
            c.execute("PRAGMA synchronous=EXTRA")
            c.execute("PRAGMA foreign_keys=ON")
            if (c.execute("PRAGMA synchronous").fetchone()[0] != 3 or
                    c.execute("PRAGMA locking_mode").fetchone()[0] != "exclusive" or
                    c.execute("PRAGMA foreign_keys").fetchone()[0] != 1):
                raise StorageFormatError("Required SQLite configuration unavailable")
            c.execute("BEGIN EXCLUSIVE")
            if version:
                history = dict(c.execute("SELECT version,checksum FROM storage_migrations"))
                expected = {v: digest(encode_value(MIGRATIONS[v-1])) for v in range(1, version+1)}
                if history != expected:
                    raise StorageFormatError("Storage migration history mismatch")
            for next_version in range(version + 1, LAYOUT_VERSION + 1):
                for statement in MIGRATIONS[next_version - 1]:
                    c.execute(statement)
                c.execute("INSERT INTO storage_migrations VALUES (?,?)",
                          (next_version, digest(encode_value(MIGRATIONS[next_version-1]))))
                c.execute(f"PRAGMA user_version={next_version}")
            c.execute(f"PRAGMA application_id={APPLICATION_ID}")
            c.execute("UPDATE storage_meta SET revision=revision WHERE id=1")
            self._hit("before_sql_commit", "storage-migration")
            c.execute("COMMIT")
            self._adopt_verified_image(self.load())
        except sqlite3.OperationalError as error:
            self._close_failed()
            if "locked" in str(error).lower():
                raise OwnershipUnavailable("Another process owns this database") from error
            raise
        except BaseException:
            self._close_failed()
            raise

    def _check(self):
        if get_ident() != self._owner:
            raise RuntimeError("SQLite connection belongs only to its worker thread")
        if self._connection is None:
            raise RuntimeError("Database is closed")

    def _hit(self, name, operation_id):
        if self._hook is not None:
            self._hook(name, operation_id)

    def _close_failed(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def bind_runtime(self, epoch):
        self._check()
        if type(epoch) is not str or not epoch:
            raise ValueError("A nonempty runtime epoch is required")
        if self._runtime_epoch is not None and self._runtime_epoch != epoch:
            raise RuntimeError("One coordinator runtime per database owner")
        self._runtime_epoch = epoch

    def _read_check(self):
        self._check()
        if self._connection.in_transaction:
            raise StorageFormatError("Unresolved SQLite transaction; no committed read is available")

    def load(self):
        self._read_check()
        c = self._connection
        revision, cut_bytes, snapshot_sha, txid = c.execute(
            "SELECT revision,cut,snapshot_sha,txid FROM storage_meta WHERE id=1").fetchone()
        if cut_bytes is None:
            if (revision or txid is not None or c.execute("SELECT count(*) FROM graph").fetchone()[0]
                    or c.execute("SELECT count(*) FROM transactions").fetchone()[0]
                    or c.execute("SELECT count(*) FROM kernel_bundles").fetchone()[0]):
                raise StorageFormatError("Contradictory empty database metadata")
            return BootstrapImage(None, 0, None)
        cut = decode_cut(cut_bytes)
        maps = {name: {} for name in MAPS}
        for name, key_bytes, value_bytes in c.execute("SELECT name,key,value FROM graph"):
            if name not in maps:
                raise StorageFormatError("Unknown graph map")
            key, value = decode_value(key_bytes), decode_value(value_bytes)
            if key in maps[name]:
                raise StorageFormatError("Duplicate decoded graph identity")
            maps[name][key] = value
        snapshot = SnapshotData(cut.epoch, cut.sequence, **{name: MappingProxyType(value) for name,value in maps.items()},
                                schema_manifest=cut.schema_manifest, policy_revision=cut.policy_revision,
                                index_revision=cut.index_revision)
        snapshot_bytes = _checked_snapshot_bytes(snapshot, self._registries)
        if (digest(snapshot_bytes) != snapshot_sha or
                any(p != "durable" for p in snapshot.generation_partitions.values())):
            raise StorageFormatError("Durable graph digest/partition mismatch")
        row = c.execute("SELECT target,snapshot_sha,revision FROM transactions WHERE txid=?", (txid,)).fetchone()
        if row != (cut_bytes, snapshot_sha, revision):
            raise StorageFormatError("Head journal mismatch")
        self._verify_journal(txid)
        return BootstrapImage(snapshot, revision, txid, self.journal())

    def _verify_journal(self, txid):
        self._read_check()
        c = self._connection
        row = c.execute("SELECT operation_id,base,target,fingerprint,snapshot_sha,bundle_sha FROM transactions WHERE txid=?", (txid,)).fetchone()
        if row is None:
            return None
        operation, base, target, fingerprint, snapshot_sha, bundle_sha = row
        bundle = c.execute("SELECT format,payload FROM kernel_bundles WHERE txid=?", (txid,)).fetchone()
        if bundle is None or bundle[0] != "agent-native.kernel-bundle/1" or digest(bundle[1]) != bundle_sha:
            raise StorageFormatError("Kernel envelope/journal mismatch")
        decode_value(bundle[1])
        actual = digest(encode_value((txid, operation, decode_value(base), decode_value(target), snapshot_sha, bundle_sha)))
        if actual != fingerprint:
            raise StorageFormatError("Transaction fingerprint mismatch")
        return decode_cut(target), fingerprint

    def _adopt_verified_image(self, image):
        """Advance the worker baseline only after a full consistent durable read.

        The image is the current DB head, even when the definitive journal entry
        being resolved belongs to an older transaction. Unknown/collision/read
        failures never install partial or historical metadata as a new baseline.
        """
        self._image, self._revision = image, image.durable_revision

    def _fresh(self, submission, projected, previous):
        if self._runtime_epoch is None or submission.base.epoch != self._runtime_epoch:
            raise StaleSubmission("Submission is not from the bound coordinator runtime")
        if previous is None:
            # A fresh DB can be seeded from a real already-published C1 cut.
            return
        if (submission.base.sequence < previous.sequence or
                projected.schema_manifest != previous.schema_manifest or
                submission.base.policy_revision < previous.policy_revision):
            raise StaleSubmission("Stale durable cut")
        sequence = projected.sequence
        for key, old in previous.records.items():
            new = projected.records.get(key)
            if new is None:
                if projected.existence_versions.get(key) != sequence:
                    raise StaleSubmission("Durable deletion lacks a fresh tombstone")
            elif encode_value(new) != encode_value(old) and new.version != sequence:
                raise StaleSubmission("Stale durable record overwrite")
        for key, new in projected.records.items():
            if key not in previous.records and new.version != sequence:
                raise StaleSubmission("Stale durable record insertion")
        for key, old in previous.generations.items():
            if (key not in projected.generations or projected.generations[key] < old or
                    projected.generations[key] > old + 1):
                raise StaleSubmission("Generation ledger regression/jump")
        for name in ("existence_versions", "field_versions", "collection_versions"):
            before, after = getattr(previous, name), getattr(projected, name)
            for key, old in before.items():
                new = after.get(key)
                owner = key if name == "existence_versions" else key[0]
                if new is None and name == "collection_versions" and owner in projected.records and projected.records[owner].version == sequence:
                    continue
                if new is None or (new != old and new != sequence):
                    raise StaleSubmission("Historical durable version lost/regressed")
            for key, new in after.items():
                if key not in before and new != sequence:
                    raise StaleSubmission("Historical durable version inserted stale")

    def commit(self, submission):
        self._check()
        if type(submission) is not DurableSubmission:
            raise TypeError("Typed durable submission required")
        c = self._connection
        try:
            old = self._verify_journal(submission.transaction_id)
        except Exception:
            return DurableOutcome(submission.transaction_id, submission.target, DBOutcome.UNKNOWN, "journal_unavailable")
        try:
            check_snapshot(submission.snapshot, self._registries)
            projected = durable_projection(submission.snapshot)
            snapshot_bytes = encode_snapshot(projected)
            bundle = encode_value(submission.kernel_bundle)
            snapshot_sha, bundle_sha = digest(snapshot_bytes), digest(bundle)
            fingerprint = _fingerprint(submission, snapshot_sha, bundle_sha)
            if old is not None:
                if old != (submission.target, fingerprint):
                    return DurableOutcome(submission.transaction_id, submission.target, DBOutcome.UNKNOWN, "transaction_id_collision")
                self._adopt_verified_image(self.load())
                return DurableOutcome(submission.transaction_id, submission.target, DBOutcome.COMMITTED, "duplicate_transaction")
            self._fresh(submission, projected, self._image.snapshot)
        except Exception as error:
            return DurableOutcome(submission.transaction_id, submission.target,
                                  DBOutcome.UNKNOWN if old is not None else DBOutcome.ROLLED_BACK,
                                  "preflight:" + type(error).__name__)
        try:
            self._hit("before_sql_begin", submission.operation_id)
            c.execute("BEGIN IMMEDIATE")
            actual = c.execute("SELECT revision FROM storage_meta WHERE id=1").fetchone()[0]
            if actual != self._revision:
                raise StaleSubmission("Durable revision fence changed")
            c.execute("DELETE FROM graph")
            for name in MAPS:
                for key, value in getattr(projected, name).items():
                    c.execute("INSERT INTO graph VALUES (?,?,?)", (name, encode_value(key), encode_value(value)))
            next_revision = actual + 1
            c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (
                submission.transaction_id, submission.operation_id, encode_cut(submission.base), encode_cut(submission.target),
                fingerprint, snapshot_sha, bundle_sha, next_revision))
            c.execute("INSERT INTO kernel_bundles VALUES (?,?,?)", (submission.transaction_id,
                      "agent-native.kernel-bundle/1", bundle))
            c.execute("UPDATE storage_meta SET revision=?,cut=?,snapshot_sha=?,txid=? WHERE id=1 AND revision=?", (
                next_revision, encode_cut(submission.target), snapshot_sha, submission.transaction_id, actual))
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                raise StaleSubmission("Durable revision CAS failed")
            self._hit("before_sql_commit", submission.operation_id)
            c.execute("COMMIT")
            self._adopt_verified_image(self.load())
            self._hit("after_sql_commit", submission.operation_id)
            return DurableOutcome(submission.transaction_id, submission.target, DBOutcome.COMMITTED)
        except Exception as error:
            try:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                # Serialized terminal readback is authoritative even after COMMIT raised.
                found = self._verify_journal(submission.transaction_id)
                if found is not None:
                    if found != (submission.target, fingerprint):
                        raise StorageFormatError("Conflicting journal after SQL error")
                    self._adopt_verified_image(self.load())
                    return DurableOutcome(submission.transaction_id, submission.target, DBOutcome.COMMITTED, "readback_after:" + type(error).__name__)
                self._adopt_verified_image(self.load())
                return DurableOutcome(submission.transaction_id, submission.target, DBOutcome.ROLLED_BACK, "rollback:" + type(error).__name__)
            except Exception:
                return DurableOutcome(submission.transaction_id, submission.target, DBOutcome.UNKNOWN, "sql_outcome_unavailable")

    def lookup(self, submission):
        self._check()
        if type(submission) is not DurableSubmission:
            raise TypeError("Lookup requires the full immutable expected submission")
        transaction_id, target = submission.transaction_id, submission.target
        expected = None
        try:
            # A failed rollback may have left our own uncommitted journal visible.
            # It must never be mistaken for COMMITTED evidence.
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            self._read_check()
            expected = submission_fingerprint(submission)
            found = self._verify_journal(transaction_id)
            if found is not None and found != (target, expected):
                return JournalResolution(DurableOutcome(transaction_id, target, DBOutcome.UNKNOWN,
                                         "submission_fingerprint_collision"), None, expected)
            image = self.load()
            self._adopt_verified_image(image)
            outcome = DBOutcome.COMMITTED if found is not None else DBOutcome.ROLLED_BACK
            return JournalResolution(DurableOutcome(transaction_id, target, outcome), image, expected)
        except Exception as error:
            return JournalResolution(DurableOutcome(transaction_id, target, DBOutcome.UNKNOWN,
                                     type(error).__name__), None, expected)

    def journal(self):
        self._read_check()
        result = []
        for txid, operation, base, target, fingerprint, revision in self._connection.execute(
                "SELECT txid,operation_id,base,target,fingerprint,revision FROM transactions ORDER BY revision").fetchall():
            self._verify_journal(txid)
            fmt, payload = self._connection.execute("SELECT format,payload FROM kernel_bundles WHERE txid=?", (txid,)).fetchone()
            result.append(CommittedBundle(txid, operation, decode_cut(base), decode_cut(target),
                                          fingerprint, revision, fmt, decode_value(payload)))
        return tuple(result)

    def kernel_bundle(self, transaction_id):
        self._check()
        self._verify_journal(transaction_id)
        row = self._connection.execute("SELECT payload FROM kernel_bundles WHERE txid=?", (transaction_id,)).fetchone()
        return decode_value(row[0]) if row is not None else None

    def close(self):
        self._check()
        self._connection.close()
        self._connection = None
        return ShutdownResult(True)
