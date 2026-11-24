"""Qt adapter for the SDK's bounded semantic scene.

The component author supplies geometry and meanings. Qt painting, text widgets,
hit resolution and gesture-to-event conversion are owned by this adapter.
"""
from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, QRect, QSignalBlocker, Qt
from PySide6.QtGui import QAction, QColor, QImage, QInputMethodEvent, QKeySequence, QPainter, QPolygon, QRegion, QShortcut
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QWidget
from shiboken6 import isValid

from .engine import UiUnavailable


def _rect(node):
    return QRect(*node.rect)


def _area(node):
    if not node.outline:
        return QRegion(_rect(node))
    x, y, _, _ = node.rect
    return QRegion(QPolygon([QPoint(x + dx, y + dy) for dx, dy in node.outline]))


def input_regions(nodes):
    """One top-to-bottom hit calculation, including Qt's native child layer."""
    painted = [node for node in nodes if node.kind == "region"]
    native = [node for node in nodes if node.kind in ("button", "field")]
    covered = QRegion()
    out = {}
    for node in reversed(painted + native):
        area = _area(node)
        out[node.id] = area.subtracted(covered)
        covered = covered.united(area)
    return out


class InteractionRenderer(QWidget):
    """A mount-owned native surface. An application cannot adopt child widgets."""

    def __init__(self, session, parent):
        super().__init__(parent)
        self.session = session
        self.setObjectName("merovingerInteraction")
        self.setFixedSize(*session.definition.size)
        self._nodes = ()
        self._bindings = {}
        self._widgets = {}
        self._shortcuts = {}
        self._allowed = False
        self._disposed = False
        self._pressed = None
        self._application = QApplication.instance()
        self._application.installEventFilter(self)

    def check_topology(self):
        expected = {self, *self._widgets.values()}
        parent = self.session.parent
        if (not isValid(self) or not isValid(parent) or self.parentWidget() is not parent
                or set(parent.findChildren(QWidget)) != expected
                or set(parent.findChildren(QShortcut)) != {shortcut for _, shortcut in self._shortcuts.values()}
                or bool(parent.findChildren(QAction))
                or any(not isValid(widget) or widget.parentWidget() is not self
                       for widget in self._widgets.values())):
            raise UiUnavailable("Unregistered widget in the managed interaction surface")

    def matches(self, nodes, bindings):
        self.check_topology()
        return nodes == self._nodes and bindings == self._bindings and self._native_matches()

    def _native_matches(self):
        """Cached scene equality alone does not certify the live Qt projection."""
        if self.isHidden() or not self.isEnabled():
            return False
        for node in self._nodes:
            if node.kind not in ("button", "field"):
                continue
            widget = self._widgets.get(node.id)
            field = node.kind == "field"
            event = "edit" if field else "activate"
            if (type(widget) is not (QLineEdit if field else QPushButton)
                    or widget.geometry() != _rect(node) or widget.isHidden()
                    or widget.text() != (node.value if field else node.label)
                    or widget.accessibleName() != node.label
                    or widget.isEnabled() != (self._allowed and f"{node.id}:{event}" in self._bindings)):
                return False
            if field and (widget.isReadOnly() or widget.echoMode() != QLineEdit.EchoMode.Normal
                          or widget.completer() is not None or widget.inputMask()):
                return False
            if not field and (widget.isCheckable() or widget.menu() is not None or not widget.icon().isNull()):
                return False
        return all(shortcut.key() == QKeySequence(key)
                   and shortcut.isEnabled() == (self._allowed and slot in self._bindings)
                   for key, (slot, shortcut) in self._shortcuts.items())

    def eventFilter(self, receiver, event):
        if self._disposed or not isValid(receiver):
            return False
        parent = self.session.parent
        if not isValid(parent):
            return False
        current = receiver
        while current is not None and current is not parent:
            current = current.parent()
        if current is None:
            return False
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                            QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove, QEvent.Type.KeyPress,
                            QEvent.Type.KeyRelease, QEvent.Type.ShortcutOverride, QEvent.Type.InputMethod,
                            QEvent.Type.ContextMenu, QEvent.Type.Wheel, QEvent.Type.Shortcut):
            try:
                self.check_topology()
            except UiUnavailable:
                return True
            if not self._native_matches():
                return True
            if type(receiver) is QLineEdit and event.type() == QEvent.Type.InputMethod and event.preeditString():
                # Uncommitted IME paint is outside the semantic field model.
                # Only committed text crosses the native input boundary.
                if event.commitString() or event.replacementLength():
                    committed = QInputMethodEvent()
                    committed.setCommitString(event.commitString(), event.replacementStart(), event.replacementLength())
                    QApplication.sendEvent(receiver, committed)
                return True
            if type(receiver) is QLineEdit and event.type() == QEvent.Type.KeyPress and any(
                event.matches(kind) for kind in (QKeySequence.StandardKey.Copy, QKeySequence.StandardKey.Cut,
                                                 QKeySequence.StandardKey.Undo, QKeySequence.StandardKey.Redo)
            ):
                return True
        return False

    def _refresh_input(self):
        for key, widget in self._widgets.items():
            event = "edit" if type(widget) is QLineEdit else "activate"
            widget.setEnabled(self._allowed and f"{key}:{event}" in self._bindings)
        for _, (slot, shortcut) in self._shortcuts.items():
            shortcut.setEnabled(self._allowed and slot in self._bindings)
        self.update()

    def input_available(self, allowed):
        self._allowed = allowed
        self._refresh_input()

    def apply(self, nodes, bindings):
        self.check_topology()
        self._nodes, self._bindings = nodes, dict(bindings)
        wanted = {node.id: node.kind for node in nodes if node.kind in ("button", "field")}
        for key, widget in tuple(self._widgets.items()):
            expected = "field" if type(widget) is QLineEdit else "button"
            if wanted.get(key) != expected:
                self._widgets.pop(key)
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for node in nodes:
            if node.kind not in ("button", "field"):
                continue
            widget = self._widgets.get(node.id)
            if widget is None:
                if node.kind == "field":
                    widget = QLineEdit(self)
                    widget.setMaxLength(4096)
                    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
                    widget.setDragEnabled(False)
                    widget.textEdited.connect(lambda value, key=node.id: self._edit(key, value))
                else:
                    widget = QPushButton(self)
                    widget.clicked.connect(lambda checked=False, key=node.id: self._send(key, "activate"))
                self._widgets[node.id] = widget
                widget.setObjectName(node.id)
            with QSignalBlocker(widget):
                if node.kind == "field":
                    widget.setReadOnly(False)
                    widget.setEchoMode(QLineEdit.EchoMode.Normal)
                    if widget.completer() is not None:
                        widget.setCompleter(None)
                    if widget.inputMask():
                        widget.setInputMask("")
                else:
                    widget.setCheckable(False)
                widget.setGeometry(_rect(node))
                widget.setAccessibleName(node.label)
                text = node.value if node.kind == "field" else node.label
                if text != widget.text():
                    if node.kind == "field":
                        cursor = widget.cursorPosition()
                        widget.setText(text)
                        widget.setCursorPosition(min(cursor, len(text.encode("utf-16-le")) // 2))
                    else:
                        widget.setText(text)
                widget.show()
                widget.raise_()
        shortcuts = {node.shortcut: node.id + ":activate" for node in nodes if node.shortcut}
        for key, (slot, shortcut) in tuple(self._shortcuts.items()):
            if shortcuts.get(key) != slot:
                self._shortcuts.pop(key)
                shortcut.setEnabled(False)
                shortcut.setParent(None)
                shortcut.deleteLater()
        for key, slot in shortcuts.items():
            if key not in self._shortcuts:
                shortcut = QShortcut(QKeySequence(key), self)
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.activated.connect(lambda name=slot: self._send(name.split(":")[0], "activate"))
                self._shortcuts[key] = slot, shortcut
        self._allowed = bool(self.session.status.ready)
        self._refresh_input()

    def _edit(self, key, value):
        widget = self._widgets[key]
        original = next(node.value for node in self._nodes if node.id == key)
        cursor = widget.cursorPosition()
        # QLineEdit applies the key before emitting textEdited. The transient
        # edit is an input argument, not an authorized native publication.
        with QSignalBlocker(widget):
            widget.setText(original)
        result = self._send(key, "edit", value=value)
        if result is not None and result.accepted and self._widgets.get(key) is widget:
            widget.setCursorPosition(min(cursor, len(widget.text().encode("utf-16-le")) // 2))

    def _send(self, node, event, **data):
        self.check_topology()
        slot = f"{node}:{event}"
        entry = self._bindings.get(slot)
        if not self._allowed or entry is None or not self._native_matches():
            return None
        _, binding = entry
        result = self.session.invoke(slot, {argument: data[source] for source, argument in binding.inputs})
        if not result.accepted and event == "edit":
            self.apply(self._nodes, self._bindings)
        return result

    def _at(self, point):
        areas = input_regions(self._nodes)
        for node in reversed(self._nodes):
            if node.kind == "region" and areas[node.id].contains(point):
                return node.id
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        point = event.position().toPoint()
        node = self._at(point)
        if not self._allowed or node is None:
            return super().mousePressEvent(event)
        self._pressed = (node, point, False)
        self._send(node, "drag_begin", x=point.x(), y=point.y(), dx=0, dy=0)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._pressed is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return super().mouseMoveEvent(event)
        node, start, _ = self._pressed
        point = event.position().toPoint()
        delta = point - start
        self._pressed = (node, start, True)
        self._send(node, "drag", x=point.x(), y=point.y(), dx=delta.x(), dy=delta.y())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._pressed is None or event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        node, start, moved = self._pressed
        self._pressed = None
        point = event.position().toPoint()
        delta = point - start
        if moved or f"{node}:drag_begin" in self._bindings:
            self._send(node, "drag_end", x=point.x(), y=point.y(), dx=delta.x(), dy=delta.y())
        elif self._at(point) == node:
            self._send(node, "activate")
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self._paint_scene(painter)
        finally:
            painter.end()

    def _paint_scene(self, painter, *, include_controls=False):
        palette = self.palette()
        painter.fillRect(self.rect(), palette.window())
        for node in self._nodes:
            rect = _rect(node)
            center = int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap)
            if node.kind == "label":
                painter.setPen(palette.text().color())
                painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter) | int(Qt.TextFlag.TextWordWrap), node.label)
            elif node.kind == "region":
                painter.save()
                try:
                    painter.setClipRegion(_area(node))
                    painter.fillRect(rect, palette.highlight() if node.selected else palette.midlight())
                    painter.setPen(palette.highlightedText().color() if node.selected else palette.text().color())
                    painter.drawText(rect.adjusted(4, 4, -4, -4), center, node.label)
                finally:
                    painter.restore()
        if include_controls:
            for node in self._nodes:
                if node.kind in ("button", "field"):
                    event = "edit" if node.kind == "field" else "activate"
                    enabled = self._allowed and f"{node.id}:{event}" in self._bindings
                    rect = _rect(node)
                    painter.fillRect(rect, palette.base() if enabled else palette.midlight())
                    painter.setPen(palette.mid().color())
                    painter.drawRect(rect.adjusted(0, 0, -1, -1))
                    painter.setPen(palette.text().color() if enabled else palette.mid().color())
                    painter.drawText(rect.adjusted(3, 0, -3, 0), center,
                                     node.value if node.kind == "field" else node.label)

    def frame(self):
        self.check_topology()
        if not self._native_matches():
            raise UiUnavailable("Native controls changed outside the published interaction")
        image = QImage(self.width(), self.height(), QImage.Format.Format_RGB888)
        image.fill(QColor("white"))
        painter = QPainter(image)
        try:
            # Don't grab Qt child pixels: styles, caret/preedit and outside
            # host drawing cannot introduce bytes into the agent package.
            self._paint_scene(painter, include_controls=True)
        finally:
            painter.end()
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
            raise UiUnavailable("Could not render a coherent interaction frame")
        data = bytes(buffer.data())
        if len(data) > 2 * 1024 * 1024:
            raise UiUnavailable("Interaction frame exceeds its byte limit")
        return data

    def teardown(self):
        self._disposed = True
        if isValid(self._application):
            self._application.removeEventFilter(self)
        self.hide()
        if self.parentWidget() is not None and self.parentWidget().layout() is not None:
            self.parentWidget().layout().removeWidget(self)
        self.deleteLater()
