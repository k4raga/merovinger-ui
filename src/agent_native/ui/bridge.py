"""Trusted in-process bridge over the existing C1/C2 managed UI.

This is a local host API, not authentication or a model transport. It shares
the host's UI or full system profile; no Runner, program backend, memory
runtime, CLI or network is constructed here.
"""
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QBoxLayout
from shiboken6 import isValid

from agent_native.context import ContextExportGate, ContextRequest, PolicyRules
from agent_native.kernel.commands import CommandReceipt
from agent_native.kernel.coordinator import Phase
from agent_native.observation import CaptureService, SurfaceRegistry, CaptureError
from .declarations import Action, Control
from .engine import EngineUi
from .host import GuiHostDriver


@dataclass(frozen=True)
class BridgeStatus:
    ready: bool
    stage: str
    last_result: object
    closing: bool
    closed: bool
    error: object


@dataclass(frozen=True, slots=True)
class BridgeInvocationResult:
    """One invocation's public outcome and its original kernel receipt, if any."""
    invocation_id: str
    outcome: str
    final: bool
    receipt: CommandReceipt | None = None
    error: str | None = None


class BridgeInvocation:
    """A live, Qt-thread-owned handle returned by AppBridge.invoke_tracked()."""
    def __init__(self, on_result):
        self._invocation_id = uuid4().hex
        self._intake = None
        self._result = None
        self._on_result = on_result
        self._notification_pending = False

    @property
    def invocation_id(self):
        return self._invocation_id

    @property
    def intake(self):
        """The same intake string returned by AppBridge.invoke()."""
        return self._intake

    @property
    def accepted(self):
        return self.intake == "accepted"

    @property
    def result(self):
        """Latest outcome snapshot, or None while awaiting the first outcome."""
        return self._result

    def _publish(self, outcome, error=None, receipt=None, *, final=True):
        if self._result is not None and self._result.final:
            return
        result = BridgeInvocationResult(self.invocation_id, outcome, final, receipt, error)
        if result != self._result:
            self._result = result
            if self._on_result is not None and not self._notification_pending:
                self._notification_pending = True
                QTimer.singleShot(0, self._notify)

    def _notify(self):
        self._notification_pending = False
        callback, result = self._on_result, self._result
        if result.final:
            self._on_result = None
        callback(result)


class BridgeAttachError(RuntimeError):
    """A failed committed mount may still own work: explicitly close .bridge."""
    def __init__(self, bridge):
        self.bridge = bridge
        super().__init__("Bridge attachment failed; close the retained bridge")


class BridgeObservation:
    """A local, policy-checked plan plus metadata-only original capture handles.

    No PNG/raw pixel accessor is provided. External/model consumers must use the
    existing ExportBroker pipeline and its current authorization/redaction.
    """
    def __init__(self, bridge, plan, batch, destination):
        self._bridge, self._plan, self._batch = bridge, plan, batch
        self._destination, self.closed = destination, False

    def _check(self):
        if self.closed:
            raise RuntimeError("Observation is closed")
        self._bridge.gate.validate(self._plan, destination=self._destination)
        for frame in self._batch.frames:
            frame.stamp  # original owner/expiry checks

    @property
    def plan(self):
        self._check()
        return self._plan

    @property
    def frames(self):
        self._check()
        return self._batch.frames

    @property
    def coverage(self):
        self._check()
        return self._batch.coverage

    def close(self):
        EngineUi.assert_owner()
        if not self.closed:
            self._batch.close()
            self.closed = True
            self._bridge._observations.discard(self)

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *error):
        self.close()


class AppBridge:
    """Embed a managed surface in an existing QApplication and QBoxLayout.

    invoke() returns intake/acceptance. Use invoke_tracked() for the outcome
    of a specific call while the existing Qt event loop drives the HostTurn.
    close() is nonblocking: keep that event loop alive and retry until True.
    Coordinator/kernel/port/application ownership always remains with the host.
    """
    @classmethod
    def attach(cls, parent, *, coordinator, kernel, index, principal,
               declarations, widgets=None, capture=None, extensions=False, readonly=False):
        EngineUi.assert_owner()
        if (not isinstance(parent, QWidget) or not isValid(parent)
                or not isinstance(parent.layout(), QBoxLayout)):
            raise TypeError("Use a live QWidget with an existing QBoxLayout")
        if (kernel.coordinator is not coordinator or kernel.system is not index.system
                or tuple(kernel.registries) != index.registries):
            raise ValueError("Use the SAME coordinator/kernel/index")
        if type(principal) is not str or not principal:
            raise TypeError("Use the trusted host principal")
        if type(extensions) is not bool:
            raise TypeError("Use an exact bool for extensions")
        declarations = tuple(declarations)
        if not declarations or any(type(c) is not Control for c in declarations):
            raise TypeError("Use original finite managed declarations")
        if type(readonly) is not bool:
            raise TypeError("Use an exact bool for readonly")
        if readonly and any(c.action is not None for c in declarations):
            raise ValueError("Readonly bridge declarations cannot contain actions")
        if coordinator.phase != Phase.IDLE:
            raise CaptureError("capture_busy")
        if capture is not None:
            if type(capture) is not CaptureService or capture.coordinator is not coordinator:
                raise ValueError("Use the SAME original CaptureService")
            capture._check()
        # Fail before native/C1 mount if another capture owner already exists.
        owned = capture is None
        if owned:
            capture = CaptureService(coordinator, SurfaceRegistry(coordinator))
        bridge = cls.__new__(cls)
        bridge.engine = None
        bridge.capture, bridge._owns_capture = capture, owned
        bridge.coordinator, bridge.kernel, bridge.index = coordinator, kernel, index
        bridge.principal, bridge._observations = principal, set()
        bridge._model_observations = set()
        bridge._closing = bridge._closed = bridge._disposed = False
        bridge._registered = bridge._mount_attempted = False
        bridge._driver, bridge._owns_driver = None, False
        prior_driver = GuiHostDriver._drivers.get(coordinator)
        try:
            bridge.gate = ContextExportGate(coordinator, index)
            bridge.engine = EngineUi(coordinator, kernel, index, principal,
                                     parent=parent, embedded=True, widgets=widgets,
                                     extensions=extensions, readonly=readonly)
            capture.surfaces.register(bridge.engine)
            bridge._registered = True
            parent.layout().addWidget(bridge.engine.root)
            bridge._mount_attempted = True
            bridge.engine.mount(declarations)
            bridge._driver = bridge.engine.pump.driver
            bridge._owns_driver = prior_driver is None
            return bridge
        except BaseException as error:
            if bridge.engine is not None and hasattr(bridge.engine, "pump"):
                bridge._driver = bridge.engine.pump.driver
                bridge._owns_driver = prior_driver is None
            try:
                bridge.close()
            except Exception:
                pass  # original failure is retained; .bridge still owns cleanup
            raise BridgeAttachError(bridge) from error

    @property
    def surface_id(self):
        return self.engine.surface_id

    @property
    def status(self):
        EngineUi.assert_owner()
        broker = getattr(self.engine, "broker", None)
        return BridgeStatus(
            not self._closing and not self._closed and bool(broker and broker.ready),
            getattr(broker, "stage", "unmounted"),
            getattr(broker, "last_result", None), self._closing, self._closed,
            getattr(self.engine, "health_fault", None)
            or getattr(getattr(self.engine, "pump", None), "error", None))

    def invoke(self, path, value=None):
        """Trusted host invokes a declared bound control through UiSessionBroker."""
        self._validate_action(path)
        return self.engine.invoke(path, value)

    def invoke_tracked(self, path, value=None, *, on_result=None):
        """Invoke a bound control and return its own intake and outcome handle.

        Optional on_result(result) runs on the Qt event loop after this call
        returns. An unresolved outcome can later become final; updates before
        the callback runs may be coalesced into the latest snapshot.
        """
        self._validate_action(path)
        if on_result is not None and not callable(on_result):
            raise TypeError("Use a callable on_result")
        invocation = BridgeInvocation(on_result)
        invocation._intake = self.engine.invoke(path, value, _invocation=invocation)
        if not invocation.accepted and invocation.result is None:
            invocation._publish("failed_no_effect", invocation.intake)
        return invocation

    def _validate_action(self, path):
        EngineUi.assert_owner()
        if self._closing or self._closed:
            raise RuntimeError("Bridge is closing")
        if type(path) is not str or path not in self.engine.controls:
            raise ValueError("Unknown managed control path")
        if self.engine.controls[path].action is None:
            raise ValueError("Control has no bound action")

    def observe(self, destination, *, rules=None):
        """Local policy-bound semantics and frame metadata from ONE guarded cut."""
        EngineUi.assert_owner()
        if not self.status.ready:
            raise RuntimeError("Bridge is not ready")
        rules = PolicyRules() if rules is None else rules
        task, grant = self.engine.observation_scope()
        request = ContextRequest(self.principal, task, grant,
                                 destination, self.engine.focus_ref, rules=rules)
        self.engine.prepare_capture()
        plan = self.gate.current(request)
        batch = None
        try:
            with self.gate.capture(plan, destination=destination) as cut:
                batch = self.capture.capture_in_guard(
                    cut, (self.surface_id,), required_surface_ids=(self.surface_id,))
                if any(frame.stamp.cut != plan.cut
                       or frame.stamp.derived_revision != plan._derived_revision
                       for frame in batch.frames):
                    raise RuntimeError("Context/frame cut mismatch")
            observation = BridgeObservation(self, plan, batch, destination)
            self._observations.add(observation)
            return observation
        except BaseException:
            if batch is not None:
                batch.close()
            raise

    def observe_for_model(self, *, destination, rules=None, limits=None):
        """Prepare policy-filtered context and redacted PNGs for any host adapter.

        Explicitly poll the returned ModelObservation, then take its owned
        payload. No provider, network request or hidden event loop is started.
        """
        from .model import ModelObservation, ExportBroker, Destination
        EngineUi.assert_owner()
        if type(destination) is not Destination:
            raise TypeError("Use an exact nonsecret Destination identity")
        if not self.status.ready:
            raise RuntimeError("Bridge is not ready")
        task, grant = self.engine.observation_scope()
        request = ContextRequest(self.principal, task, grant, destination.identifier,
                                 self.engine.focus_ref, rules=PolicyRules() if rules is None else rules)
        origin = self.kernel.authenticated(self.principal, channel="ui")
        broker = ExportBroker(self.kernel, self.index, self.gate, self.capture,
                              origin, request, destination, limits=limits)
        try:
            pending = ModelObservation(self, broker)
            self._model_observations.add(pending)
            return pending
        except BaseException:
            broker.close()
            raise

    def close(self):
        """Release only this bridge's work; False means cleanup is still pending."""
        EngineUi.assert_owner()
        if self._closed:
            return True
        self._closing = True
        models_drained = all([pending.close() for pending in tuple(self._model_observations)])
        if not models_drained:
            return False
        for observation in tuple(self._observations):
            observation.close()
        engine = self.engine
        if engine is not None and self._mount_attempted and not engine.closed:
            broker = getattr(engine, "broker", None)
            if getattr(engine, "_mount_failed", False) or broker and (broker.ready or broker.stage == "failed"):
                engine.request_close()
            if not engine.closed:
                return False
        if self.coordinator.phase != Phase.IDLE:
            return False
        if self._registered:
            self.capture.surfaces.unregister(engine.surface_id)
            self._registered = False
        if engine is not None and not self._disposed:
            if self._mount_attempted:
                engine.dispose()
            else:
                engine.root.hide()
                engine.root.deleteLater()  # no C1 mount or GUI driver was created
            self._disposed = True
        # Borrowers keep the original shared owner alive. Never close their
        # surface service/driver or silently orphan its timer and registration.
        if self._owns_capture and self.capture.surfaces._entries:
            return False
        driver = self._driver
        if self._owns_driver and driver is not None and not driver.closed:
            if driver.views or not driver.drained:
                return False
            driver._discard_empty()
        if self._owns_capture:
            self.capture.close()
            if getattr(self.coordinator, "_owned_widget_capture_service", None) is self.capture:
                return False  # a moved/pinned consumer still owns real buffers
        self._closed = True
        return True
