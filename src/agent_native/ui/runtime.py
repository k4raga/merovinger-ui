"""Standalone UI ownership over the original C1/C2 and SQLite implementations.

No QApplication, Qt event loop, model, transport or sandbox is created here.
The database path is explicit. Full SYS7 databases are never migrated to UI.
"""
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from agent_native.schema import Registry
from agent_native.state import StateStore, Patch
from agent_native.state.derived import DerivedCache
from agent_native.kernel import Coordinator, Phase, Status
from agent_native.kernel.commands import CommandKernel
from agent_native.kernel.system_definitions import ui_system_definitions
from agent_native.context import install_context_definitions, ContextIndex, ContextExportGate
from agent_native.persistence import SQLiteDurablePort, bootstrap_pair


class UiRuntimeOpenError(RuntimeError):
    """Failed bootstrap retains its owner; retry runtime.close() for late DB ACKs.

    Closing advances only that bootstrap publication, without replay/cancellation.
    Live bridge/HostTurn registrations still require their original lifecycle.
    """
    def __init__(self, runtime):
        self.runtime = runtime
        super().__init__("UI runtime bootstrap failed")


class UiRuntime:
    """Own one UI-profile database/kernel. Close attached bridges before close().

    open() performs bounded synchronous SQLite bootstrap, not a GUI event loop.
    During normal use the existing GUI driver owns HostTurn progress. close()
    returns False while live registrations or durable work remain; it never
    discards that work or rewrites database compatibility information.
    """
    @classmethod
    def open(cls, *, registries, path, initial_patch=None, principal="owner",
             command_policy=None, timeout=10):
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError("Use a finite bootstrap timeout in (0, 300]")
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Use an absolute explicit SQLite path")
        registries = tuple(registries)
        if not registries or any(type(r) is not Registry for r in registries):
            raise TypeError("Provide the original domain Registry objects")
        if initial_patch is not None and type(initial_patch) is not Patch:
            raise TypeError("initial_patch must be a real Patch")
        if type(principal) is not str or not principal:
            raise ValueError("A principal is required")
        self = cls.__new__(cls)
        self.port = self.coordinator = self.kernel = self.gate = None
        self.closed = False
        self._port_close = None
        self._bridges = []
        self._bootstrap_transaction = None
        self._bootstrap_failed = False
        self.path, self.principal = path, principal
        self.system = ui_system_definitions()
        self.ui = install_context_definitions(self.system)
        self.registries = (*registries, self.system.registry)
        self.index = ContextIndex(self.system, self.ui, self.registries)
        epoch = "ui-" + uuid4().hex
        try:
            self.port = SQLiteDurablePort(path, self.registries, runtime_epoch=epoch)
            image = self.port.start().result(timeout=timeout)
            self.restored = image.snapshot is not None
            if self.restored:
                self.store, self.cache = bootstrap_pair(self.registries, image,
                    definitions=self.index.query_definitions, epoch=epoch)
            else:
                self.store = StateStore(self.registries, epoch=epoch)
                with self.store.read() as cut:
                    self.cache = DerivedCache.rebuild(cut, self.index.query_definitions)
            self.coordinator = Coordinator(self.store, self.cache, durable_port=self.port,
                priority_keys=self.index.required_keys)
            self.turn = self.coordinator.host_turn
            self.port.bind_outcome_sink(self.coordinator.offer_db_outcome)
            self.kernel = CommandKernel(self.coordinator, self.registries, self.system,
                journal=image.journal, command_policy=command_policy)
            self.origin = self.kernel.authenticated(principal)
            self.gate = ContextExportGate(self.coordinator, self.index)
            if not self.restored:
                terminal = []
                result = self.kernel._system_submit(self.origin,
                    lambda current, basis: initial_patch if initial_patch is not None else Patch(),
                    on_terminal=terminal.append)
                self._bootstrap_transaction = result.transaction_id
                end = monotonic() + timeout
                while result.status == Status.AWAITING_DB and not terminal and monotonic() < end:
                    self.turn.tick()
                    sleep(.001)
                final = terminal[-1] if terminal else result
                if final.status != Status.COMMITTED:
                    raise RuntimeError("Initial UI publication did not commit: " + str(final.status))
            self._bootstrap_transaction = None
            return self
        except BaseException as error:
            self._bootstrap_failed = True
            try:
                self.close(timeout=timeout)
            except BaseException:
                pass
            raise UiRuntimeOpenError(self) from error

    def attach(self, parent, *, declarations, **options):
        """Attach the original managed AppBridge; caller owns QApplication."""
        if self.closed or self._port_close is not None:
            raise RuntimeError("UI runtime is closing")
        from .bridge import AppBridge
        bridge = AppBridge.attach(parent, coordinator=self.coordinator,
            kernel=self.kernel, index=self.index, principal=self.principal,
            declarations=declarations, **options)
        self._bridges.append(bridge)
        return bridge

    def close(self, *, timeout=5):
        """Return False for live owners/work. Never cancel tasks behind a bridge."""
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError("Use a finite close timeout in (0, 300]")
        if self.closed:
            return True
        if any(not bridge.status.closed for bridge in self._bridges):
            return False
        if self.coordinator is not None:
            coordinator = self.coordinator
            turn = coordinator._host_turn
            if turn is not None and (turn._participants or turn._critical):
                return False
            # Failed open has no GUI driver. Drain only its original publication;
            # never dequeue a caller's intention or replay/cancel the transaction.
            if self._bootstrap_failed and self._bootstrap_transaction is not None:
                end = monotonic() + timeout
                while (coordinator.phase in (Phase.AWAITING_DB, Phase.RECONCILING)
                        and not coordinator.terminal_retry_pending
                        and coordinator.pending_transaction_id == self._bootstrap_transaction
                        and monotonic() < end):
                    coordinator.pump(max_intentions=0)
                    if coordinator.phase != Phase.IDLE:
                        sleep(.001)
            if (coordinator.phase != Phase.IDLE or coordinator.terminal_retry_pending
                    or coordinator._queue):
                return False
        if self.kernel is not None:
            self.kernel.close()
        if self.port is not None:
            if self._port_close is None:
                self._port_close = self.port.close()
            try:
                result = self._port_close.result(timeout=timeout)
            except TimeoutError:
                return False
            if not result.resolved:
                raise RuntimeError("SQLite ownership unresolved: " + str(result.detail))
            thread = self.port._thread
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    return False
        self.closed = True
        return True
