"""Reversible finite Qt adoption over normal EngineUi declarations.

Exact QLabel/QPushButton/QLineEdit/QListView/QCheckBox/QSpinBox instances are
supported in QBoxLayouts. Geometry changes while attached. Native editor/list
events use the same canonical C1 editing/selection policies as generated views.
Host connections remain attached; projection may block native signals. Authors
replace old domain handlers with SAME Action bindings, never a callback catalog.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QBoxLayout, QLineEdit,
                              QListView, QCheckBox, QSpinBox, QStyledItemDelegate)
from shiboken6 import isValid


@dataclass
class _Adopted:
    widget: QWidget
    parent: QWidget
    layout: QBoxLayout
    index: int
    alignment: object
    stretch: int
    text: object
    accessible: str
    focus: object
    hidden: bool
    enabled: bool
    properties: dict = field(default_factory=dict)
    signals: list = field(default_factory=list)
    moved: bool = False


class WidgetAdoption:
    """Projection ownership only; grants and command admission stay in C1/C2."""
    types = {"text": QLabel, "button": QPushButton, "editor": QLineEdit,
             "list": QListView, "toggle": QCheckBox, "number": QSpinBox}

    def __init__(self, parent, widgets):
        if not isinstance(widgets, Mapping) or not widgets:
            raise ValueError("Use a nonempty explicit path-to-widget mapping")
        self.records, self.restored, seen = {}, False, set()
        for path, widget in widgets.items():
            if type(path) is not str or not path or type(widget) not in self.types.values():
                raise TypeError("Unsupported exact Qt adoption type")
            if not isValid(widget) or not parent.isAncestorOf(widget) or id(widget) in seen:
                raise ValueError("Adopt unique live descendants of the host parent")
            seen.add(id(widget))
            original_parent = widget.parentWidget()
            layout = original_parent.layout()
            if not isinstance(layout, QBoxLayout) or layout.indexOf(widget) < 0:
                raise ValueError("Adopt widgets from an existing direct QBoxLayout slot")
            properties = {"contextMenuPolicy": widget.contextMenuPolicy(),
                          "acceptDrops": widget.acceptDrops(),
                          "minimumSize": widget.minimumSize(), "maximumSize": widget.maximumSize()}
            text = widget.text() if not isinstance(widget, QListView) else None
            if type(widget) is QLabel:
                self._plain_label(widget)
                properties.update(textFormat=widget.textFormat(), wordWrap=widget.wordWrap())
            elif type(widget) in (QPushButton, QCheckBox):
                self._plain_button(widget)
                if type(widget) is QCheckBox:
                    properties["checked"] = widget.isChecked()
            elif type(widget) is QLineEdit:
                if widget.echoMode() != QLineEdit.EchoMode.Normal or widget.actions() or widget.isClearButtonEnabled():
                    raise ValueError("Only plain single-line text adoption is supported")
                properties.update(readOnly=widget.isReadOnly(), inputMask=widget.inputMask(),
                    maxLength=widget.maxLength(), validator=widget.validator(), completer=widget.completer(),
                    cursor=widget.cursorPosition(), selection=widget.selectionStart(),
                    selectionLength=len(widget.selectedText().encode("utf-16-le")) // 2,
                    modified=widget.isModified(), placeholderText=widget.placeholderText())
            elif type(widget) is QListView:
                if (type(widget.itemDelegate()) is not QStyledItemDelegate
                        or widget.viewport().findChildren(QWidget)):
                    raise ValueError("Lists require a standard text delegate, not embedded/custom cells")
                properties.update(model=widget.model(), selectionModel=widget.selectionModel(),
                    verticalScrollMode=widget.verticalScrollMode(), horizontalPolicy=widget.horizontalScrollBarPolicy(),
                    selectionMode=widget.selectionMode(), editTriggers=widget.editTriggers(), dragDropMode=widget.dragDropMode(),
                    modelColumn=widget.modelColumn(),
                    verticalScroll=widget.verticalScrollBar().value(), horizontalScroll=widget.horizontalScrollBar().value())
            else:
                properties.update(minimum=widget.minimum(), maximum=widget.maximum(), value=widget.value(),
                    prefix=widget.prefix(), suffix=widget.suffix(), specialValueText=widget.specialValueText(),
                    displayIntegerBase=widget.displayIntegerBase(), singleStep=widget.singleStep(),
                    wrapping=widget.wrapping(), lineReadOnly=widget.lineEdit().isReadOnly())
            if type(widget) in (QLabel, QPushButton, QCheckBox) and widget.findChildren(QWidget):
                raise ValueError("Plain adopted controls cannot contain unmanaged child widgets")
            index = layout.indexOf(widget)
            self.records[path] = _Adopted(widget, original_parent, layout, index,
                layout.itemAt(index).alignment(), layout.stretch(index), text,
                widget.accessibleName(), widget.focusPolicy(), widget.isHidden(),
                not widget.testAttribute(Qt.WidgetAttribute.WA_ForceDisabled), properties)

    @staticmethod
    def _plain_label(widget):
        pixmap = widget.pixmap()
        if widget.movie() is not None or pixmap is not None and not pixmap.isNull():
            raise ValueError("Adopted label must remain text-only PlainText")

    @staticmethod
    def _plain_button(widget):
        if not widget.icon().isNull() or (type(widget) is QPushButton and (widget.menu() is not None or widget.isCheckable())):
            raise ValueError("Only plain noncheckable QPushButton adoption is supported")
        if type(widget) is QCheckBox and widget.isTristate():
            raise ValueError("Only two-state checkbox adoption is supported")

    def validate(self, declarations):
        controls = {control.path: control for control in declarations}
        if not self.records.keys() <= controls.keys():
            raise ValueError("Every adopted widget needs a normal typed declaration")
        for path, record in self.records.items():
            control = controls[path]
            if type(record.widget) is not self.types.get(control.kind) or control.parent is not None:
                raise ValueError("Adopted control type must match a top-level typed declaration")

    def take(self, control, capture_root):
        record = self.records.get(control.path)
        if record is None:
            return None
        widget = record.widget
        if not isValid(widget) or widget.parentWidget() is not record.parent:
            raise ValueError("Adopted widget ownership changed before mount")
        record.layout.removeWidget(widget)
        widget.setParent(capture_root)
        record.moved = True
        if type(widget) is QLabel:
            widget.setTextFormat(Qt.TextFormat.PlainText)
            widget.setWordWrap(True)
        elif type(widget) in (QPushButton, QCheckBox):
            widget.setText(control.label)
        elif type(widget) is QSpinBox:
            widget.setPrefix("")
            widget.setSuffix("")
            widget.setSpecialValueText("")
            widget.setDisplayIntegerBase(10)
            widget.setWrapping(False)
            widget.setSingleStep(1)
        return widget

    def remember_selection(self,path,selection):
        self.records[path].properties["managedSelectionModel"] = selection

    def connect_signal(self, path, signal, slot):
        signal.connect(slot)
        self.records[path].signals.append((signal, slot))

    def connect(self, path, slot):
        self.connect_signal(path, self.records[path].widget.clicked, slot)

    def validate_paint(self, controls):
        for path, record in self.records.items():
            widget = record.widget
            if not isValid(widget):
                raise ValueError("Adopted widget was deleted by its host")
            if type(widget) is QLabel:
                self._plain_label(widget)
                if widget.textFormat() != Qt.TextFormat.PlainText:
                    raise ValueError("Adopted label must remain text-only PlainText")
            elif type(widget) in (QPushButton, QCheckBox):
                self._plain_button(widget)
                if widget.text() != controls[path].label:
                    raise ValueError("Adopted button presentation changed")
            elif type(widget) is QLineEdit:
                if (widget.echoMode() != QLineEdit.EchoMode.Normal or widget.actions()
                        or widget.isClearButtonEnabled() or widget.placeholderText()):
                    raise ValueError("Adopted editor presentation changed")
            elif type(widget) is QListView:
                model = widget.model()
                count = 0 if model is None else model.rowCount()
                if (type(widget.itemDelegate()) is not QStyledItemDelegate
                        or widget.modelColumn() != 0 or count > 4096
                        or widget.selectionModel() is not record.properties.get("managedSelectionModel")
                        or widget.itemDelegateForColumn(0) is not None
                        or any(widget.itemDelegateForRow(row) is not None for row in range(count))
                        or widget.viewport().findChildren(QWidget)):
                    raise ValueError("Adopted list delegate/cell presentation changed")
            elif type(widget) is QSpinBox:
                if (widget.prefix() or widget.suffix() or widget.specialValueText()
                        or widget.displayIntegerBase() != 10 or not widget.lineEdit().isReadOnly()):
                    raise ValueError("Adopted number presentation changed")

    def restore(self):
        if self.restored:
            return
        for record in sorted(self.records.values(), key=lambda value: (id(value.layout), value.index)):
            widget, properties = record.widget, record.properties
            for signal, slot in record.signals:
                if isValid(widget):
                    signal.disconnect(slot)
            record.signals.clear()
            if not record.moved:
                continue
            if not isValid(widget) or not isValid(record.parent) or not isValid(record.layout):
                raise RuntimeError("Host destroyed an adopted control before detach")
            with QSignalBlocker(widget):
                widget.hide()
                widget.setParent(record.parent)
                record.layout.insertWidget(record.index, widget, record.stretch, record.alignment)
                widget.setAccessibleName(record.accessible)
                widget.setFocusPolicy(record.focus)
                widget.setEnabled(record.enabled)
                widget.setContextMenuPolicy(properties["contextMenuPolicy"])
                widget.setAcceptDrops(properties["acceptDrops"])
                widget.setMinimumSize(properties["minimumSize"])
                widget.setMaximumSize(properties["maximumSize"])
                if type(widget) is QListView:
                    with QSignalBlocker(widget.verticalScrollBar()), QSignalBlocker(widget.horizontalScrollBar()):
                        widget.setModel(properties["model"])
                        auto_selection = widget.selectionModel()
                        original_selection = properties["selectionModel"]
                        if original_selection is not None:
                            widget.setSelectionModel(original_selection)
                        for selection in (properties.get("managedSelectionModel"), auto_selection):
                            if selection is not None and selection is not original_selection and isValid(selection):
                                selection.deleteLater()
                        widget.setModelColumn(properties["modelColumn"])
                        widget.setVerticalScrollMode(properties["verticalScrollMode"])
                        widget.setHorizontalScrollBarPolicy(properties["horizontalPolicy"])
                        widget.setSelectionMode(properties["selectionMode"])
                        widget.setEditTriggers(properties["editTriggers"])
                        widget.setDragDropMode(properties["dragDropMode"])
                        widget.verticalScrollBar().setValue(properties["verticalScroll"])
                        widget.horizontalScrollBar().setValue(properties["horizontalScroll"])
                elif type(widget) is QLineEdit:
                    widget.setInputMask(properties["inputMask"])
                    widget.setMaxLength(properties["maxLength"])
                    widget.setValidator(properties["validator"])
                    widget.setCompleter(properties["completer"])
                    widget.setReadOnly(properties["readOnly"])
                    widget.setPlaceholderText(properties["placeholderText"])
                    widget.setText(record.text)
                    cursor, start, length = properties["cursor"], properties["selection"], properties["selectionLength"]
                    anchor = cursor if start < 0 else start + length if cursor == start else start
                    widget.setSelection(anchor, cursor - anchor)
                    widget.setModified(properties["modified"])
                elif type(widget) is QSpinBox:
                    widget.setRange(properties["minimum"], properties["maximum"])
                    for name in ("prefix","suffix","specialValueText","displayIntegerBase","singleStep","wrapping"):
                        getattr(widget, "set" + name[0].upper() + name[1:])(properties[name])
                    widget.setValue(properties["value"])
                    widget.lineEdit().setReadOnly(properties["lineReadOnly"])
                else:
                    widget.setText(record.text)
                    if type(widget) is QLabel:
                        widget.setTextFormat(properties["textFormat"])
                        widget.setWordWrap(properties["wordWrap"])
                    elif type(widget) is QCheckBox:
                        widget.setChecked(properties["checked"])
                widget.setVisible(not record.hidden)
            record.moved = False
        self.restored = True
