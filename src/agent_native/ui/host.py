"""Qt lifetime adapter for the SAME Qt-free Coordinator HostTurn.

One application-owned timer drives all views and future runner participants.
No view owns the core scheduler, SQL progress or another client's futures.
"""
from weakref import WeakKeyDictionary
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication
from agent_native.kernel.coordinator import Phase, CoordinatorBusy


class _PolledFuture:
    """Terminal errors are data until the owned UI finalizer has run.

    This retires only a HostTurn polling registration. Future cancellation
    does not attest physical worker return or release any service charge.
    """
    def __init__(self, future):
        self.future = future

    def done(self):
        return self.future.done()

    def result(self):
        try:
            return True, self.future.result()
        except BaseException as error:
            return False, error


class GuiHostDriver(QObject):
    _drivers = WeakKeyDictionary()

    @classmethod
    def attach(cls, coordinator, *, application=None, interval_ms=5):
        from .engine import EngineUi
        EngineUi.assert_owner()
        app = QApplication.instance()
        if app is None or application is not None and application is not app:
            raise ValueError("Use the owning QApplication")
        if type(interval_ms) is not int or interval_ms < 1:
            raise ValueError("Use a positive timer interval")
        driver = cls._drivers.get(coordinator)
        if driver is None:
            driver = cls(coordinator, app, interval_ms)
            cls._drivers[coordinator] = driver
        elif driver.closed:
            raise RuntimeError("GuiHostDriver is closed")
        return driver

    def __init__(self, coordinator, application, interval_ms):
        from .engine import EngineUi
        EngineUi.assert_owner()
        if coordinator in self._drivers or application is not QApplication.instance():
            raise ValueError("Use the one owning GUI host")
        super().__init__(application)
        self.coordinator, self.turn = coordinator, coordinator.host_turn
        self.views, self.closed, self.error, self._futures = [], False, None, 0
        try:
            self.registration = self.turn.register_participant(self)
            self.timer = QTimer(self)
            self.timer.setInterval(interval_ms)
            self.timer.timeout.connect(self.tick)
            self.timer.start()
        except BaseException:
            if hasattr(self,"registration"):
                self.turn.unregister(self.registration)
            if hasattr(self,"timer"):
                self.timer.stop()
            self.closed = True
            self.deleteLater()
            raise
        self._drivers[coordinator] = self

    def _discard_empty(self):
        """Undo only a driver created solely by a refused first mount."""
        if self.views or self._futures or self.closed:
            raise RuntimeError("Cannot discard an owned GUI host with work")
        self.turn.unregister(self.registration)
        self.timer.stop()
        self.closed = True
        if self._drivers.get(self.coordinator) is self:
            del self._drivers[self.coordinator]
        self.deleteLater()

    def attach_view(self, engine):
        engine.assert_owner()
        if engine.coordinator is not self.coordinator or self.closed:
            raise ValueError("Foreign or closed GUI host")
        facade = GuiPump(engine, driver=self)
        self.views.append(facade)
        return facade

    def tick(self):
        from .engine import EngineUi
        EngineUi.assert_owner()
        if self.closed:
            return ()
        # tick owns pump0, critical boundaries and IDLE-only pump1. No second
        # Coordinator driver or batching of semantic callbacks exists here.
        outcomes = self.turn.tick()
        if self.coordinator.phase == Phase.IDLE:
            for facade in tuple(self.views):
                if facade.active and not facade.engine.closed and not facade.error:
                    try:
                        facade.engine.refresh()
                    except Exception as error:
                        facade._fault(error)
        return outcomes

    @property
    def drained(self):
        return not self._futures and all(x.engine.closed for x in self.views)

    def close(self):
        from .engine import EngineUi
        EngineUi.assert_owner()
        if not self.drained or self.coordinator.phase != Phase.IDLE:
            raise CoordinatorBusy("Settle GUI work before host shutdown")
        for facade in tuple(self.views):
            facade.dispose()
        self.turn.unregister(self.registration)
        self.timer.stop()
        self.closed = True


class GuiPump:
    """Compatible per-view facade; stop never stops the shared host timer."""
    def __init__(self, engine, *, interval_ms=5, driver=None):
        engine.assert_owner()
        self.engine = engine
        self._new_driver = driver is None and engine.coordinator not in GuiHostDriver._drivers
        self.driver = driver or GuiHostDriver.attach(engine.coordinator, interval_ms=interval_ms)
        if any(x.engine is engine for x in self.driver.views):
            raise ValueError("View already has a GUI facade")
        self.error, self.active, self.disposed = None, False, False
        try:
            self.registration = self.driver.turn.register_participant(self, self._step)
        except BaseException:
            if self._new_driver and not self.driver.views and not self.driver._futures:
                self.driver._discard_empty()
            raise
        if driver is None:
            self.driver.views.append(self)

    @property
    def timer(self):
        return self.driver.timer

    def _fault(self, error):
        self.error = type(error).__name__
        self.engine.health_fault = "pump:" + self.error
        self.engine.dirty = True
        self.engine.refuse("gui_pump_failed")

    def _step(self):
        if not self.active or self.disposed or self.engine.closed:
            return None
        try:
            # Closing is recovery even after a presentation/paint fault.
            if self.error and self.engine.broker.stage not in ("closing", "closing_view"):
                return None
            return self.engine.step_once()
        except Exception as error:
            self._fault(error)
            raise

    def dispatch(self, callback, *args):
        self.engine.assert_owner()
        if self.disposed or not self.active:
            raise CoordinatorBusy("View intake is stopped")
        return self.driver.turn.dispatch(self.registration, callback, *args)

    def poll_future(self, future, callback):
        self.engine.assert_owner()
        if not callable(getattr(future, "done", None)) or not callable(getattr(future, "result", None)):
            raise TypeError("Use a future with done/result")
        self.driver._futures += 1
        finalized = False
        def finalize():
            nonlocal finalized
            if not finalized:
                finalized = True
                self.driver._futures -= 1
        def complete(outcome):
            try:
                successful, value = outcome
                if not successful:
                    raise value
                return callback(value)
            except Exception as error:
                self._fault(error)
                raise
            finally:
                finalize()
        try:
            # HostTurn obtains result BEFORE invoking its callback. A private
            # outcome adapter ensures exception/cancel also reaches finalization.
            # Host ownership survives disposal; this is not a worker-return proof.
            self.driver.turn.poll_future(self.driver.registration, _PolledFuture(future), complete)
        except BaseException:
            finalize()
            raise

    def tick(self):
        self.engine.assert_owner()
        return self.driver.tick()

    def start(self):
        self.engine.assert_owner()
        if self.disposed:
            raise ValueError("Disposed view facade")
        self.active = True

    def stop(self):
        self.engine.assert_owner()
        self.active = False

    def _abandon_unmounted(self):
        """Only pre-commit mount rollback; never erase accepted UI state."""
        self.engine.assert_owner()
        if self.engine._mount_committed:
            raise RuntimeError("Accepted mount requires a separate owned close")
        if not self.disposed:
            self.driver.turn.unregister(self.registration)
            self.active, self.disposed = False, True
            self.driver.views.remove(self)
            if self._new_driver and not self.driver.views and not self.driver._futures:
                self.driver._discard_empty()

    def dispose(self):
        self.engine.assert_owner()
        if not self.engine.closed:
            raise CoordinatorBusy("Close the view before disposal")
        if not self.disposed:
            self.driver.turn.unregister(self.registration)
            self.disposed, self.active = True, False
            if self in self.driver.views:
                self.driver.views.remove(self)
