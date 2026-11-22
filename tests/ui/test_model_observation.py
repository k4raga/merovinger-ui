"""Real Qt/C1 AppBridge action and export tests."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from queue import Queue
from threading import Event
from time import monotonic, sleep

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from agent_native.context import PolicyRules
from agent_native.kernel.coordinator import DBOutcome, Phase
from agent_native.observation._export_types import ExportError, ExportLimits
from agent_native.observation.channel import Destination, Observation
from agent_native.ui import BridgeInvocation, BridgeInvocationResult


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


@pytest.fixture
def command_demo(tmp_path):
    demo, wait = fixture(tmp_path)
    try:
        yield demo, wait
    finally:
        wait(demo.bridge.close)
        wait(demo.runtime.close)
        demo.window.hide()


def assert_committed(demo, invocation, outcome, enabled):
    assert isinstance(invocation, BridgeInvocation)
    assert isinstance(outcome, BridgeInvocationResult)
    assert outcome is invocation.result
    assert outcome.invocation_id == invocation.invocation_id
    assert outcome.final and outcome.outcome == "committed_local"
    assert outcome.error is None
    assert outcome.receipt.outcome == "committed_local"
    with demo.runtime.coordinator.read() as cut:
        request = cut.view.field(outcome.receipt.invocation, "request")
        assert request.key.entity_id == invocation.invocation_id
        assert cut.view.field(demo.board, "enabled") is enabled


def test_tracked_command_reports_local_commit(command_demo):
    demo, wait = command_demo
    results = []
    invocation = demo.bridge.invoke_tracked("enabled", False, on_result=results.append)
    assert invocation.accepted and invocation.intake == "accepted"
    assert invocation.result is None
    assert results == []

    wait(lambda: results)
    assert len(results) == 1
    assert_committed(demo, invocation, results[0], False)


def test_tracked_command_reports_refusal_without_effect(command_demo):
    demo, wait = command_demo
    results = []
    invocation = demo.bridge.invoke_tracked("enabled", "wrong type", on_result=results.append)
    assert invocation.accepted

    wait(lambda: results)
    assert len(results) == 1
    result = results[0]
    assert result is invocation.result and result.final
    assert result.invocation_id == invocation.invocation_id
    assert (result.outcome, result.error, result.receipt) == ("failed_no_effect", "invalid_arguments", None)
    with demo.runtime.coordinator.read() as cut:
        assert cut.view.field(demo.board, "enabled") is True


@pytest.mark.parametrize("uncertain_enqueue", [False, True])
def test_tracked_command_resolves_after_delayed_ack(command_demo, monkeypatch, uncertain_enqueue):
    demo, wait = command_demo
    coordinator, port = demo.runtime.coordinator, demo.runtime.port
    held = Queue()
    deliver = port._deliver
    submit = port.submit

    def hold_command_ack(outcome, operation_id):
        if operation_id.startswith("command-"):
            held.put(outcome)
        else:
            deliver(outcome, operation_id)

    def lose_enqueue_confirmation(submission):
        disposition = submit(submission)
        if submission.operation_id.startswith("command-"):
            raise OSError("test lost enqueue confirmation")
        return disposition

    monkeypatch.setattr(port, "_deliver", hold_command_ack)
    if uncertain_enqueue:
        monkeypatch.setattr(port, "submit", lose_enqueue_confirmation)

    results = []
    invocation = demo.bridge.invoke_tracked("enabled", False, on_result=results.append)
    wait(lambda: not held.empty())
    ack = held.get_nowait()
    try:
        assert ack.outcome == DBOutcome.COMMITTED and invocation.accepted
        refused = demo.bridge.invoke_tracked("enabled", True)
        assert not refused.accepted and refused.invocation_id != invocation.invocation_id
        assert (refused.result.outcome, refused.result.error) == ("failed_no_effect", "busy")
        if not uncertain_enqueue:
            assert invocation.result is None and not results
            coordinator.offer_db_outcome(replace(ack, outcome=DBOutcome.UNKNOWN))

        wait(lambda: results)
        assert len(results) == 1
        unknown = results[0]
        assert unknown is invocation.result
        assert unknown.invocation_id == invocation.invocation_id
        assert (unknown.outcome, unknown.final, unknown.receipt) == ("unknown", False, None)
        assert coordinator.phase == Phase.RECONCILING
    finally:
        coordinator.offer_db_outcome(ack)
    wait(lambda: len(results) == 2)
    assert_committed(demo, invocation, results[1], False)


def test_sequential_tracked_commands_keep_distinct_receipts(command_demo):
    demo, wait = command_demo
    first_results, second_results, subsequent = [], [], []

    def on_first(result):
        first_results.append(result)
        subsequent.append(demo.bridge.invoke_tracked("enabled", True, on_result=second_results.append))

    first = demo.bridge.invoke_tracked("enabled", False, on_result=on_first)
    wait(lambda: second_results)
    second, = subsequent
    prior, = first_results
    current, = second_results
    assert first.accepted and second.accepted
    assert prior is first.result and current is second.result
    assert prior.invocation_id == first.invocation_id != second.invocation_id
    assert prior.final and prior.outcome == "committed_local"
    assert prior.receipt.plan_id != current.receipt.plan_id
    assert_committed(demo, second, current, True)
    with demo.runtime.coordinator.read() as cut:
        request = cut.view.field(prior.receipt.invocation, "request")
        assert request.key.entity_id == first.invocation_id


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
