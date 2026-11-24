"""Native user and provider-neutral agent traces over one C1/C2 session."""
from dataclasses import replace
import importlib.util
from pathlib import Path
from queue import Queue
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QVBoxLayout, QWidget

from agent_native.context import PolicyRules
from agent_native.context.policy import ContextUnavailable
from agent_native.kernel import CommandPermissionLimit
from agent_native.kernel.coordinator import DBOutcome, Phase
from agent_native.schema import Ref
from agent_native.state import Patch, Update
from agent_native.ui import InteractionBinding, InteractionNode, UiRuntime, UiUnavailable


_spec = importlib.util.spec_from_file_location(
    "symbiotic_demo", Path(__file__).parents[2] / "examples/symbiotic/main.py",
)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


def wait(test):
    deadline = monotonic() + 10
    app = QApplication.instance()
    while monotonic() < deadline:
        app.processEvents()
        if test():
            return
        sleep(0.002)
    raise AssertionError("Interaction did not settle")


@pytest.fixture
def mount(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    opened = []

    def setup(build, *, rules=None, allow=True, previous=False):
        domain, patch, definition, ref = build()
        path = tmp_path / f"interaction-{len(opened)}.db"
        args = dict(registries=(domain,), path=path, initial_patch=patch,
                    command_policy=lambda req: CommandPermissionLimit(
                        (allow() if callable(allow) else allow) and req.channel == "ui"))
        if previous:
            old = UiRuntime.open(**args)
            manifest = old.store.export_snapshot().schema_manifest
            assert old.close()
        runtime = UiRuntime.open(**args)
        if previous:
            assert runtime.restored
            assert runtime.store.export_snapshot().schema_manifest == manifest
        window = QWidget()
        window.setLayout(QVBoxLayout())
        session = runtime.attach_interaction(window, definition=definition, rules=rules)
        opened.append((session, runtime, window))
        window.show()
        wait(lambda: session.status.ready and (session.displayed is not None or rules is not None))
        return session, runtime, window, ref

    yield setup
    for session, runtime, window in reversed(opened):
        wait(session.close)
        wait(runtime.close)
        window.hide()


def agent(session, packet, call_id, name, args):
    return session.invoke_tool(packet.request_id, packet.offer_id, call_id, name, args)


def native_button(window, key):
    button = window.findChild(QPushButton, key)
    assert button is not None
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def field(runtime, ref, name):
    with runtime.coordinator.read() as cut:
        return cut.view.field(ref, name)


def test_form_navigation_handoff_graph_and_previous_database(mount):
    session, runtime, window, ref = mount(demo.form_definition, previous=True)
    initial = session.prepare_turn("form", frame=True)
    assert not QImage.fromData(initial.image_png).isNull()
    assert [a["id"] for a in initial.actions] == ["open:activate"]

    native_button(window, "open")
    stale = agent(session, initial, "old", "open:activate", {})
    assert stale.result.outcome == "failed_no_effect" and stale.result.error == "stale_interaction"
    assert not window.findChild(QPushButton, "review").isEnabled()
    title = window.findChild(QLineEdit, "title")
    title.setFocus()
    QTest.keyClicks(title, "A")
    packet = session.prepare_turn("form")
    assert packet.state["title"] == "A" and packet.state["page"] == "form"
    assert "review:activate" not in {a["id"] for a in packet.actions}

    filled = agent(session, packet, "fill", "detail:edit", {"text": "Agent text"})
    assert filled.result.outcome == "applied_local"
    assert window.findChild(QLineEdit, "detail").text() == "Agent text"
    same = agent(session, packet, "fill", "detail:edit", {"text": "Agent text"})
    assert same is filled
    conflict = agent(session, packet, "fill", "detail:edit", {"text": "Different"})
    assert conflict.result.error == "call_identity_conflict"

    after_fill = session.prepare_turn("form")
    assert "review:activate" in {a["id"] for a in after_fill.actions}
    native_button(window, "review")
    assert session.prepare_turn("form").state["page"] == "confirm"
    native_button(window, "cancel")
    assert field(runtime, ref, "saves") == 0
    assert session.prepare_turn("form").state["page"] == "home"

    opened = agent(session, session.prepare_turn("form"), "open", "open:activate", {})
    assert opened.result.outcome == "applied_local"
    first = agent(session, session.prepare_turn("form"), "title", "title:edit", {"text": "Shared"})
    assert first.result.outcome == "applied_local"
    detail = window.findChild(QLineEdit, "detail")
    detail.setFocus()
    QTest.keyClicks(detail, "Done")
    native_button(window, "review")
    save_offer = session.prepare_turn("form")
    save = agent(session, save_offer, "save", "save:activate", {})
    assert save.accepted and save.result is None
    wait(lambda: save.result is not None and save.result.final)
    assert save.result.outcome == "committed_local" and save.result.receipt is not None
    assert agent(session, save_offer, "save", "save:activate", {}) is save
    assert field(runtime, ref, "title") == "Shared"
    assert field(runtime, ref, "detail") == "Done"
    assert field(runtime, ref, "saves") == 1
    session.finish_turn("form")


def test_independent_layers_native_and_agent_preview_commit_cancel(mount):
    session, runtime, window, layer = mount(demo.layers_definition)
    surface = window.findChild(QWidget, "merovingerInteraction")
    point = QPoint(55, 87)
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, pos=point)
    assert session.prepare_turn("layers").state["selected"] == "alpha"

    QTest.mousePress(surface, Qt.MouseButton.LeftButton, pos=point)
    start = session.prepare_turn("layers")
    assert start.state["preview"] == 40
    update = agent(session, start, "preview", "alpha:drag", {"dx": 35})
    assert update.result.outcome == "applied_local"
    QTest.mouseRelease(surface, Qt.MouseButton.LeftButton, pos=point)
    mid = session.prepare_turn("layers")
    model_alpha = next(node for node in mid.scene if node["id"] == "alpha")
    assert mid.state["preview"] == model_alpha["rect"][0] == 75
    assert field(runtime, layer, "x") == 40

    cancel = agent(session, mid, "cancel", "cancel:activate", {})
    assert cancel.result.outcome == "applied_local"
    assert session.prepare_turn("layers").state["preview"] is None
    assert field(runtime, layer, "saves") == 0

    begin = agent(session, session.prepare_turn("layers"), "begin", "alpha:drag_begin", {})
    assert begin.result.outcome == "applied_local"
    drag = agent(session, session.prepare_turn("layers"), "drag", "alpha:drag", {"dx": 20})
    assert drag.result.outcome == "applied_local"
    native_button(window, "apply")
    wait(lambda: session.status.ready and field(runtime, layer, "saves") == 1)
    assert field(runtime, layer, "x") == 60
    assert "apply:activate" not in {a["id"] for a in session.prepare_turn("layers").actions}
    session.finish_turn("layers")


def test_denied_action_and_revoked_scope(mount):
    session, runtime, window, ref = mount(demo.form_definition, allow=False)
    for call_id, slot, args in (
        ("open", "open:activate", {}), ("title", "title:edit", {"text": "Good"}),
        ("detail", "detail:edit", {"text": "Also good"}), ("review", "review:activate", {}),
    ):
        assert agent(session, session.prepare_turn("policy"), call_id, slot, args).result.outcome == "applied_local"
    offered = session.prepare_turn("policy")
    assert "save:activate" not in {a["id"] for a in offered.actions}
    assert not window.findChild(QPushButton, "save").isEnabled()
    hidden = agent(session, offered, "save", "save:activate", {})
    assert hidden.result.error == "interaction_offer_unavailable"

    approved = session.prepare_turn("policy")
    grant = session.broker.grant
    runtime.kernel.revoke_grant(runtime.origin, grant)
    wait(lambda: runtime.coordinator.phase == Phase.IDLE)
    assert field(runtime, grant, "revoked") is True
    revoked = agent(session, approved, "cancel", "cancel:activate", {})
    assert revoked.result.outcome == "failed_no_effect"
    with pytest.raises(ContextUnavailable):
        session.prepare_turn("policy", frame=True)
    assert field(runtime, ref, "saves") == 0


def test_unregistered_control_cannot_receive_native_input_or_export(mount):
    session, runtime, window, ref = mount(demo.form_definition)
    raw = QPushButton("Injected", window)
    effects = []
    raw.clicked.connect(lambda: effects.append(True))
    raw.show()
    QTest.mouseClick(raw, Qt.MouseButton.LeftButton)
    assert not effects
    with pytest.raises(UiUnavailable):
        session.prepare_turn("unregistered", frame=True)
    assert field(runtime, ref, "saves") == 0
    raw.deleteLater()


def test_policy_hides_whole_scene_and_frame(mount):
    session, runtime, _, ref = mount(demo.form_definition, rules=PolicyRules(hidden_entities=(
        demo.form_definition()[3].key,
    )))
    with pytest.raises(ContextUnavailable):
        session.prepare_turn("hidden", frame=True)
    assert field(runtime, ref, "saves") == 0


def test_native_widget_divergence_blocks_dispatch_and_frame(mount):
    session, runtime, window, ref = mount(demo.form_definition)
    native_button(window, "open")
    turn = session.prepare_turn("frame", frame=True)
    title = window.findChild(QLineEdit, "title")
    title.setText("SENSITIVE-DIVERGED-NATIVE-TEXT")
    with pytest.raises(UiUnavailable):
        session.prepare_turn("frame", frame=True)
    blocked = agent(session, turn, "mutated-field", "title:edit", {"text": "Unexpected input"})
    assert blocked.result.outcome == "failed_no_effect"
    session.refresh()
    assert title.text() == ""
    assert session.prepare_turn("frame", frame=True).image_png == turn.image_png

    title.move(1, 1)
    with pytest.raises(UiUnavailable):
        session.prepare_turn("frame", frame=True)
    session.refresh()
    title.setEnabled(False)
    with pytest.raises(UiUnavailable):
        session.prepare_turn("frame", frame=True)
    session.refresh()
    title.hide()
    with pytest.raises(UiUnavailable):
        session.prepare_turn("frame", frame=True)
    session.refresh()
    title.setFocus()
    QApplication.sendEvent(title, QInputMethodEvent("SENSITIVE-PREEDIT", []))
    assert title.text() == ""
    assert session.prepare_turn("frame", frame=True).image_png == turn.image_png
    assert field(runtime, ref, "saves") == 0


def test_policy_change_without_new_cut_converges_native_actions(mount):
    permitted = [True]
    session, runtime, window, ref = mount(demo.form_definition, allow=lambda: permitted[0])
    for call_id, slot, args in (
        ("open", "open:activate", {}), ("title", "title:edit", {"text": "Title"}),
        ("detail", "detail:edit", {"text": "Detail"}), ("review", "review:activate", {}),
    ):
        assert agent(session, session.prepare_turn("live-policy"), call_id, slot, args).result.outcome == "applied_local"
    offer = session.prepare_turn("live-policy")
    save = window.findChild(QPushButton, "save")
    assert save.isEnabled()
    permitted[0] = False
    with pytest.raises(UiUnavailable):
        session.prepare_turn("live-policy")
    assert agent(session, offer, "old-save", "save:activate", {}).result.outcome == "failed_no_effect"
    session.refresh()
    denied = session.prepare_turn("live-policy")
    assert denied.sequence == offer.sequence
    assert "save:activate" not in {action["id"] for action in denied.actions}
    assert not save.isEnabled()
    permitted[0] = True
    session.refresh()
    restored = session.prepare_turn("live-policy")
    assert restored.sequence == offer.sequence and save.isEnabled()
    assert field(runtime, ref, "saves") == 0


def test_private_state_is_invalidated_before_membership_reexport(mount):
    session, runtime, _, layer = mount(demo.layers_definition)
    for call_id, slot, args in (
        ("choose", "alpha:activate", {}), ("begin", "alpha:drag_begin", {}),
        ("preview", "alpha:drag", {"dx": 15}),
    ):
        assert agent(session, session.prepare_turn("membership"), call_id, slot, args).result.outcome == "applied_local"
    prior = session.prepare_turn("membership", frame=True)
    assert prior.state["selected"] == "alpha" and prior.state["preview"] == 55

    collection = session.definition.sources["layers"].source
    runtime.kernel._system_submit(runtime.origin, lambda view, basis: Patch(updates=(
        Update(Ref(collection.owner), {collection.field: tuple(
            ref for ref in view.resolve_collection(collection).members if ref != layer
        )}),
    )))
    wait(lambda: runtime.coordinator.phase == Phase.IDLE and session.displayed.identity.sequence > prior.sequence)
    current = session.prepare_turn("membership", frame=True)
    assert current.state["selected"] is None and current.state["preview"] is None
    assert "alpha" not in {node["id"] for node in current.scene}
    assert current.image_png != prior.image_png
    assert agent(session, prior, "old-preview", "alpha:drag", {"dx": 22}).result.outcome == "failed_no_effect"
    assert field(runtime, layer, "saves") == 0


def test_scene_rejects_bindings_without_native_gesture():
    activate = InteractionBinding("open")
    edit = InteractionBinding("edit", inputs=(("value", "text"),))
    with pytest.raises(ValueError, match="no native activation"):
        InteractionNode("edit", "field", (0, 0, 40, 30), "Edit", activate=activate, edit=edit)
    with pytest.raises(ValueError, match="separate activation"):
        InteractionNode("shape", "region", (0, 0, 50, 30), "Shape", activate=activate,
                        drag_begin=InteractionBinding("begin"), drag=InteractionBinding("drag"))


def test_interaction_unknown_is_not_local_success(mount, monkeypatch):
    session, runtime, _, ref = mount(demo.form_definition)
    for call_id, slot, args in (
        ("open", "open:activate", {}), ("title", "title:edit", {"text": "A"}),
        ("detail", "detail:edit", {"text": "B"}), ("review", "review:activate", {}),
    ):
        assert agent(session, session.prepare_turn("uncertain"), call_id, slot, args).result.outcome == "applied_local"

    held = Queue()
    original = runtime.port._deliver

    def hold(ack, operation_id):
        if operation_id.startswith("command-"):
            held.put(ack)
        else:
            original(ack, operation_id)

    monkeypatch.setattr(runtime.port, "_deliver", hold)
    received = []
    turn = session.prepare_turn("uncertain")
    call = session.invoke_tool("uncertain", turn.offer_id, "save", "save:activate", {}, on_result=received.append)
    wait(lambda: not held.empty())
    ack = held.get_nowait()
    try:
        runtime.coordinator.offer_db_outcome(replace(ack, outcome=DBOutcome.UNKNOWN))
        wait(lambda: received)
        assert received[0].outcome == "unknown" and not received[0].final
        assert not session.status.ready
        session.finish_turn("uncertain")
        assert ("uncertain", "save") not in session._calls
    finally:
        runtime.coordinator.offer_db_outcome(ack)
    wait(lambda: call.result.final)
    assert call.result.outcome == "committed_local"
    wait(lambda: received[-1].final)
    assert received[-1].outcome == "committed_local" and not session._calls
    assert field(runtime, ref, "saves") == 1
