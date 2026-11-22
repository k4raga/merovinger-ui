"""Managed Qt bindings and an embeddable in-process AppBridge.

Importing this package loads Qt Widgets; importing AppBridge never starts a
model, sandbox, QApplication or event loop. Bridge exports are lazy to avoid
the capture registry / UI package import cycle.
"""
from .declarations import Action, Control, INPUT, text, button, editor, list_view, number, menu, canvas, toggle
from .engine import EngineUi, UiUnavailable, UiAppliedCut, NodeMapping, PaintProvenance
from .session import UiSessionBroker
from .host import GuiPump, GuiHostDriver

__all__ = ["Action","Control","INPUT","text","button","editor","list_view","number","menu","canvas","toggle",
           "EngineUi","UiUnavailable","UiAppliedCut","NodeMapping","PaintProvenance","GuiPump","GuiHostDriver","UiSessionBroker"]


_BRIDGE_EXPORTS = ('AppBridge', 'BridgeObservation', 'BridgeStatus', 'BridgeAttachError',
                   'BridgeInvocation', 'BridgeInvocationResult')
_RUNTIME_EXPORTS = ('UiRuntime', 'UiRuntimeOpenError')
_MODEL_EXPORTS = ('ModelObservation', 'ModelPayload', 'ModelImage', 'Destination')


def __getattr__(name):
    if name in _RUNTIME_EXPORTS:
        from . import runtime
        value = getattr(runtime, name)
        globals()[name] = value
        return value
    if name in _MODEL_EXPORTS:
        from . import model
        value = getattr(model, name)
        globals()[name] = value
        return value
    if name in _BRIDGE_EXPORTS:
        from . import bridge
        value = getattr(bridge, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ += list(_BRIDGE_EXPORTS + _RUNTIME_EXPORTS + _MODEL_EXPORTS)
