"""Two independent synthetic components using the same extensible interaction SDK."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypedDict

from pydantic import ConfigDict, with_config
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from agent_native.kernel import CommandPermissionLimit
from agent_native.kernel.commands import Plan
from agent_native.schema import CollectionRef, EntityKey, Owned, Ref, Registry
from agent_native.state import Create, Patch, Update
from agent_native.state.tracking import FieldRef
from agent_native.ui import (
    InteractionAction, InteractionBinding, InteractionCollection, InteractionDefinition,
    InteractionNode, UiRuntime,
)


closed = with_config(ConfigDict(extra="forbid"))


@closed
class NoInput(TypedDict):
    pass


@closed
class InputText(TypedDict):
    text: str


@closed
class FormState(TypedDict):
    page: Literal["home", "form", "confirm"]
    title: str
    detail: str


@closed
class LayerSelection(TypedDict):
    layer: str


@closed
class Delta(TypedDict):
    dx: int


@closed
class LayerState(TypedDict):
    selected: str | None
    preview: int | None


def button(key, label, x, y, action, *, enabled=True, shortcut=None):
    return InteractionNode(key, "button", (x, y, 128, 34), label,
                           activate=InteractionBinding(action, enabled=enabled), shortcut=shortcut)


def label(key, caption, x=20, y=16):
    return InteractionNode(key, "label", (x, y, 550, 34), caption)


def form_definition():
    domain = Registry("example.symbiotic_form", 1)

    @domain.entity("entry")
    class Entry:
        title: str
        detail: str
        saves: int

    @domain.command("save")
    def save(ctx, entry: Ref[Entry], title: str, detail: str):
        if not title or not detail:
            raise ValueError("Require both fields")
        return Plan(Patch(updates=(Update(entry, {
            "title": title, "detail": detail, "saves": ctx.view.field(entry, "saves") + 1,
        }),)))

    entry = Ref(EntityKey(Entry.__entity_type__, "entry", 1))
    cmd = domain.command_descriptor("save").definition_id
    initial = {"page": "home", "title": "", "detail": ""}

    def transition(page):
        return lambda _, state, sources: {**state, "page": page}

    def edit(field):
        return lambda args, state, sources: {**state, field: args["text"]}

    def scene(state, sources):
        if state["page"] == "home":
            return (label("headline", f"Saved: {sources['saved']}"),
                    button("open", "Edit entry", 20, 64, "open"))
        if state["page"] == "form":
            return (
                label("headline", "Edit entry"),
                InteractionNode("title", "field", (20, 65, 510, 30), "Title", state["title"],
                                edit=InteractionBinding("title", inputs=(("value", "text"),))),
                InteractionNode("detail", "field", (20, 110, 510, 30), "Detail", state["detail"],
                                edit=InteractionBinding("detail", inputs=(("value", "text"),))),
                button("review", "Review", 20, 159, "review", enabled=bool(state["title"] and state["detail"])),
                button("cancel", "Cancel", 161, 159, "cancel", shortcut="Escape"),
            )
        return (label("headline", f"Review: {state['title']} / {state['detail']}"),
                button("save", "Save entry", 20, 64, "save"), button("back", "Back", 161, 64, "back"),
                button("cancel", "Cancel", 302, 64, "cancel", shortcut="Escape"))

    definition = InteractionDefinition(FormState, initial,
        {"saved": FieldRef(entry, "title")},
        (
            InteractionAction("open", NoInput, transition("form")),
            InteractionAction("title", InputText, edit("title")),
            InteractionAction("detail", InputText, edit("detail")),
            InteractionAction("review", NoInput, transition("confirm")),
            InteractionAction("back", NoInput, transition("form")),
            InteractionAction("cancel", NoInput, lambda *_: initial),
            InteractionAction("save", NoInput,
                              lambda _, state, sources: {"entry": entry, "title": state["title"],
                                                         "detail": state["detail"]}, command=cmd),
        ),
        scene,
    )
    patch = Patch(creates=(Create(Entry.__entity_type__, "entry",
                                  {"title": "New", "detail": "", "saves": 0}),))
    return domain, patch, definition, entry


def layers_definition():
    domain = Registry("example.symbiotic_layers", 1)

    @domain.entity("layer")
    class Layer:
        name: str
        x: int
        y: int
        locked: bool
        saves: int

    @domain.entity("document")
    class Document:
        layers: Owned[Layer]

    @domain.command("move")
    def move(ctx, layer: Ref[Layer], x: int):
        if ctx.view.field(layer, "locked") or not 20 <= x <= 410:
            raise ValueError("Layer cannot be moved there")
        return Plan(Patch(updates=(Update(layer, {
            "x": x, "saves": ctx.view.field(layer, "saves") + 1,
        }),)))

    doc = Ref(EntityKey(Document.__entity_type__, "document", 1))
    layer = Ref(EntityKey(Layer.__entity_type__, "alpha", 1))
    locked = Ref(EntityKey(Layer.__entity_type__, "beta", 1))
    cmd = domain.command_descriptor("move").definition_id

    def find(sources, key):
        return next(item for item in sources["layers"] if item["ref"]["entity_id"] == key)

    def select(args, state, sources):
        find(sources, args["layer"])
        return {"selected": args["layer"], "preview": None}

    def start(args, state, sources):
        current = find(sources, args["layer"])
        if current["locked"]:
            raise ValueError("Layer is locked")
        return {"selected": args["layer"], "preview": current["x"]}

    def preview(args, state, sources):
        current = find(sources, state["selected"])
        return {**state, "preview": max(20, min(410, current["x"] + args["dx"]))}

    def persist(_, state, sources):
        value = find(sources, state["selected"])
        return {"layer": dict(value["ref"]), "x": state["preview"]}

    def scene(state, sources):
        nodes = [label("headline", "Layers — select, move, apply or cancel")]
        selected = next((item for item in sources["layers"]
                         if item["ref"]["entity_id"] == state["selected"]), None)
        pending = state["preview"] is not None and selected is not None
        for item in sources["layers"]:
            key = item["ref"]["entity_id"]
            chosen = selected is item
            fixed = {"layer": key}
            x = state["preview"] if chosen and pending else item["x"]
            nodes.append(InteractionNode(key, "region", (x, item["y"], 120, 42), item["name"],
                activate=InteractionBinding("select", fixed, enabled=not chosen),
                drag_begin=InteractionBinding("begin", fixed, enabled=chosen and not item["locked"]),
                drag=InteractionBinding("preview", inputs=(("dx", "dx"),),
                                        enabled=chosen and pending and not item["locked"]),
                selected=chosen, outline=((8, 0), (120, 0), (120, 34), (112, 42), (0, 42), (0, 8))))
        if pending:
            nodes.extend((
                button("apply", "Apply", 20, 275, "apply",
                       enabled=not selected["locked"] and state["preview"] != selected["x"]),
                button("cancel", "Cancel preview", 162, 275, "cancel", shortcut="Escape"),
            ))
        return tuple(nodes)

    definition = InteractionDefinition(LayerState, {"selected": None, "preview": None}, {
        "layers": InteractionCollection(CollectionRef(doc.key, "layers"), ("name", "x", "y", "locked")),
    }, (
        InteractionAction("select", LayerSelection, select),
        InteractionAction("begin", LayerSelection, start),
        InteractionAction("preview", Delta, preview),
        InteractionAction("cancel", NoInput, lambda _, state, sources: {**state, "preview": None}),
        InteractionAction("apply", NoInput, persist, command=cmd),
    ), scene)
    patch = Patch(creates=(
        Create(Layer.__entity_type__, "alpha", {"name": "First layer", "x": 40, "y": 70, "locked": False, "saves": 0}),
        Create(Layer.__entity_type__, "beta", {"name": "Locked layer", "x": 200, "y": 145, "locked": True, "saves": 0}),
        Create(Document.__entity_type__, "document", {"layers": (layer, locked)}),
    ))
    return domain, patch, definition, layer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("form", "layers"))
    args = parser.parse_args()
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    build = form_definition if args.component == "form" else layers_definition
    domain, seed, definition, _ = build()
    with TemporaryDirectory() as folder:
        runtime = UiRuntime.open(registries=(domain,), path=Path(folder) / "scene.db", initial_patch=seed,
                                 command_policy=lambda req: CommandPermissionLimit(req.channel == "ui"))
        window = QWidget()
        window.setLayout(QVBoxLayout())
        window.setWindowTitle(f"Merovinger — {args.component}")
        session = runtime.attach_interaction(window, definition=definition)
        window.show()
        timer = QTimer()
        def close():
            if not window.isVisible() and session.close() and runtime.close():
                timer.stop()
                app.quit()
        timer.timeout.connect(close)
        timer.start(50)
        return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
