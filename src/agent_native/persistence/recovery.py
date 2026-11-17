"""Owner-thread restore orchestration. No handler replay or private coordinator access."""
from uuid import uuid4

from agent_native.kernel import DBOutcome, CutIdentity
from agent_native.state import StateStore
from agent_native.state.derived import DerivedCache
from .codec import durable_projection, encode_snapshot, StorageFormatError
from .storage import BootstrapImage, JournalResolution, submission_fingerprint


def bootstrap_pair(registries, image, *, definitions=(), epoch=None, limits=None):
    if type(image) is not BootstrapImage:
        raise TypeError("Typed bootstrap image required")
    epoch = uuid4().hex if epoch is None else epoch
    if image.snapshot is None:
        if image.durable_revision or image.last_transaction_id is not None or image.journal:
            raise StorageFormatError("Contradictory empty bootstrap image")
        store = StateStore(registries, epoch=epoch, limits=limits)
    else:
        if epoch == image.snapshot.epoch:
            raise ValueError("Restart requires a fresh epoch")
        if encode_snapshot(durable_projection(image.snapshot)) != encode_snapshot(image.snapshot):
            raise StorageFormatError("Restart image must be durable-only")
        store = StateStore.from_snapshot(registries, image.snapshot, epoch=epoch, limits=limits)
    with store.read() as view:
        cache = DerivedCache.rebuild(view, definitions)
    return store, cache


def rebuild_pending(coordinator, registries, resolution=None, *, limits=None):
    handoff = coordinator.recovery_handoff()
    outcome = None
    if handoff.transaction_id is not None:
        if type(resolution) is not JournalResolution:
            raise TypeError("Definitive journal resolution required")
        outcome = resolution.outcome
        if (outcome.outcome != DBOutcome.COMMITTED or outcome.transaction_id != handoff.transaction_id
                or outcome.target != handoff.target or resolution.image is None
                or resolution.image.snapshot is None):
            raise StorageFormatError("No correlated committed recovery proof")
        expected_fingerprint = submission_fingerprint(handoff.submission)
        if resolution.expected_fingerprint != expected_fingerprint:
            raise StorageFormatError("Recovery proof is not for the full retained submission")
        actual = resolution.image.snapshot
        if (CutIdentity.read(actual) != handoff.target or
                encode_snapshot(durable_projection(handoff.target_snapshot)) != encode_snapshot(actual)):
            raise StorageFormatError("Durable database does not prove the retained RAM target")
        journal = [entry for entry in resolution.image.journal if entry.transaction_id == handoff.transaction_id]
        if (len(journal) != 1 or journal[0].target != handoff.target
                or resolution.image.last_transaction_id != handoff.transaction_id
                or journal[0].durable_revision != resolution.image.durable_revision
                or journal[0].fingerprint != expected_fingerprint):
            raise StorageFormatError("Recovery journal proof is missing")
    elif resolution is not None:
        raise TypeError("Memory-only rebuild has no DB resolution")
    store = StateStore.from_snapshot(registries, handoff.target_snapshot, limits=limits)
    with store.read() as view:
        cache = DerivedCache.rebuild(view, handoff.definitions)
    coordinator.install_rebuilt(store, cache, outcome=outcome)
    return store, cache
