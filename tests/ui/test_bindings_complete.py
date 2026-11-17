"""Finite affected native bindings: existing/generated editing, list, bool/int and readonly.

Fresh UI-profile SQLite only; no model, program, sandbox, old stores or platform matrix.
"""
from pathlib import Path
from time import monotonic, sleep
import json
import os

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent, QTimer, QItemSelectionModel
from PySide6.QtGui import QStandardItemModel, QStandardItem, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLineEdit, QListView, QCheckBox, QSpinBox

from agent_native.schema import Registry, Ref, EntityKey, Owned, CollectionRef
from agent_native.state import Patch, Create
from agent_native.state.tracking import FieldRef
from agent_native.kernel import CommandPermissionLimit, Phase
from agent_native.kernel.commands import Plan
from agent_native.state import Update
from agent_native.ui import EngineUi, Action, INPUT, editor, list_view, number, toggle, UiUnavailable, UiRuntime
from agent_native.context import ContextRequest
from agent_native.observation import SurfaceRegistry, CaptureService


def spin(predicate, timeout=8):
    end = monotonic() + timeout
    while monotonic() < end:
        QApplication.instance().processEvents()
        if predicate():
            return
        sleep(.001)
    raise AssertionError("finite native binding did not settle")


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication(["bindings-complete"])


class Env:
    def __init__(self, path):
        self.host, self.engines, self.capture = None, [], None
        self.app = Registry("ui.complete.bindings", 1)
        @self.app.entity("item")
        class Item:
            title: str
            checked: bool
            count: int
        @self.app.entity("shelf")
        class Shelf:
            items: Owned[Item]
        self.calls = []
        @self.app.command("rename")
        def rename(ctx, item: Ref[Item], value: str):
            self.calls.append(("rename", value))
            return Plan(Patch(updates=(Update(item, {"title": value}),)))
        @self.app.command("check")
        def check(ctx, item: Ref[Item], value: bool):
            self.calls.append(("check", value))
            return Plan(Patch(updates=(Update(item, {"checked": value}),)))
        @self.app.command("count")
        def count(ctx, item: Ref[Item], value: int):
            self.calls.append(("count", value))
            return Plan(Patch(updates=(Update(item, {"count": value}),)))
        self.item = Ref(EntityKey(Item.__entity_type__, "first", 1))
        self.other = Ref(EntityKey(Item.__entity_type__, "second", 1))
        self.shelf = Ref(EntityKey(Shelf.__entity_type__, "shelf", 1))
        self.collection = CollectionRef(self.shelf.key, "items")
        self.commands = {name: self.app.command_descriptor(name).definition_id for name in ("rename", "check", "count")}
        initial = Patch(creates=(
            Create(Item.__entity_type__, "first", {"title": "alpha", "checked": False, "count": 3}),
            Create(Item.__entity_type__, "second", {"title": "beta", "checked": True, "count": 4}),
            Create(Shelf.__entity_type__, "shelf", {"items": (self.item, self.other)})))
        self.runtime = UiRuntime.open(registries=(self.app,), path=path.resolve(), initial_patch=initial,
            command_policy=lambda request: CommandPermissionLimit(request.principal == "owner" and request.channel == "ui"))
        self.capture = CaptureService(self.runtime.coordinator, SurfaceRegistry(self.runtime.coordinator))

    def attach(self, declarations, widgets=None, readonly=False):
        engine = EngineUi(self.runtime.coordinator, self.runtime.kernel, self.runtime.index, "owner",
                          parent=self.host, embedded=True, widgets=widgets, extensions=False, readonly=readonly)
        self.engines.append(engine)
        self.host.layout().addWidget(engine.root)
        engine.mount(declarations)
        engine.root.show()
        self.ready(engine)
        return engine

    def ready(self, engine):
        spin(lambda: self.runtime.coordinator.phase == Phase.IDLE and engine.broker.ready
             and not engine.dirty and not engine.inputs and engine.displayed is not None)

    def row(self, ref):
        with self.runtime.coordinator.read() as cut:
            return dict(cut.view.resolve(ref).values)

    def detach(self, engine):
        if not engine.closed:
            engine.request_close()
            spin(lambda: engine.closed)
        engine.dispose()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def frame(self, engine):
        self.ready(engine)
        self.capture.surfaces.register(engine)
        try:
            with self.capture.capture((engine.surface_id,)) as batch:
                assert batch.frames[0].stamp.cut == engine.displayed.identity
                assert batch.frames[0].stamp.capture_target == "managed-content"
                assert any(part.kind == "draft" for mapping in batch.frames[0].mappings for part in mapping.node.provenance)
        finally:
            self.capture.surfaces.unregister(engine.surface_id)

    def close(self):
        errors = []
        for engine in reversed(self.engines):
            if not engine.disposing:
                try:
                    self.detach(engine)
                except Exception as error:
                    errors.append(str(error))
        if self.capture is not None:
            self.capture.close()
        if self.engines:
            try:
                self.engines[0].pump.driver.close()
            except Exception as error:
                errors.append(str(error))
        if self.host is not None:
            self.host.close()
        assert self.runtime.close(), errors
        assert not errors


@pytest.fixture
def env(application, tmp_path):
    value = Env(tmp_path / "fresh-ui.sqlite")
    value.host = QWidget()
    value.host.resize(700, 820)
    value.host.setLayout(QVBoxLayout())
    value.host.show()
    try:
        yield value
    finally:
        value.close()


def declarations(env, actions=True):
    def action(name):
        return Action(env.commands[name], {"item": env.item, "value": INPUT}) if actions else None
    return (editor("edit", FieldRef(env.item, "title"), action=action("rename")),
            list_view("items", env.collection, label_field="title"),
            toggle("check", FieldRef(env.item, "checked"), action("check"), label="Checked"),
            number("count", FieldRef(env.item, "count"), action("count"), minimum=0, maximum=99))


def test_real_existing_inputs_lists_toggles_numbers_restore_and_reattach(env):
    edit = QLineEdit("host edit", env.host)
    edit.setReadOnly(True)
    edit.setPlaceholderText("Host hint")
    edit.setMaxLength(20)
    edit.setCursorPosition(2)
    view = QListView(env.host)
    model = QStandardItemModel(view)
    model.appendRow(QStandardItem("Host A"))
    model.appendRow(QStandardItem("Host B"))
    view.setModel(model)
    view.setCurrentIndex(model.index(1, 0))
    selection = view.selectionModel()
    check = QCheckBox("Host checkbox", env.host)
    check.setChecked(True)
    count = QSpinBox(env.host)
    count.setRange(10, 20)
    count.setValue(12)
    count.setPrefix("host:")
    widgets = {"edit": edit, "items": view, "check": check, "count": count}
    for widget in widgets.values():
        env.host.layout().addWidget(widget)
    events = []
    edit.textEdited.connect(lambda value: events.append(("edit", value)))
    check.clicked.connect(lambda value: events.append(("check", value)))
    ticks = []
    timer = QTimer(env.host)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(2)
    for round_number in (1, 2):
        engine = env.attach(declarations(env), widgets)
        assert engine.widgets["edit"] is edit and type(edit) is QLineEdit
        assert view.model() is engine.models["items"] and not edit.placeholderText()
        engine.focus("edit")
        env.ready(engine)
        QTest.keyClick(edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(edit, "renamed" + str(round_number))
        assert env.row(engine.drafts["edit"])["text"] == "renamed" + str(round_number)
        assert env.row(env.item)["title"] != "renamed" + str(round_number)
        ime = QInputMethodEvent("preedit", [])
        QCoreApplication.sendEvent(edit, ime)
        assert env.row(engine.drafts["edit"])["composing"]
        engine.focus("items")
        env.ready(engine)
        assert not env.row(engine.drafts["edit"])["composing"]
        QTest.keyClick(view, Qt.Key.Key_Down)
        with env.runtime.coordinator.read() as cut:
            chosen = cut.view.field(engine.selections["items"], "resources")
            assert len(chosen) == 1
        engine.focus("edit")
        env.ready(engine)
        QTest.keyClick(edit, Qt.Key.Key_Return)
        spin(lambda: env.row(env.item)["title"] == "renamed" + str(round_number))
        env.ready(engine)
        check.click()
        spin(lambda: env.row(env.item)["checked"] == (round_number == 1))
        env.ready(engine)
        count.stepUp()
        spin(lambda: env.row(env.item)["count"] == 3 + round_number)
        env.frame(engine)
        if round_number == 1:
            edit.setPlaceholderText("UNTRACKED")
            with pytest.raises(UiUnavailable, match="editor presentation"):
                engine.prepare_capture()
            edit.setPlaceholderText("")
        old_calls = len(env.calls)
        env.detach(engine)
        assert edit.parentWidget() is env.host and edit.text() == "host edit"
        assert edit.isReadOnly() and edit.maxLength() == 20 and edit.cursorPosition() == 2
        assert edit.placeholderText() == "Host hint"
        assert view.findChildren(QItemSelectionModel) == [selection]
        assert view.model() is model and view.selectionModel() is selection
        assert view.currentIndex().row() == 1 and model.data(model.index(0, 0)) == "Host A"
        assert check.text() == "Host checkbox" and check.isChecked()
        assert count.minimum() == 10 and count.maximum() == 20 and count.value() == 12 and count.prefix() == "host:"
        edit.setReadOnly(False)
        QTest.keyClicks(edit, "x")
        edit.setReadOnly(True)
        edit.setText("host edit")
        edit.setCursorPosition(2)
        check.click()
        check.setChecked(True)
        assert len(env.calls) == old_calls
        assert events and ticks and env.host.isVisible()
    assert len(env.calls) == 6
    path = os.environ.get("AGENT_NATIVE_BINDINGS_EVIDENCE")
    if path:
        Path(path).write_text(json.dumps({"status": "PASS", "existing_types": ["QLineEdit", "QListView", "QCheckBox", "QSpinBox"],
            "rounds": 2, "typed_commands": len(env.calls), "canonical_draft_selection_ime": True,
            "capture": True, "original_model_connections_properties_restored": True,
            "platform": QApplication.platformName()}, indent=2) + "\n")


def test_readonly_generated_controls_have_real_read_grant_and_context(env):
    engine = env.attach(declarations(env, actions=False), readonly=True)
    task, grant = engine.observation_scope()
    row = env.row(grant)
    assert tuple(row["actions"]) == tuple(row["create_types"]) == ()
    assert not row["allow_activity_transfer"] and row["resources"]
    plan = env.runtime.gate.current(ContextRequest("owner", task, grant, "local:test", engine.focus_ref))
    assert plan.policy.allowed
    assert not engine.widgets["check"].isEnabled() and not engine.widgets["count"].isEnabled()
    engine.focus("edit")
    env.ready(engine)
    QTest.keyClick(engine.widgets["edit"], Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(engine.widgets["edit"], "local draft")
    assert env.row(engine.drafts["edit"])["text"] == "local draft"
    assert env.row(env.item)["title"] == "alpha" and not env.calls
    QTest.keyClick(engine.widgets["edit"], Qt.Key.Key_Return)
    assert not env.calls
    env.frame(engine)


def test_readonly_rejects_domain_actions_before_mount(env):
    engine = EngineUi(env.runtime.coordinator, env.runtime.kernel, env.runtime.index, "owner",
                      parent=env.host, embedded=True, extensions=False, readonly=True)
    with pytest.raises(UiUnavailable, match="Read-only"):
        engine.mount(declarations(env))
    assert not engine._mount_committed
    engine.dispose()
