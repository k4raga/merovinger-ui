"""Explicit owner-thread model observation; no provider or hidden event loop."""
from agent_native.observation.channel import ExportBroker, Destination, ModelPayload, ModelImage
from agent_native.observation._export_types import ExportError
from .engine import EngineUi


class ModelObservation:
    """poll() advances export CPU work; the host continues its normal Qt loop.

    take() performs the final live authorization and transfers one owned packet.
    close() must be retried until True: cancellation alone is not physical drain.
    """
    def __init__(self, bridge, broker):
        self._bridge, self._broker = bridge, broker
        self._demand = broker.current(surface_ids=(bridge.surface_id,))
        self._observation = None
        self._taken = self._closing = self.closed = False

    def poll(self):
        EngineUi.assert_owner()
        if self._closing:
            return self.close()
        self._broker.pump()
        if self._observation is None and not self._taken:
            try:
                self._observation = self._demand.result()
            except ExportError as error:
                if error.code == "observation_pending":
                    return False
                raise
        return self._observation is not None or self._taken

    def take(self):
        EngineUi.assert_owner()
        if self._closing or self._taken:
            raise ExportError("observation_unavailable")
        if self._observation is None:
            raise ExportError("observation_pending")
        payload = self._broker.materialize(self._observation)
        self._observation, self._taken = None, True
        return payload

    def close(self):
        EngineUi.assert_owner()
        if self.closed:
            return True
        self._closing = True
        if self._observation is not None:
            self._observation.close()
            self._observation = None
        self._broker.close()
        self._broker.pump()
        if not self._broker.drained:
            return False
        self.closed = True
        self._bridge._model_observations.discard(self)
        return True
