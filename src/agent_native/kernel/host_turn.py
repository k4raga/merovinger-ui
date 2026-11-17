"""One Qt-free owner scheduler for the existing Coordinator, never a writer.

Trusted participants get one semantic action per callback. Critical hooks run at
IDLE after every boundary, before a second recovery callback or queued intention.
Widgets own registrations/timers; they never own this turn or retained proofs.
"""
from collections import deque, OrderedDict
from dataclasses import dataclass
from inspect import iscoroutinefunction, isgeneratorfunction, isasyncgenfunction

from .coordinator import Coordinator, CoordinatorBusy, Phase, Status, SubmissionResult, _main_thread


@dataclass(frozen=True, slots=True, init=False)
class TurnRegistration:
    _issuer: object
    owner: object


class HostTurn:
    def __init__(self, coordinator, *, max_registrations=128, max_callbacks=8):
        _main_thread()
        if type(coordinator) is not Coordinator:
            raise TypeError("Use the real Coordinator")
        if coordinator._host_turn is not None:
            raise ValueError("Coordinator already owns a HostTurn")
        for limit in (max_registrations, max_callbacks):
            if type(limit) is not int or limit < 1:
                raise ValueError("Use positive exact limits")
        self.coordinator = coordinator
        self._issuer, self._limit, self._callbacks = object(), max_registrations, max_callbacks
        self._participants, self._critical = {}, {}
        self._futures, self._watches, self._notifications = [], {}, OrderedDict()
        self._rotation, self._running = 0, False
        self.errors = deque(maxlen=16)
        coordinator._host_turn = self

    @staticmethod
    def _callback(callback):
        if (not callable(callback) or iscoroutinefunction(callback)
                or isgeneratorfunction(callback) or isasyncgenfunction(callback)):
            raise TypeError("Use a trusted synchronous one-step callback")

    def _registration(self, owner):
        _main_thread()
        if len(self._participants) + len(self._critical) >= self._limit:
            raise ValueError("HostTurn registration quota")
        registration = object.__new__(TurnRegistration)
        object.__setattr__(registration, "_issuer", self._issuer)
        object.__setattr__(registration, "owner", owner)
        return registration

    def register_participant(self, owner_token, step=None):
        if step is not None:
            self._callback(step)
        registration = self._registration(owner_token)
        self._participants[id(registration)] = (registration, step)
        return registration

    def register_critical(self, owner_token, callback):
        self._callback(callback)
        registration = self._registration(owner_token)
        self._critical[id(registration)] = (registration, callback)
        return registration

    def _check(self, registration):
        _main_thread()
        if (type(registration) is not TurnRegistration or getattr(registration, "_issuer", None) is not self._issuer
                or id(registration) not in self._participants
                or self._participants[id(registration)][0] is not registration):
            raise ValueError("Unissued or disposed HostTurn registration")

    def unregister(self, registration):
        _main_thread()
        entry = self._participants.get(id(registration)) or self._critical.get(id(registration))
        if entry is None or entry[0] is not registration:
            raise ValueError("Unissued or disposed HostTurn registration")
        self._participants.pop(id(registration), None)
        self._critical.pop(id(registration), None)
        self._futures = [x for x in self._futures if x[0] is not registration]
        self._watches = {k: x for k, x in self._watches.items() if x[0] is not registration}
        self._notifications = OrderedDict((k, x) for k, x in self._notifications.items() if x[0] is not registration)

    def poll_future(self, registration, future, one_callback):
        self._check(registration)
        self._callback(one_callback)
        if not callable(getattr(future, "done", None)) or not callable(getattr(future, "result", None)):
            raise TypeError("Use a future with done/result")
        if len(self._futures) >= self._limit:
            raise ValueError("HostTurn future quota")
        self._futures.append((registration, future, one_callback))

    def watch_operation(self, registration, operation_id, callback):
        """Correlated transport notifications, not a journal or receipt authority."""
        self._check(registration)
        self._callback(callback)
        if type(operation_id) is not str or not operation_id:
            raise ValueError("Use an exact operation ID")
        key = (id(registration), operation_id)
        if key in self._watches or key in self._notifications or len(set(self._watches) | set(self._notifications)) >= self._limit:
            raise ValueError("HostTurn operation watch collision/quota")
        self._watches[key] = (registration, callback)

    def _invoke(self, callback, *args, critical=False):
        c = self.coordinator
        if c._turn_budget is not None:
            raise CoordinatorBusy("HostTurn callback reentry")
        c._turn_budget = 0 if critical else 1
        try:
            value = callback(*args)
            c._reject_async(value)
            if critical and value is not None:
                raise TypeError("Critical hook must return None")
            return value
        finally:
            c._turn_budget = None

    def _seal(self):
        if self.coordinator.phase != Phase.IDLE:
            return False
        healthy = True
        for ident, (registration, callback) in tuple(self._critical.items()):
            if ident not in self._critical:
                continue
            try:
                self._invoke(callback, critical=True)
            except Exception as error:
                # Framework hooks must retire their own unusable capability.
                # An accidental broken hook cannot starve all cancellation work.
                self.errors.append((registration.owner, "critical", type(error).__name__))
                self._critical.pop(ident, None)
                healthy = False
            if self.coordinator.phase != Phase.IDLE:
                return False
        return healthy

    def dispatch(self, registration, trusted_one_step, *args):
        """Run one synchronous foreground step through this SAME owner turn.

        Requires a live participant registration and uninterrupted IDLE entry.
        Zero-write critical hooks run BEFORE, followed by a fresh phase/budget
        and registration check. No pump, dequeue, future or other participant
        is run. The callback receives at most one semantic action; AFTER hooks
        run even when it raises. Reentry/busy raises CoordinatorBusy; invalid
        registration raises ValueError, async callbacks/results raise TypeError.

        Callback exceptions propagate: an already accepted first action is NOT
        rolled back. An ordinary AFTER health fault is recorded in errors while
        preserving the accepted result; it must never look like pre-action Busy.
        """
        self._check(registration)
        c = self.coordinator
        if (self._running or c.phase != Phase.IDLE or c.terminal_retry_pending
                or c._turn_budget is not None):
            raise CoordinatorBusy("Foreground dispatch requires non-reentrant unguarded IDLE")
        self._callback(trusted_one_step)
        self._running = True
        primary_error, entered = None, False
        try:
            healthy = self._seal()
            self._check(registration)
            if (not healthy or c.phase != Phase.IDLE or c.terminal_retry_pending
                    or c._turn_budget is not None):
                raise CoordinatorBusy("Foreground dispatch was blocked before its action")
            entered = True
            result = self._invoke(trusted_one_step, *args)
            self._record([], result)
            return result
        except BaseException as error:
            primary_error = error
            if entered and isinstance(error, Exception):
                self.errors.append((registration.owner, "dispatch", type(error).__name__))
            raise
        finally:
            try:
                try:
                    self._seal()
                except BaseException as error:
                    if primary_error is None:
                        raise
                    # Preserve the original callback failure, even if a fatal
                    # AFTER hook also fails. Ordinary hook faults were recorded
                    # by _seal; no accepted action is reported as a rollback.
                    self.errors.append((registration.owner, "critical_after", type(error).__name__))
            finally:
                self._running = False

    def _record(self, outcomes, result):
        if type(result) is not SubmissionResult:
            return
        outcomes.append(result)
        for (ident, operation), (registration, callback) in tuple(self._watches.items()):
            if operation == result.operation_id:
                # One reserved pending notification per watch. Repeated UNKNOWN
                # transport statuses are superseded by the definitive result;
                # they cannot overflow before the critical seal boundary.
                self._notifications[(ident, operation)] = (registration, callback, result)
                if result.status in (Status.COMMITTED, Status.FAILED):
                    del self._watches[(ident, operation)]

    def tick(self):
        _main_thread()
        if self._running:
            raise CoordinatorBusy("HostTurn tick is non-reentrant")
        self._running = True
        c, outcomes = self.coordinator, []
        try:
            if c.terminal_retry_pending:
                self._record(outcomes, c.retry_terminal())
            self._seal()
            if c.phase in (Phase.IDLE, Phase.AWAITING_DB, Phase.RECONCILING) and not c.terminal_retry_pending:
                for result in c.pump(max_intentions=0):
                    self._record(outcomes, result)
            self._seal()
            # One callback then a critical hook; never batch mutations first.
            for _ in range(min(len(self._notifications), self._callbacks)):
                _, (registration, callback, result) = self._notifications.popitem(last=False)
                if id(registration) not in self._participants:
                    continue
                try:
                    returned = self._invoke(callback, result)
                    self._record(outcomes, returned)
                except Exception as error:
                    self.errors.append((registration.owner, "outcome", type(error).__name__))
                self._seal()
            completed = 0
            for registration, future, callback in tuple(self._futures):
                if completed >= self._callbacks:
                    break
                if not future.done():
                    continue
                item = (registration, future, callback)
                if item not in self._futures:
                    continue
                self._futures.remove(item)
                completed += 1
                try:
                    result = self._invoke(lambda: callback(future.result()))
                    self._record(outcomes, result)
                except Exception as error:
                    self.errors.append((registration.owner, "future", type(error).__name__))
                self._seal()
            participants = tuple(self._participants.values())
            if participants and c.phase == Phase.IDLE:
                registration, callback = participants[self._rotation % len(participants)]
                self._rotation += 1
                if callback is not None:
                    try:
                        self._record(outcomes, self._invoke(callback))
                    except Exception as error:
                        self.errors.append((registration.owner, "participant", type(error).__name__))
                    self._seal()
            # This phase observation and pump share an uninterrupted owner stack.
            if c.phase == Phase.IDLE:
                for result in c.pump(max_intentions=1):
                    self._record(outcomes, result)
                self._seal()
            return tuple(outcomes)
        finally:
            self._running = False
