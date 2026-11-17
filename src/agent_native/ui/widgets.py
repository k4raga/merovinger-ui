"""Controlled native realizations. User input is copied before C1 admission."""
from PySide6.QtCore import Qt, QSignalBlocker, QEvent, QModelIndex, QAbstractListModel, QObject
from PySide6.QtGui import QInputMethodEvent, QPainter, QColor, QPen
from PySide6.QtWidgets import QLineEdit, QListView, QWidget

from .editing import units


def checked_rectangle(values):
    """Finite Qt-safe logical geometry, with nonnegative sizes.

    Canvas source bounds are semantic geometry. The simple paint uses a
    1-logical-pixel pen; pixel-policy masking must add a conservative AA guard.
    """
    if len(values)!=4 or any(type(value) not in (int,float) for value in values):
        raise ValueError("Canvas needs four direct numeric fields")
    x,y,width,height=values
    if not (-1_000_000<=x<=1_000_000 and -1_000_000<=y<=1_000_000
            and 0<=width<=1_000_000 and 0<=height<=1_000_000):
        raise ValueError("Unsupported canvas geometry range")
    return tuple(values)



def _editor_keyPressEvent(engine,path,widget,event):
    key = event.key()
    if key in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
        event.accept()
        return
    if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
        engine.cycle_focus(key == Qt.Key.Key_Backtab)
        event.accept()
        return
    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        engine.submit_editor(path)
        event.accept()
        return
    mods = event.modifiers()
    select_all = key == Qt.Key.Key_A and mods == Qt.KeyboardModifier.ControlModifier
    unsafe = mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.AltModifier)
    names = {Qt.Key.Key_Left:"left", Qt.Key.Key_Right:"right", Qt.Key.Key_Home:"home",
             Qt.Key.Key_End:"end", Qt.Key.Key_Backspace:"backspace", Qt.Key.Key_Delete:"delete"}
    if unsafe and not select_all:
        engine.refuse("unsupported_shortcut")
    else:
        engine.edit_key(path, names.get(key, "text"), event.text(),
                             bool(mods & Qt.KeyboardModifier.ShiftModifier), select_all)
    event.accept()


def _editor_inputMethodEvent(engine,path,widget,event):
    cursor = None
    for attr in event.attributes():
        if attr.type != QInputMethodEvent.AttributeType.Cursor:
            engine.refuse("unsupported_ime_attribute")
            event.accept()
            return
        cursor = attr.start
    engine.edit_ime(path, event.commitString(), event.preeditString(),
                         event.replacementStart(), event.replacementLength(), cursor)
    event.accept()


def _editor_focusOutEvent(engine,path,widget,event):
    # Stock QLineEdit may commit platform preedit on focus loss. C1 owns the
    # composition lifecycle; an unrelated native focus event cannot commit.
    QWidget.focusOutEvent(widget,event)
    widget.update()


def _editor_mousePressEvent(engine,path,widget,event):
    if event.button() == Qt.MouseButton.LeftButton:
        at = widget.cursorPositionAt(event.position().toPoint())
        engine.focus_caret(path, at, at)
    event.accept()


def _editor_mouseMoveEvent(engine,path,widget,event):
    if event.buttons() & Qt.MouseButton.LeftButton:
        values = engine.draft_values(path)
        engine.text_selection(path, widget.cursorPositionAt(event.position().toPoint()),
                                   values["anchor_utf16"])
    event.accept()


def _editor_mouseDoubleClickEvent(engine,path,widget,event):
    engine.refuse("unsupported_double_click")
    event.accept()


def _apply_editor_draft(widget,values):
    with QSignalBlocker(widget):
        QLineEdit.setText(widget, values["text"])
        widget.setSelection(values["anchor_utf16"], values["cursor_utf16"]-values["anchor_utf16"])
        attrs = [QInputMethodEvent.Attribute(QInputMethodEvent.AttributeType.Cursor,
                     values["preedit_cursor_utf16"], 1, None)]
        QLineEdit.inputMethodEvent(widget, QInputMethodEvent(values["preedit"], attrs))
        return values["preedit"]


class ControlledEditor(QLineEdit):
    def __init__(self, engine, path):
        super().__init__()
        self.engine, self.path, self.preedit = engine, path, ""
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setAcceptDrops(False)

    def keyPressEvent(self,event):
        return _editor_keyPressEvent(self.engine,self.path,self,event)

    def inputMethodEvent(self,event):
        return _editor_inputMethodEvent(self.engine,self.path,self,event)

    def focusOutEvent(self,event):
        return _editor_focusOutEvent(self.engine,self.path,self,event)

    def mousePressEvent(self,event):
        return _editor_mousePressEvent(self.engine,self.path,self,event)

    def mouseMoveEvent(self,event):
        return _editor_mouseMoveEvent(self.engine,self.path,self,event)

    def mouseDoubleClickEvent(self,event):
        return _editor_mouseDoubleClickEvent(self.engine,self.path,self,event)

    def paintEvent(self,event):
        try:
            QLineEdit.paintEvent(self,event)
        except Exception as error:
            self.engine.note_paint_error(error)

    def apply_draft(self,values):
        self.preedit = _apply_editor_draft(self,values)


class AdoptedEditor(QObject):
    """Real QLineEdit input filter using the same canonical editing policy."""
    _events = {QEvent.Type.KeyPress: _editor_keyPressEvent,
               QEvent.Type.InputMethod: _editor_inputMethodEvent,
               QEvent.Type.FocusOut: _editor_focusOutEvent,
               QEvent.Type.MouseButtonPress: _editor_mousePressEvent,
               QEvent.Type.MouseMove: _editor_mouseMoveEvent,
               QEvent.Type.MouseButtonDblClick: _editor_mouseDoubleClickEvent}

    def __init__(self,engine,path,widget):
        super().__init__(widget)
        self.engine,self.path,self.widget,self.preedit = engine,path,widget,""
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        widget.setAcceptDrops(False)
        widget.setReadOnly(False)
        widget.setPlaceholderText("")
        widget.setValidator(None)
        widget.setCompleter(None)
        widget.setInputMask("")
        widget.setMaxLength(32767)
        widget.installEventFilter(self)

    def eventFilter(self,obj,event):
        if obj is not self.widget or self.engine.disposing or self.engine.closed:
            return False
        handler = self._events.get(event.type())
        if handler is None:
            return False
        handler(self.engine,self.path,self.widget,event)
        return True

    def apply_draft(self,values):
        self.preedit = _apply_editor_draft(self.widget,values)

    def close(self):
        self.widget.removeEventFilter(self)
        self.deleteLater()


class RefListModel(QAbstractListModel):
    """Ref cache over one pinned source alias; rows never own domain records."""
    def __init__(self, engine, label_field, parent=None):
        super().__init__(parent)
        self.engine=engine
        self.label_field, self.refs, self.cut = label_field, (), None

    def apply(self, cut, refs):
        self.beginResetModel()
        self.cut, self.refs = cut, tuple(refs)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.refs)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        try:
            if not index.isValid() or not 0 <= index.row() < len(self.refs):
                return None
            ref = self.refs[index.row()]
            if role == Qt.ItemDataRole.DisplayRole:
                value=self.cut.view.field(ref,self.label_field)
                if type(value) is not str:
                    raise ValueError("List label is not a scalar string")
                return value
            if role == Qt.ItemDataRole.UserRole:
                return ref
            return None
        except Exception as error:
            self.engine.note_paint_error(error)
            return None

    def ref_at(self, index):
        return self.refs[index.row()] if index.isValid() and 0 <= index.row() < len(self.refs) else None


def _list_mousePressEvent(engine,path,widget,event):
    ref = widget.model().ref_at(widget.indexAt(event.position().toPoint()))
    engine.focus_select(path, ref)
    event.accept()


def _list_mouseMoveEvent(engine,path,widget,event):
    event.accept()


def _list_mouseDoubleClickEvent(engine,path,widget,event):
    engine.refuse("unsupported_double_click")
    event.accept()


def _list_keyPressEvent(engine,path,widget,event):
    if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
        engine.cycle_focus(event.key() == Qt.Key.Key_Backtab)
    elif event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
        current = widget.currentIndex().row()
        row = max(0, min(len(widget.model().refs)-1, current + (-1 if event.key() == Qt.Key.Key_Up else 1)))
        if widget.model().refs:
            engine.select(path, widget.model().refs[row])
    else:
        engine.refuse("unsupported_list_key")
    event.accept()


def _list_wheelEvent(engine,path,widget,event):
    engine.scroll(path, engine.scroll_value(path)-event.angleDelta().y()//3)
    event.accept()


class ControlledList(QListView):
    def __init__(self, engine, path, model):
        super().__init__()
        self.engine, self.path = engine, path
        self.setModel(model)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setAcceptDrops(False)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)

    def _scrolled(self, value):
        if not self.engine.applying:
            with QSignalBlocker(self.verticalScrollBar()):
                self.verticalScrollBar().setValue(self.engine.scroll_value(self.path))
            self.engine.scroll(self.path, value)

    def mousePressEvent(self,event):
        return _list_mousePressEvent(self.engine,self.path,self,event)

    def mouseMoveEvent(self,event):
        return _list_mouseMoveEvent(self.engine,self.path,self,event)

    def mouseDoubleClickEvent(self,event):
        return _list_mouseDoubleClickEvent(self.engine,self.path,self,event)

    def keyPressEvent(self,event):
        return _list_keyPressEvent(self.engine,self.path,self,event)

    def wheelEvent(self,event):
        return _list_wheelEvent(self.engine,self.path,self,event)


class AdoptedList(QObject):
    """Real QListView and viewport, backed only by the original RefListModel."""
    _events = {QEvent.Type.MouseButtonPress: _list_mousePressEvent,
               QEvent.Type.MouseMove: _list_mouseMoveEvent,
               QEvent.Type.MouseButtonDblClick: _list_mouseDoubleClickEvent,
               QEvent.Type.KeyPress: _list_keyPressEvent,
               QEvent.Type.Wheel: _list_wheelEvent}

    def __init__(self,engine,path,widget):
        super().__init__(widget)
        self.engine,self.path,self.widget = engine,path,widget
        widget.setModelColumn(0)
        widget.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        widget.setSelectionMode(QListView.SelectionMode.SingleSelection)
        widget.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        widget.setDragDropMode(QListView.DragDropMode.NoDragDrop)
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        widget.setAcceptDrops(False)
        self.viewport = widget.viewport()
        widget.installEventFilter(self)
        self.viewport.installEventFilter(self)
        self.slot = self._scrolled
        widget.verticalScrollBar().valueChanged.connect(self.slot)

    def _scrolled(self,value):
        if not self.engine.applying:
            with QSignalBlocker(self.widget.verticalScrollBar()):
                self.widget.verticalScrollBar().setValue(self.engine.scroll_value(self.path))
            self.engine.scroll(self.path,value)

    def eventFilter(self,obj,event):
        if self.engine.disposing or self.engine.closed or obj not in (self.widget,self.viewport):
            return False
        handler = self._events.get(event.type())
        if handler is None:
            return False
        handler(self.engine,self.path,self.widget,event)
        return True

    def close(self):
        self.widget.verticalScrollBar().valueChanged.disconnect(self.slot)
        self.widget.removeEventFilter(self)
        self.viewport.removeEventFilter(self)
        self.deleteLater()


class RefCanvas(QWidget):
    """Plain 2D source rectangles. No GPU/video/foreign/native child capture."""
    def __init__(self, engine, control):
        super().__init__()
        self.engine, self.control, self.refs, self.cut = engine, control, (), None
        self.setFixedHeight(control.height)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def apply(self, cut, refs):
        self.cut, self.refs = cut, tuple(refs)
        self.rectangles()  # Validate before any future paint can be accepted.
        self.update()

    def rectangles(self):
        if self.cut is None:
            return ()
        result = []
        for ref in self.refs:
            values = tuple(self.cut.view.field(ref, name) for name in self.control.fields)
            result.append((ref, checked_rectangle(values)))
        return tuple(result)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor("#171b20"))
            painter.setPen(QPen(QColor("#5ac88b"), 1))
            from PySide6.QtCore import QRectF
            for ref, values in self.rectangles():
                rect = QRectF(*values)
                painter.fillRect(rect, QColor("#274437"))
                painter.drawRect(rect)
                painter.drawText(rect.adjusted(6, 4, -3, -3), Qt.AlignmentFlag.AlignTop,
                                 self.cut.view.field(ref, self.control.label_field))
        except Exception as error:
            self.engine.note_paint_error(error)
        finally:
            painter.end()

    def mousePressEvent(self, event):
        from PySide6.QtCore import QRectF
        selected = None
        for ref, values in reversed(self.rectangles()):
            if QRectF(*values).contains(event.position()):
                selected = ref
                break
        self.engine.focus_select(self.control.path, selected)
        event.accept()
