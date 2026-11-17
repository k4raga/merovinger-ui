"""Standalone UI + provider-neutral context/PNG + a tiny local custom agent."""
import argparse
import hashlib
import json
import platform
import tempfile
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLineEdit, QCheckBox, QListView

from agent_native.schema import Registry, Ref, EntityKey, Owned, CollectionRef
from agent_native.state import Patch, Create, Update
from agent_native.state.tracking import FieldRef
from agent_native.kernel import CommandPermissionLimit
from agent_native.kernel.commands import Plan
from agent_native.ui import UiRuntime, Action, INPUT, editor, toggle, list_view, Destination


def build_demo(path, *, readonly=False):
    domain = Registry("example.ui_agent", 1)

    @domain.entity("item")
    class Item:
        title: str

    @domain.entity("board")
    class Board:
        title: str
        enabled: bool
        items: Owned[Item]

    @domain.command("set_title")
    def set_title(ctx, board: Ref[Board], title: str):
        return Plan(Patch(updates=(Update(board, {"title": title}),)))

    @domain.command("set_enabled")
    def set_enabled(ctx, board: Ref[Board], enabled: bool):
        return Plan(Patch(updates=(Update(board, {"enabled": enabled}),)))

    board = Ref(EntityKey(Board.__entity_type__, "board", 1))
    item = Ref(EntityKey(Item.__entity_type__, "one", 1))
    commands = {name: domain.command_descriptor(name).definition_id
                for name in ("set_title", "set_enabled")}

    def policy(request):
        return CommandPermissionLimit(request.principal == "owner"
                                      and request.channel == "ui"
                                      and request.command in commands.values())

    runtime = UiRuntime.open(registries=(domain,), path=path.resolve(), principal="owner",
        command_policy=policy, initial_patch=Patch(creates=(
            Create(Item.__entity_type__, "one", {"title": "A visible item"}),
            Create(Board.__entity_type__, "board",
                   {"title": "Ready", "enabled": True, "items": (item,)}))))
    window = QWidget()
    window.setWindowTitle("Merovinger UI · one state, two views")
    window.resize(560, 400)
    layout = QVBoxLayout(window)
    title, enabled, items = QLineEdit(), QCheckBox(), QListView()
    for widget in (title, enabled, items):
        layout.addWidget(widget)
    bridge = runtime.attach(window, readonly=readonly, declarations=(
        editor("title", FieldRef(board, "title"), label="Title",
               action=None if readonly else Action(commands["set_title"], {"board": board, "title": INPUT})),
        toggle("enabled", FieldRef(board, "enabled"), label="Enabled",
               action=None if readonly else Action(commands["set_enabled"], {"board": board, "enabled": INPUT})),
        list_view("items", CollectionRef(board.key, "items"), label="Items")),
        widgets={"title": title, "enabled": enabled, "items": items})
    return SimpleNamespace(runtime=runtime, bridge=bridge, window=window, board=board,
                           commands=commands, title=title, enabled=enabled, items=items)


def local_agent(packet, command):
    """An adapter receives ready JSON+PNG; it does not use our Runner or HTTP."""
    context = json.loads(packet.context_json)
    if not context or command not in packet.context_json.decode("utf-8"):
        raise AssertionError("Typed command must be offered by the original context")
    if len(packet.images) != 1 or QImage.fromData(packet.images[0].png, "PNG").isNull():
        raise AssertionError("The agent needs one authorized PNG")
    return "enabled", False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    temporary = tempfile.TemporaryDirectory(prefix="agent-native-ui-agent-") if args.database is None else None
    demo = build_demo(args.database or Path(temporary.name) / "state.sqlite")
    demo.window.show()
    destination = Destination("local-agent", "custom-python", "owner", "example-v1")
    result = {"status": "RUNNING", "platform": platform.system(), "python": platform.python_version(),
              "provider_calls": 0, "system_profile": demo.runtime.system.profile}
    state = {"phase": "initial", "pending": None, "old": None}
    deadline = monotonic() + 60
    timer = QTimer()

    def tick():
        try:
            if state["phase"] == "closing":
                if demo.bridge.close() and demo.runtime.close():
                    result["cleanup"] = {"bridge": True, "runtime": True}
                    timer.stop()
                    demo.window.hide()
                    app.quit()
                return
            if monotonic() > deadline:
                raise TimeoutError("Local model observation did not settle")
            if demo.bridge.status.error:
                raise RuntimeError(demo.bridge.status.error)
            if state["phase"] == "initial" and demo.bridge.status.ready:
                state["pending"] = demo.bridge.observe_for_model(destination=destination)
                state["phase"] = "first"
            elif state["phase"] == "first" and state["pending"].poll():
                with state["pending"].take() as packet:
                    action = local_agent(packet, demo.commands["set_enabled"])
                    state["old"] = (packet.cut, hashlib.sha256(packet.images[0].png).hexdigest())
                    result["first_context_bytes"] = len(packet.context_json)
                state["pending"].close()
                if demo.bridge.invoke(*action) != "accepted":
                    raise AssertionError("Bound action was not accepted")
                state["phase"] = "command"
            elif state["phase"] == "command" and demo.bridge.status.ready:
                state["pending"] = demo.bridge.observe_for_model(destination=destination)
                state["phase"] = "verify"
            elif state["phase"] == "verify" and state["pending"].poll():
                with state["pending"].take() as packet:
                    if b'"enabled":false' not in packet.context_json:
                        raise AssertionError("Fresh authorized context must show the C1 result")
                    image = packet.images[0]
                    digest = hashlib.sha256(image.png).hexdigest()
                    if image.stamp.cut != packet.cut or packet.cut == state["old"][0] or digest == state["old"][1]:
                        raise AssertionError("Expected a coherent fresh frame")
                    result.update(status="PASS", second_context_bytes=len(packet.context_json),
                                  png_bytes=len(image.png), source_sequence=packet.cut.sequence,
                                  visual_status=packet.visual_status, native_widgets=["QLineEdit", "QCheckBox", "QListView"])
                state["phase"] = "closing"
        except Exception as error:
            result.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
            state["phase"] = "closing"

    timer.timeout.connect(tick)
    timer.start(5)
    app.exec()
    if temporary is not None:
        temporary.cleanup()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
