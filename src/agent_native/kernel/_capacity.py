"""One staged durable-capacity predicate; no store, serializer or new writer."""
from ._input_lineage import native_bytes

RESPONSE_RESERVE = 192 * 1024
RESPONSE_EVENT_RESERVE = 1
MINIMUM_TERMINAL_RESERVE = 8192


def reservation_events(definitions, reservation):
    """Slots for the one mandatory accepted response, held with its byte reserve.

    Native row shape stays unchanged: the SDK owner type fixes its closed future
    topology. UNKNOWN is not release; only the original row's released state
    consumes this claim. Session terminal disposition itself adds no history event.
    """
    if reservation["state"] != "reserved":
        return 0
    typ = reservation["owner"]["entity_type"]
    if typ == definitions.EffectDispatch.__entity_type__:
        return 2
    if typ == definitions.ExternalFact.__entity_type__:
        return 1
    return RESPONSE_EVENT_RESERVE if typ == definitions.ModelRequest.__entity_type__ else 0


def reservation_commits(definitions, reservation):
    """Finite sequence space is reserved alongside bytes, never refunded on UNKNOWN.

    Every original dispatch can require retain then apply; a retained Fact needs
    only apply. Ordinary commits must preserve these successor slots too.
    """
    if reservation["state"] != "reserved":
        return 0
    typ = reservation["owner"]["entity_type"]
    return 2 if typ == definitions.EffectDispatch.__entity_type__ else (
        1 if typ in (definitions.ExternalFact.__entity_type__, definitions.ModelRequest.__entity_type__,
                     definitions.RunnerSession.__entity_type__) else 0)


def check_counter_capacity(definitions, reader):
    promised = sum(reservation_commits(definitions, reader.resolve(r).values)
        for r in reader.entities(definitions.RuntimeStorageReservation.__entity_type__))
    if not 0 <= reader.sequence <= 2**63 - 1 - promised:
        raise ValueError("runtime_counter_capacity")


def check_staged_capacity(definitions, staged):
    """Exact whole snapshot measurement plus outstanding native reservations.

    Logical accounting only; encoding allocates, and this is not an RSS bound or
    protection against physical disk failure. Infrastructure UNKNOWN is intact.
    """
    from agent_native.persistence.codec import encode_snapshot, durable_projection, MAX_BYTES
    snapshot = staged._data()
    check_counter_capacity(definitions, staged)
    size = len(encode_snapshot(durable_projection(snapshot)))
    reserved = sum(staged.field(r,"bytes") for r in staged.entities(definitions.RuntimeStorageReservation.__entity_type__)
        if staged.field(r,"state") == "reserved")
    if size + reserved > MAX_BYTES:
        raise ValueError("runtime_storage_capacity")
    return size


def response_reservation_ref(kernel, request):
    from ._input_lineage import native_digest
    return kernel._ref(kernel.system.RuntimeStorageReservation, native_digest(("model-response", request)))


def session_reservation_ref(kernel, session):
    from ._input_lineage import native_digest
    return kernel._ref(kernel.system.RuntimeStorageReservation, native_digest(("session-terminal", session)))
