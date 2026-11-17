"""Affected real Qt/C1 export tests; no provider/Runner/program backend."""
import importlib.util
import json
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from agent_native.context import PolicyRules
from agent_native.observation._export_types import ExportError, ExportLimits
from agent_native.observation.channel import Destination, Observation


def fixture(tmp_path, *, readonly=False):
    spec = importlib.util.spec_from_file_location(
        "export_example", Path(__file__).parents[2] / "examples/ui_agent/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    demo = module.build_demo(tmp_path / "export.sqlite", readonly=readonly)
    demo.window.show()

    def wait(predicate):
        deadline = monotonic() + 20
        while monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            sleep(.002)
        raise AssertionError("Real UI/export operation did not settle")

    wait(lambda: demo.bridge.status.ready)
    return demo, wait


def test_model_png_context_action_fresh_frame_and_policy(tmp_path):
    demo, wait = fixture(tmp_path)
    bridge = demo.bridge
    dest = Destination("local-agent", "custom", "host-user", "test-v1")
    try:
        pending = bridge.observe_for_model(destination=dest)
        wait(pending.poll)
        issued = pending._observation
        # Recombining even genuine handles cannot invent an issued observation.
        forged = Observation(issued.observation_id, issued.context, issued.assets, issued.visual_status)
        with pytest.raises(ExportError):
            pending._broker.materialize(forged)
        with pending.take() as packet:
            assert packet.context_json and json.loads(packet.context_json)
            assert len(packet.images) == 1
            image = packet.images[0]
            assert not QImage.fromData(image.png, "PNG").isNull()
            assert image.stamp.cut == packet.cut
            old_png, old_cut = image.png, packet.cut
            assert b"Ready" in packet.context_json
        with pytest.raises(ExportError):
            pending.take()
        wait(pending.close)

        assert bridge.invoke("enabled", False) == "accepted"
        wait(lambda: bridge.status.ready)
        pending = bridge.observe_for_model(destination=dest)
        wait(pending.poll)
        with pending.take() as packet:
            assert b'"enabled":false' in packet.context_json
            assert packet.cut != old_cut
            assert packet.images[0].png != old_png
        wait(pending.close)

        hidden = PolicyRules(hidden_fields=((demo.board.key.entity_type, "enabled"),))
        pending = bridge.observe_for_model(destination=dest, rules=hidden)
        wait(pending.poll)
        with pending.take() as packet:
            assert b'"enabled":false' not in packet.context_json
            assert packet.images == ()
            assert packet.visual_status == "unavailable"
        wait(pending.close)

        # One coordinator-wide ceiling covers TWO brokers and mixed old wires.
        limits = ExportLimits(max_wire_handles=2)
        a = bridge.observe_for_model(destination=dest, limits=limits)
        b = bridge.observe_for_model(destination=dest, limits=limits)
        wait(a.poll)
        wait(b.poll)
        first = a.take()
        wire = b._broker.seal((b._observation.context,))
        with pytest.raises(ExportError, match="export_quota_exceeded"):
            b.take()
        wire.close()
        second = b.take()
        literal = a._broker.user_intent(demo.runtime.kernel.authenticated("owner"), "quota")
        with pytest.raises(ExportError, match="export_quota_exceeded"):
            a._broker.seal((literal,))
        first.close()
        wire = a._broker.seal((literal,))
        wire.close()
        literal.close()
        second.close()
        wait(a.close)
        wait(b.close)

        # Final live task/grant checks happen again after readiness, before bytes.
        pending = bridge.observe_for_model(destination=dest)
        wait(pending.poll)
        result = demo.runtime.kernel.revoke_grant(demo.runtime.kernel.authenticated("owner"),
                                         bridge.engine.broker.grant)
        wait(lambda: demo.runtime.coordinator.phase.value == "idle")
        with pytest.raises(ExportError):
            pending.take()
        wait(pending.close)
    finally:
        wait(bridge.close)
        wait(demo.runtime.close)
        demo.window.hide()


def test_close_waits_for_real_cpu_return(tmp_path, monkeypatch):
    from agent_native.observation import export_policy
    demo, wait = fixture(tmp_path, readonly=True)
    bridge = demo.bridge
    entered, release = Event(), Event()
    original = export_policy._cpu_entry

    def held(mailbox):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("Test CPU release deadline")
        return original(mailbox)

    monkeypatch.setattr(export_policy, "_cpu_entry", held)
    try:
        pending = bridge.observe_for_model(
            destination=Destination("cancel-agent", "custom", "host-user", "test-v1"))
        def started():
            pending.poll()
            return entered.is_set()
        wait(started)
        assert bridge.close() is False
        assert not pending.closed and not bridge.status.closed
        assert bridge.capture.measurements().reserved_bytes > 0
        release.set()
        wait(bridge.close)
        assert pending.closed and bridge.status.closed
        assert pending._broker.measurements().retained_bytes == 0
        assert pending._broker.measurements().working_bytes == 0
    finally:
        release.set()
        wait(bridge.close)
        wait(demo.runtime.close)
        demo.window.hide()
