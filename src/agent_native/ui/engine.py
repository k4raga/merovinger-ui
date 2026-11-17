"""Finite managed Qt Widgets adapter over the ONE C1/C2 authority.

QWidgets, Ref models and NodeMappings are disposable projections. All semantic
draft/focus/selection/menu/scroll writes are memory-only Coordinator intentions.
Production capture/policy/asset ownership is intentionally outside S1-015.
"""
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import current_thread, main_thread
from uuid import uuid4
import hashlib
from typing import get_origin, get_args, Annotated
import weakref
from shiboken6 import isValid

from PySide6.QtCore import Qt, QEvent, QPoint, QRect, QSignalBlocker, QItemSelectionModel
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QSpinBox,
                              QFrame, QApplication, QSizePolicy, QLayout, QCheckBox)
from agent_native.context import resolve_view_bindings, resource_record
from agent_native.schema import Ref, EntityKey, CollectionRef
from agent_native.state import Create, Update, Patch, thaw
from agent_native.state.tracking import FieldRef, ReadTracker
from agent_native.kernel import PreparedChange, Status
from agent_native.kernel.coordinator import Phase, CoordinatorBusy

from .declarations import Control, INPUT, argument_resources
from .editing import key_edit, ime_edit, validated, units, position
from .widgets import ControlledEditor, ControlledList, RefListModel, RefCanvas
from .session import UiSessionBroker, GuiPump


class UiUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InputIntent:
    kind: str
    path: str
    payload: tuple
    target: Ref
    version: int
    focus_revision: int | None = None
    focus_version: int | None = None
    cache_incarnations: tuple = ()


@dataclass(frozen=True, slots=True)
class PaintProvenance:
    """Conservative source metadata for C4; this is not an export permission."""
    kind: str
    source: Ref | CollectionRef | None
    fields: tuple[str, ...] = ()
    semantic_ref: Ref | None = None
    semantic_fields: tuple[str, ...] = ()
    author_literal: str | None = None


@dataclass(frozen=True, slots=True)
class NodeMapping:
    visual_id: tuple
    binding: Ref | None
    source: Ref | CollectionRef | None
    parent: Ref | None
    rect: tuple
    clipped_rect: tuple
    transform: tuple
    dpr: float
    z_order: int
    rendered: bool
    provenance: tuple[PaintProvenance, ...]


@dataclass(frozen=True, slots=True)
class UiAppliedCut:
    identity: object
    view: Ref
    surface_id: str
    surface_generation: int
    layout_epoch: int
    mappings: tuple[NodeMapping, ...]
    presentation: str
    realization_fingerprint: str
    cache_incarnations: tuple
    derived_revision: int
    surface_kind: str
    omitted_categories: tuple[str, ...]


class _Root(QWidget):
    def __init__(self, engine, parent=None):
        super().__init__(parent, Qt.WindowType.Widget if parent is not None else Qt.WindowType.Window)
        self.engine = engine

    def closeEvent(self, event):
        if self.engine.closed or not self.engine._mount_committed:
            event.accept()
            return
        broker = getattr(self.engine,"broker",None)
        if (self.engine.coordinator.phase != Phase.IDLE or broker is not None and broker.holding
                or self.engine.inputs or self.engine.conflicted_inputs):
            self.engine.refuse("close_waits_for_ack")
            event.ignore()
            return
        self.engine.request_close()
        event.accept() if self.engine.closed else event.ignore()

    def resizeEvent(self,event):
        if hasattr(self.engine,"capture_root"):
            self.engine.allocate_shell()
        QWidget.resizeEvent(self,event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.engine.cycle_focus(event.key() == Qt.Key.Key_Backtab)
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.engine.close_menus()
            event.accept()
        else:
            super().keyPressEvent(event)


class EngineUi:
    """One mounted finite view. Build on the Qt/process main thread.

    The caller supplies the SAME ContextIndex/SYS definitions already installed
    before StateStore/Kernel. mount() creates ordinary memory SYS bindings from
    normal declarations; there is no application context setter. The host must
    install an explicit CommandKernel command_policy for automatic UI grants;
    SAME registered command profiles declare effects/Create/transfer ceilings.

    embedded=True creates an owned child panel under parent, with inherited host
    styling and no top-level window. The host lays out root and must close/dispose
    this view before destroying its parent. Optional widgets adopt exact existing
    QLabel/QPushButton/QLineEdit/QListView/QCheckBox/QSpinBox instances; dispose
    restores their QBoxLayout placement and
    presentation. Geometry changes while attached. External host signals remain
    connected; the author replaces old domain handlers with SAME Action bindings.
    extensions=False omits the optional extension projection; UI-only system
    definitions also select this capability off. It changes no grants.
    readonly=True disallows domain Actions, while canonical local Draft/focus/
    selection edits remain available. Action-free views receive a genuine C2
    read grant with no commands/effects/creation/transfer authority.
    """
    def __init__(self, coordinator, kernel, index, principal, *, input_limit=32,
                 parent=None, embedded=False, widgets=None, extensions=True, readonly=False):
        self.assert_owner()
        if QApplication.instance() is None:
            raise RuntimeError("Create QApplication on the main thread first")
        if any(type(value) is not bool for value in (embedded, extensions, readonly)):
            raise TypeError("Use exact boolean UI options")
        if embedded:
            if not isinstance(parent, QWidget) or not isValid(parent):
                raise ValueError("Embedded UI requires a live host QWidget parent")
        elif parent is not None or widgets is not None:
            raise ValueError("Parent/widget adoption requires embedded=True")
        self.embedded, self.readonly = embedded, readonly
        self.extensions = extensions and getattr(index.system, 'supports_extensions', True)
        self._input_adapters = {}
        self._host_parent = parent
        self._embedded_disposed = False
        self._adoption = None
        if widgets is not None:
            from .embedding import WidgetAdoption
            self._adoption = WidgetAdoption(parent, widgets)
        if type(input_limit) is not int or input_limit < 1:
            raise ValueError("A bounded input queue is required")
        self.coordinator, self.kernel, self.index = coordinator, kernel, index
        self.ui, self.principal = index.ui, principal
        if self.extensions:
            index.bind_extensions(kernel)
        self.input_limit, self.inputs = input_limit, deque()
        self.conflicted_inputs = deque()
        self._input_lineage, self._scroll_corrections, self._input_stamps = {}, {}, {}
        self._focus_draft = None
        self.controls, self.widgets, self.models = {}, {}, {}
        self._derived_panels = {}
        self.extension_program_runtime = None
        self.labels = {}
        self.paint_error = None
        self.health_fault = None
        self.closed = False
        self.disposing = False
        self._mount_committed, self._mount_failed = False, False
        self._mount_status = None
        self.paint_error_generation = 0
        self.bindings, self.states, self.drafts, self.selections = {}, {}, {}, {}
        self.declared_sources = {}
        self.displayed = None
        self.rows = {}
        self.applying, self.dirty = False, True
        self.layout_epoch, self.surface_generation = 0, 1
        self.surface_id = "managed-content-"+uuid4().hex
        self.known_widgets = set()
        self.last_outcome, self.accepted_inputs, self.conflicts = "new", 0, 0
        self.root = _Root(self, parent)
        if not self.embedded:
            self.root.setWindowTitle("Agent Native · Managed UI")
        self.root.setAccessibleName("Управляемый интерфейс Agent Native")
        if not self.embedded:
            self.root.setMinimumSize(480,500)
            self.root.resize(720,980)
        self.capture_root = QWidget(self.root)
        self.capture_root.setObjectName("managedContent")
        self.chrome = QWidget(self.root)
        self.chrome.setObjectName("diagnosticChrome")
        self.layout = QVBoxLayout(self.capture_root)
        self.layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.layout.setContentsMargins(20, 18, 20, 18)
        self.layout.setSpacing(9)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.status = QLabel("Инициализация",self.chrome)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(False)
        self.status.setAccessibleName("Состояние движка")
        if not self.embedded:
            self.root.setStyleSheet("""
            QWidget { background:#20252b; color:#e4e9e6; font-size:14px; }
            QLineEdit,QListView,QSpinBox { background:#171b20; border:1px solid #3c4842;
                border-radius:5px; padding:6px; selection-background-color:#35754f; }
            QPushButton { background:#2c3932; border:1px solid #446950; border-radius:5px; padding:7px; }
            QPushButton:hover { background:#36543f; } QPushButton:disabled { color:#8b958e; }
            QFrame#menuPanel { border:1px solid #3c4842; border-radius:5px; }
            QScrollBar:vertical { width:13px; background:#171b20; }
            QScrollBar::handle:vertical { background:#52765f; min-height:20px; }
        """)
        # A QObject event filter is hosted by the root; EngineUi is not a QObject model.
        from PySide6.QtCore import QObject
        engine = self
        class Watch(QObject):
            def eventFilter(self, obj, event):
                # QObject child teardown continues after capture_root's C++
                # object was deleted. Never dereference a stale Qt wrapper.
                if (engine.closed or engine.disposing or not isValid(engine.capture_root)
                        or not isValid(obj)):
                    return False
                in_content=(obj is engine.capture_root or
                    isinstance(obj,QWidget) and engine.capture_root.isAncestorOf(obj))
                if in_content and event.type() in (QEvent.Type.Resize,QEvent.Type.Move,QEvent.Type.LayoutRequest,
                                    QEvent.Type.StyleChange,QEvent.Type.FontChange,QEvent.Type.Polish,
                                    QEvent.Type.Show,QEvent.Type.Hide,QEvent.Type.ChildAdded,QEvent.Type.ChildRemoved):
                    engine.layout_epoch += 1
                return False
        self.watch = Watch(self.root)
        self.allocate_shell()

    @staticmethod
    def assert_owner():
        if current_thread() is not main_thread():
            raise RuntimeError("Qt UI belongs to the process main thread")

    def allocate_shell(self):
        """Only public window dimensions determine this fixed surface allocation."""
        self.assert_owner()
        width,height=self.root.width(),self.root.height()
        band=84
        self.capture_root.setGeometry(0,0,width,max(0,height-band))
        self.chrome.setGeometry(0,max(0,height-band),width,min(band,height))
        self.status.setGeometry(20,4,max(0,width-40),30)
        if hasattr(self,"conflict_button"):
            self.conflict_button.setGeometry(20,40,max(0,width-40),30)

    def bind_program_runtime(self, runtime):
        """Use the SAME installed runtime for native saved-action invocation."""
        if runtime.kernel is not self.kernel or runtime.origin.principal != self.principal:
            raise ValueError("Extensions require SAME kernel/principal")
        self.extension_program_runtime = runtime
        self.dirty = True

    def _extension_request(self):
        from agent_native.programming.policy import active_request
        return active_request(self.kernel)

    def _invoke_extension(self, activation, arguments, source_run=None):
        if self.coordinator.phase != Phase.IDLE or self.applying:
            self.refuse("extension_runtime_busy")
            return
        try:
            return self._invoke_extension_ready(activation, arguments, source_run)
        except (TypeError, ValueError, LookupError):
            self.refuse("extension_unavailable")

    def _invoke_extension_ready(self, activation, arguments, source_run=None):
        request = self._extension_request()
        runtime = self.extension_program_runtime
        if (runtime is None or not runtime.available
                or runtime.run is not None and not runtime.done
                or getattr(self, "_extension_pending_native_control", None) is not None):
            self.refuse("extension_runtime_busy")
            return
        host = self.kernel.authenticated(self.principal)
        lineage = None
        with self.coordinator.read() as cut:
            if (hasattr(self.kernel.system, "SavedAction")
                    and activation.key.entity_type == self.kernel.system.SavedAction.__entity_type__):
                from agent_native.actions.runtime import resolve_action
                activation, arguments, lineage = resolve_action(self.kernel, cut, host, activation, arguments)
            from agent_native.extensions.lifecycle import owned
            row, definition = owned(self.kernel, cut.view, host, activation)
            if definition["kind"] not in ("action", "view"):
                raise ValueError("extension_action_required")
            ceiling = row["installed_ceiling"]
        options = {"activation": activation, "arguments": arguments, "source_run": source_run,
            "lineage": lineage, "request_id": "native-" + uuid4().hex}
        if request is not None:
            return self._start_native_extension(options, request.task, request.grant)
        from agent_native.programming.policy import configured_policy
        if (configured_policy(self.kernel, self.principal) is None
                or not self.broker.ready or self.broker.holding):
            raise ValueError("extension_policy_unavailable")
        task = self.broker.task
        commands = tuple(ceiling["actions"])
        if not commands:
            return self._start_native_extension(options, task, None)
        # Existing UI task + SAME command policy/profile resolver. Guest data
        # cannot widen the installed ceiling or choose the caller principal.
        from agent_native.programming.bindings import _resource
        resources = tuple(_resource(x) for x in ceiling["resources"])
        granted = self.kernel.issue_command_grant(host, task, channel="ui", commands=commands, resources=resources)
        pending = {**options, "task": task, "grant": granted.ref, "terminal": None}
        self._extension_pending_native_control = pending
        submission = granted.submission
        if submission.status in (Status.COMMITTED, Status.FAILED):
            pending["terminal"] = submission
        elif submission.status in (Status.AWAITING_DB, Status.RECONCILING):
            def complete(result):
                if (getattr(self, "_extension_pending_native_control", None) is pending
                        and result.operation_id == submission.operation_id):
                    pending["terminal"] = result
            self.pump.driver.turn.watch_operation(self.pump.registration, submission.operation_id, complete)
        else:
            self._extension_pending_native_control = None
            self.refuse("extension_caller_scope")

    def _start_native_extension(self, options, task, grant):
        from agent_native.extensions.lifecycle import invoke
        from agent_native.kernel import _command_values as values
        if grant is not None:
            with self.coordinator.read() as cut:
                row = cut.view.resolve(options["activation"]).values
                caller = self.kernel.grant_authority.check_use(cut.view, grant, principal=self.principal)
                ceiling = row["installed_ceiling"]
                if (not caller.allowed or not set(ceiling["actions"]) <= set(cut.view.field(grant, "actions"))
                        or not set(ceiling["create_types"]) <= set(cut.view.field(grant, "create_types"))
                        or not {values.record_identity(x) for x in ceiling["resources"]} <= {values.record_identity(x) for x in caller.resources}):
                    raise ValueError("extension_caller_scope")
        installed = invoke(self.kernel, self.kernel.authenticated(self.principal), options["activation"],
            task=task, request_id=options["request_id"], arguments=options["arguments"],
            input_lineage=options["lineage"], source_run=options["source_run"])
        self._extension_native_pending = installed.run
        return installed.submission

    def _advance_native_extension(self):
        pending = getattr(self, "_extension_pending_native_control", None)
        if pending is None: return False
        terminal = pending["terminal"]
        if terminal is not None and terminal.status == Status.FAILED:
            self._extension_pending_native_control = None
            self.refuse("extension_caller_scope")
            return True
        with self.coordinator.read() as cut:
            committed = cut.view.exists(pending["grant"])
        if not committed: return True
        self._extension_pending_native_control = None
        try:
            self._start_native_extension(pending, pending["task"], pending["grant"])
        except (TypeError, ValueError, LookupError):
            self.refuse("extension_unavailable")
        return True

    def _sync_extensions(self, cut):
        from agent_native.extensions.views import native_components
        from agent_native.extensions.native import DerivedPanel
        values = native_components(self.kernel, cut, principal=self.principal, request=self._extension_request())
        fresh = {value["descriptor"]["activation"]: value for value in values}
        changed = False
        for activation, panel in tuple(self._derived_panels.items()):
            if activation not in fresh or panel.publication != fresh[activation]:
                self.layout.removeWidget(panel)
                panel.hide()
                panel.setParent(None)
                panel.deleteLater()
                del self._derived_panels[activation]
                changed = True
        for activation, value in fresh.items():
            if activation not in self._derived_panels:
                panel = DerivedPanel(value, cut.view, self._invoke_extension, self.capture_root)
                self.layout.addWidget(panel)
                self._derived_panels[activation] = panel
                panel.show()
                changed = True
        if changed:
            self.known_widgets = {self.root, *self.root.findChildren(QWidget)}
        pending = getattr(self, "_extension_native_pending", None)
        runtime = self.extension_program_runtime
        if pending is not None and runtime is not None and cut.view.exists(pending):
            if runtime.available and (runtime.run is None or runtime.done):
                runtime.attach(pending)
                self._extension_native_pending = None

    def command_descriptor(self, command):
        return self.index.commands[command][1]

    def _ref(self, model, suffix):
        return Ref(EntityKey(model.__entity_type__, self.surface_id+"-"+suffix, 1))

    def _annotation(self,entity_type,name):
        registry=self.index.sources.owners[entity_type]
        annotation=dict(registry._definition("entity",entity_type).annotations)[name]
        while get_origin(annotation) is Annotated:
            annotation=get_args(annotation)[0]
        return annotation

    def _validate_control_source(self,c):
        if c.kind in ("text","editor","number","toggle"):
            expected=bool if c.kind=="toggle" else int if c.kind=="number" else str
            if self._annotation(c.source.ref.key.entity_type,c.source.name) is not expected:
                raise UiUnavailable("Unsupported composite/scalar control source")
        elif c.kind in ("list","canvas"):
            owner=c.source.owner.entity_type
            descriptor=self.index.sources.owners[owner].entity_descriptor(owner)
            relation=next(f.relation for f in descriptor.fields if f.name==c.source.field)
            if relation is None or self._annotation(relation.target_type,c.label_field) is not str:
                raise UiUnavailable("List/canvas labels require a direct string field")
            if c.kind=="canvas" and (len(c.fields)!=4 or any(
                    self._annotation(relation.target_type,name) not in (int,float) for name in c.fields)):
                raise UiUnavailable("Canvas requires four direct numeric fields")

    def mount(self, declarations):
        self.assert_owner()
        if self.controls or self.closed:
            raise RuntimeError("A view is mounted once")
        try:
            return self._mount(declarations)
        except BaseException as error:
            if not self._mount_committed:
                if hasattr(self,"pump"):
                    self.pump._abandon_unmounted()
                # The constructor-owned native shell predates this attempt;
                # hide it, retain no driver callback and leave deletion to its owner.
                self.closed, self.disposing = True, True
                self.root.hide()
            else:
                # Canonical mount exists or may exist. Keep an inactive owned facade
                # for an explicit later close; never report a physical rollback.
                self._mount_failed = True
                self.health_fault = "mount:"+type(error).__name__
                self.pump.stop()
                self.dirty = True
            raise

    def _mount(self, declarations):
        self.assert_owner()
        if self.controls:
            raise RuntimeError("A view is mounted once")
        declarations = tuple(declarations)
        if self.readonly and any(type(c) is Control and c.action is not None for c in declarations):
            raise UiUnavailable("Read-only views cannot bind domain Actions")
        if self._adoption is not None:
            self._adoption.validate(declarations)
        if not declarations or any(type(c) is not Control for c in declarations):
            raise TypeError("Use finite managed declarations")
        for c in declarations:
            self._validate_control_source(c)
            if c.path in self.controls:
                raise ValueError("Duplicate binding path")
            self.controls[c.path] = c
            self.bindings[c.path] = self._ref(self.ui.Binding, c.path)
        creates, resources, commands = [], set(), set()
        self.view_ref, self.focus_ref = self._ref(self.ui.EngineView,"view"), self._ref(self.ui.ContextFocus,"focus")
        with self.coordinator.read() as cut:
            for c in declarations:
                parent = self.bindings[c.parent] if c.parent else None
                source = c.source.ref if type(c.source) is FieldRef else c.source
                field = c.source.name if type(c.source) is FieldRef else None
                role = "menu" if c.kind == "menu" else "collection" if c.kind in ("list","canvas") else "command" if c.kind == "button" else "value"
                values = {"path":c.path,"role":role,"source":None if source is None else resource_record(source),
                    "field":field,"command_id":None if c.action is None else c.action.command,
                    "parent":None if parent is None else resource_record(parent),
                    "label":c.label,"visible":c.visible,"enabled":c.enabled}
                self.declared_sources[c.path] = self.index.sources.resolve(values)
                creates.append(Create(self.ui.Binding.__entity_type__,self.bindings[c.path].key.entity_id,values,"memory"))
                if source is not None:
                    resources.add(source)
                    if type(source) is CollectionRef:
                        resources.add(Ref(source.owner))
                if c.action is not None:
                    descriptor = self.command_descriptor(c.action.command)
                    # Legacy hints only assert the SAME registered profile.
                    # They never contribute to issuance or command authority.
                    if (c.action.create_types and set(c.action.create_types) !=
                            set(descriptor.command_permissions.create_types)):
                        raise UiUnavailable("Action creation assertion differs from command profile")
                    commands.add(descriptor.definition_id)
                    argument_seeds=argument_resources(c.action.arguments)
                    for seed in argument_seeds:
                        self.index.sources.source(seed)  # Same registered/private source rules.
                    resources.update(argument_seeds)
                if c.kind in ("menu","list","canvas"):
                    state = self.states[c.path] = self._ref(self.ui.UiState,"state-"+c.path)
                    creates.append(Create(state.key.entity_type,state.key.entity_id,{"binding":self.bindings[c.path]},"memory"))
                if c.kind == "editor":
                    draft = self.drafts[c.path] = self._ref(self.ui.Draft,"draft-"+c.path)
                    initial = cut.view.field(c.source.ref,c.source.name)
                    creates.append(Create(draft.key.entity_type,draft.key.entity_id,
                        {"binding":self.bindings[c.path],"text":initial,"cursor_utf16":units(initial),"anchor_utf16":units(initial)},"memory"))
                elif c.kind != "text":
                    selection = self.selections[c.path] = self._ref(self.ui.Selection,"selection-"+c.path)
                    creates.append(Create(selection.key.entity_type,selection.key.entity_id,
                        {"binding":self.bindings[c.path]},"memory"))
        creates.extend((
            Create(self.view_ref.key.entity_type,self.view_ref.key.entity_id,
                   {"principal":self.principal,"bindings":tuple(self.bindings.values()),"states":tuple(self.states.values())},"memory"),
            Create(self.focus_ref.key.entity_type,self.focus_ref.key.entity_id,
                   {"principal":self.principal,"view":self.view_ref},"memory")))
        self.pump = GuiPump(self)
        def commit_mount():
            result = self.coordinator.submit("ui-mount-"+uuid4().hex,
                lambda managed: PreparedChange(Patch(creates=tuple(creates))),memory_only=True)
            # Pin disposition BEFORE critical AFTER can fail. Uncertain/queued
            # is never treated as a definitive pre-commit rollback either.
            self._mount_status = result.status
            self._mount_committed = result.status not in (Status.FAILED, Status.BUSY)
            return result
        result = self.pump.driver.turn.dispatch(self.pump.registration,commit_mount)
        if result.status != Status.COMMITTED:
            raise UiUnavailable("Mount requires an idle coherent coordinator")
        self.resource_seeds = tuple(sorted(resources,key=repr))
        with self.coordinator.read() as resource_cut:
            resources = self.command_resources(resource_cut)
        self.broker = UiSessionBroker(self,resources,tuple(sorted(commands)))
        containers = {None:self.layout}
        for c in declarations:
            if c.parent is not None and c.parent not in containers:
                raise ValueError("Parent menu must precede its children")
            layout = containers[c.parent]
            adopted = None if self._adoption is None else self._adoption.take(c, self.capture_root)
            if c.kind == "text":
                widget = adopted if adopted is not None else QLabel()
                widget.setTextFormat(Qt.TextFormat.PlainText)
                widget.setWordWrap(True)
            elif c.kind == "editor":
                widget = adopted if adopted is not None else ControlledEditor(self,c.path)
                if adopted is not None:
                    from .widgets import AdoptedEditor
                    self._input_adapters[c.path] = AdoptedEditor(self,c.path,widget)
            elif c.kind == "button":
                widget = adopted if adopted is not None else QPushButton(c.label)
                slot = lambda checked=False,path=c.path:self.invoke(path)
                if adopted is None:
                    widget.clicked.connect(slot)
                else:
                    self._adoption.connect(c.path, slot)
            elif c.kind == "toggle":
                widget = adopted if adopted is not None else QCheckBox(c.label)
                slot = lambda checked=False,path=c.path:self.boolean(path,checked)
                if adopted is not None:
                    self._adoption.connect_signal(c.path, widget.clicked, slot)
                else:
                    widget.clicked.connect(slot)
            elif c.kind == "number":
                widget = adopted if adopted is not None else QSpinBox()
                widget.setRange(c.minimum,c.maximum)
                widget.lineEdit().setReadOnly(True)  # Finite stepper; free text uses editor.
                slot = lambda value,path=c.path:self.numeric(path,value)
                if adopted is not None:
                    self._adoption.connect_signal(c.path, widget.valueChanged, slot)
                else:
                    widget.valueChanged.connect(slot)
            elif c.kind == "list":
                model = self.models[c.path] = RefListModel(self,c.label_field,self.root)
                widget = adopted if adopted is not None else ControlledList(self,c.path,model)
                if adopted is not None:
                    from .widgets import AdoptedList
                    widget.setModel(model)
                    self._adoption.remember_selection(c.path,widget.selectionModel())
                    self._input_adapters[c.path] = AdoptedList(self,c.path,widget)
                widget.setFixedHeight(c.height)
            elif c.kind == "canvas":
                widget = RefCanvas(self,c)
            else:
                widget = QFrame()
                widget.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Maximum)
                widget.setObjectName("menuPanel")
                inner = QVBoxLayout(widget)
                toggle = QPushButton(c.label)
                toggle.clicked.connect(lambda checked=False,path=c.path:self.toggle_menu(path))
                inner.addWidget(toggle)
                containers[c.path] = inner
                widget.toggle = toggle
            widget.setAccessibleName(c.label or c.path)
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.widgets[c.path] = widget
            if c.label and c.kind not in ("text","button","toggle","menu"):
                caption = self.labels[c.path] = QLabel(c.label)
                caption.setTextFormat(Qt.TextFormat.PlainText)
                layout.addWidget(caption)
            layout.addWidget(widget)
        self.layout.addStretch(1)
        self.conflict_button = QPushButton("Отклонить конфликтный ввод",self.chrome)
        self.conflict_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.conflict_button.clicked.connect(self.discard_conflicts)
        self.conflict_button.hide()
        self.allocate_shell()
        self.known_widgets = {self.root,*self.root.findChildren(QWidget)}
        for widget in self.known_widgets:
            widget.installEventFilter(self.watch)
        owner=weakref.ref(self)
        def published(cut):
            engine=owner()
            if engine is not None:
                engine.dirty=True
        self.coordinator.subscribe(published)
        self.refresh()
        # Task/grant bootstrap is the next shared participant step, never a
        # second C1 action on the mount stack.
        self.pump.start()
        return self.root

    def observation_scope(self):
        """Current real task/read-or-command grant; ContextExportGate still authorizes."""
        self.assert_owner()
        if self.closed or self.coordinator.phase != Phase.IDLE or not self.broker.ready:
            raise UiUnavailable("Observation requires a ready idle UI session")
        task, grant = self.broker.task, self.broker.grant
        with self.coordinator.read() as cut:
            if task is None or grant is None or not cut.view.exists(task):
                raise UiUnavailable("UI observation scope is unavailable")
            row = cut.view.resolve(task).values
            if (row["principal"] != self.principal or row["state"] != "active"
                    or row["generation"] != 1 or not self.kernel.grant_authority.check_use(
                        cut.view, grant, principal=self.principal).allowed):
                raise UiUnavailable("UI observation grant is no longer current")
        return task, grant

    def command_resources(self,cut):
        """Bounded original Ref closure required by C2 full normalized footprint.

        This grants declared UI sources to the local UI session; it never adds
        roots to ContextIndex or copies values into a UI/agent data model.
        """
        self.assert_owner()
        resources,visited=set(),set()
        def visit(value):
            if type(value) is CollectionRef:
                resources.add(value)
                visit(Ref(value.owner))
                for member in cut.view.resolve_collection(value).members:
                    visit(member)
            elif type(value) is Ref:
                resources.add(value)
                if value in visited:
                    return
                if len(visited)>=256:
                    raise UiUnavailable("UI resource footprint limit")
                visited.add(value)
                for name, field in cut.view.resolve(value).values.items():
                    # SAME structural predicate as C2. Only the exact scalar,
                    # registered engine backlink may be omitted; identical
                    # private Ref values elsewhere remain ordinary closure.
                    if self.kernel.grant_authority.check_activity_backlink(cut.view, value, name, field) is not None:
                        continue
                    visit(field)
            elif isinstance(value,Mapping):
                for item in value.values():
                    visit(item)
            elif isinstance(value,(tuple,list)) or type(value).__name__=="FrozenList":
                for item in value:
                    visit(item)
        for source in self.resource_seeds:
            visit(source)
        return tuple(sorted(resources,key=repr))

    def attach_session(self, task):
        result = self.coordinator.submit("ui-attach-"+uuid4().hex,
            lambda view:PreparedChange(Patch(updates=(Update(self.focus_ref,{"task":task}),))),memory_only=True)
        if result.status != Status.COMMITTED:
            raise UiUnavailable("Session focus attach failed")
        self.dirty = True
        return result

    def set_outcome(self, value):
        self.assert_owner()
        self.last_outcome = value
        captions={
            "input_committed":"Готово", "accepted_command":"Сохраняем изменение…",
            "queued_input":"Ввод ожидает подтверждения", "input_busy":"Очередь ввода заполнена",
            "busy":"Ожидаем завершения операции", "input_conflict":"Ввод конфликтует с новым состоянием",
            "unresolved_input_conflict":"Сначала отклоните конфликтный ввод",
            "conflicts_discarded":"Конфликтный ввод отклонён", "committed_local":"Изменение сохранено",
            "close_waits_for_ack":"Закрытие ждёт подтверждения или решения конфликта",
            "view_closing":"Окно закрывается", "basis_expired":"Данные устарели. Повторите действие",
            "display_conflict":"Интерфейс обновился. Повторите действие", "composition_active":"Завершите ввод текста",
            "ui_unhealthy":"Требуется восстановить представление", "gui_pump_failed":"Движок ожидает восстановления",
            "projection_input_blocked":"Обновляем интерфейс", "ui_applying":"Обновляем интерфейс",
        }
        self.status.setText(captions.get(value,"Состояние: "+value))
        if hasattr(self,"conflict_button"):
            self.conflict_button.setVisible(bool(self.conflicted_inputs))

    def refuse(self, reason):
        self.set_outcome(reason)
        if "conflict" in reason:
            self.conflicts += 1
        return reason

    def refresh(self):
        self.assert_owner()
        if self.coordinator.phase!=Phase.IDLE:
            return
        if hasattr(self,"broker") and self.broker.holding:
            return
        cut = self.coordinator.read()
        if self.matches_cut(self.displayed,cut) and not self.dirty:
            cut.close()
            return
        try:
            self.apply_snapshot(cut)
            # Projection is pure. A native range clamp is not canonical until
            # its separately admitted one-step correction commits.
            self._scroll_corrections.clear()
            for path,c in self.controls.items():
                if c.kind=="list" and self.widgets[path].verticalScrollBar().value()!=self.rows[path].scroll_y:
                    target=self.states[path]
                    self._scroll_corrections[path]=InputIntent("scroll",path,
                        (self.widgets[path].verticalScrollBar().value(),),target,
                        cut.view.resolve(target).version,cache_incarnations=self._cache_stamp(cut))
        except BaseException:
            cut.close()
            raise

    def _validate_topology(self,topology):
        rows={row.source.path:row for row in topology.rows}
        if set(rows)!=set(self.controls):
            raise UiUnavailable("Unsupported declaration topology change")
        for path,row in rows.items():
            expected=self.declared_sources[path]
            if row.binding!=self.bindings[path] or any(getattr(row.source,key)!=getattr(expected,key)
                    for key in ("source","field","role","path","command_id","parent","label")):
                raise UiUnavailable("Immutable binding declaration changed")
        return rows

    def apply_snapshot(self,cut):
        self.assert_owner()
        try:
            if self.coordinator.phase!=Phase.IDLE:
                raise UiUnavailable("Projection requires idle authority")
            with self.coordinator.capture("ui-apply-"+self.surface_id) as guard:
                if not self.matches_cut(guard,cut):
                    raise UiUnavailable("Projection requires the exact current cut")
                self._apply_snapshot(cut)
        except BaseException as error:
            self.health_fault="apply:"+type(error).__name__
            self.dirty=True
            raise

    def _apply_snapshot(self, cut):
        self.assert_owner()
        if not self.coordinator.validate_cut(cut):
            raise UiUnavailable("Foreign or expired cut")
        with ReadTracker(cut.view) as reads:
            topology = resolve_view_bindings(reads.view,self.view_ref,self.index.sources)
        if topology.principal != self.principal:
            raise UiUnavailable("View principal mismatch")
        try:
            rows = self._validate_topology(topology)
        except BaseException as error:
            self.health_fault="apply:"+type(error).__name__
            self.dirty=True
            raise
        old, self.applying = self.displayed, True
        blockers = [QSignalBlocker(widget) for widget in self.known_widgets]
        blockers.extend(QSignalBlocker(widget.selectionModel()) for path,widget in self.widgets.items()
                        if self.controls[path].kind=="list")
        try:
            self.displayed, self.rows = cut, rows
            for path,c in self.controls.items():
                self._validate_control_source(c)
                widget, row = self.widgets[path], rows[path]
                widget.setVisible(topology.state == "open" and row.effective_visible)
                widget.setEnabled(row.effective_enabled)
                if path in self.labels:
                    self.labels[path].setVisible(topology.state=="open" and row.effective_visible)
                if c.kind == "text":
                    widget.setText(cut.view.field(row.source.source,row.source.field))
                elif c.kind == "editor":
                    self._input_adapters.get(path,widget).apply_draft(validated(cut.view.resolve(self.drafts[path]).values))
                elif c.kind == "toggle":
                    value = cut.view.field(row.source.source,row.source.field)
                    if type(value) is not bool:
                        raise UiUnavailable("Unsupported boolean value")
                    widget.setChecked(value)
                    widget.setEnabled(row.effective_enabled and c.action is not None)
                elif c.kind == "number":
                    value = cut.view.field(row.source.source,row.source.field)
                    if type(value) is not int or not c.minimum <= value <= c.maximum:
                        raise UiUnavailable("Unsupported numeric value")
                    widget.setValue(value)
                    widget.setEnabled(row.effective_enabled and c.action is not None)
                elif c.kind in ("list","canvas"):
                    refs = cut.view.resolve_collection(row.source.source).members
                    if c.kind == "list":
                        widget.model().apply(cut,refs)
                        selected = cut.view.field(self.selections[path],"resources")
                        from agent_native.context import identity
                        chosen = identity(selected[0]) if selected else None
                        index = widget.model().index(refs.index(chosen),0) if chosen in refs else widget.model().index(-1,0)
                        widget.selectionModel().setCurrentIndex(index,QItemSelectionModel.SelectionFlag.ClearAndSelect)
                        widget.verticalScrollBar().setValue(row.scroll_y)
                        widget.viewport().update()
                    else:
                        widget.apply(cut,refs)
                elif c.kind == "menu":
                    widget.toggle.setText(("▾ " if row.opened else "▸ ")+c.label)
            focus = cut.view.resolve(self.focus_ref).values
            target = focus["draft"] or focus["selection"]
            focused = None
            if target is not None:
                binding = cut.view.field(target,"binding")
                focused = next((p for p,b in self.bindings.items() if b == binding and rows[p].effective_visible),None)
            if focused is not None:
                widget = self.widgets[focused]
                (widget.toggle if self.controls[focused].kind=="menu" else widget).setFocus(Qt.FocusReason.OtherFocusReason)
            elif self.root.focusWidget() is not None:
                self.root.focusWidget().clearFocus()
            if self.extensions:
                self._sync_extensions(cut)
            self.layout.activate()
            focus_record = cut.view.resolve(self.focus_ref)
            self._focus_draft = focus_record.values["draft"]
            self._input_stamps = {target:(cut.view.resolve(target).version,
                cut.view.resolve(target).values.get("focus_revision"),focus_record.version)
                for target in (*self.drafts.values(),*self.selections.values(),*self.states.values())}
            self.dirty = False
        except BaseException as error:
            self.health_fault="apply:"+type(error).__name__
            self.dirty=True
            raise
        finally:
            del blockers
            self.applying = False
            if old is not None and old is not cut:
                old.close()

    def _intent(self, kind, path, payload, target):
        self.assert_owner()
        if self.applying:
            return self.refuse("projection_input_blocked")
        if not self.pump.active:
            return self.refuse("busy")
        if self.broker.stage in ("closing","closed"):
            return self.refuse("view_closing")
        if self.health_fault is not None or self.paint_error is not None:
            return self.refuse("ui_unhealthy")
        if self.displayed is None or path not in self.rows:
            return self.refuse("unavailable")
        row = self.rows[path]
        if self.conflicted_inputs:
            return self.refuse("unresolved_input_conflict")
        if not row.effective_visible or not row.effective_enabled:
            return self.refuse("hidden_or_disabled")
        stamp = self._input_stamps.get(target)
        if stamp is None:
            return self.refuse("display_expired")
        version, token, focus_version = stamp
        editing = kind in ("key","ime","caret")
        if editing and self._focus_draft != target:
            return self.refuse("editor_not_focused")
        event = InputIntent(kind,path,tuple(payload),target,version,token,
                            focus_version if editing else None,self._cache_stamp(self.displayed))
        lineage = self._input_lineage.get(target)
        if lineage is not None and event.version == lineage[1]:
            # A newly typed event may join an existing queue after our own
            # earlier edit was projected; this is not a rebase over a foreign edit.
            event = replace(event,version=lineage[0])
        if self.coordinator.phase != Phase.IDLE or self.broker.holding or self.inputs:
            if len(self.inputs)+len(self.conflicted_inputs) >= self.input_limit:
                return self.refuse("input_busy")
            self.inputs.append(event)
            self.accepted_inputs += 1
            self.set_outcome("queued_input")
            return "queued"
        try:
            result = self.pump.dispatch(self._apply_intent,event)
        except CoordinatorBusy:
            if len(self.inputs)+len(self.conflicted_inputs)>=self.input_limit:
                return self.refuse("input_busy")
            self.inputs.append(event)
            self.accepted_inputs += 1
            self.set_outcome("queued_input")
            return "queued"
        if result == "committed":
            self.refresh()
        return result

    def _apply_intent(self, event, *, expected_version=None, already_accepted=False):
        self.assert_owner()
        scroll_value = None
        if event.kind=="scroll":
            scroll_value=max(0,min(self.widgets[event.path].verticalScrollBar().maximum(),event.payload[0]))
        with self.coordinator.read() as current:
            if event.cache_incarnations != self._cache_stamp(current):
                self.conflicted_inputs.append(event)
                return self.refuse("input_conflict")
        def prepare(view):
            record = view.resolve(event.target)
            if record.version != (event.version if expected_version is None else expected_version):
                raise UiUnavailable("input_conflict")
            if event.focus_version is not None:
                owner = view.resolve(self.focus_ref)
                if owner.version != event.focus_version or owner.values["draft"] != event.target:
                    raise UiUnavailable("focus_owner_conflict")
            if event.focus_revision is not None and record.values["focus_revision"] != event.focus_revision:
                raise UiUnavailable("focus_conflict")
            topology = resolve_view_bindings(view,self.view_ref,self.index.sources)
            self._validate_topology(topology)
            row = next(r for r in topology.rows if r.binding == self.bindings[event.path])
            if not row.effective_visible or not row.effective_enabled:
                raise UiUnavailable("visibility_conflict")
            values, kind, payload = thaw(record.values), event.kind, event.payload
            updates = []
            if kind == "key":
                changed = key_edit(values,payload[0],payload[1],shift=payload[2],select_all=payload[3])
            elif kind == "ime":
                changed = ime_edit(values,*payload)
            elif kind == "caret":
                position(values["text"],payload[0]);position(values["text"],payload[1])
                changed = {**values,"cursor_utf16":payload[0],"anchor_utf16":payload[1]}
            elif kind in ("focus", "focus_caret"):
                previous = view.field(self.focus_ref,"draft")
                if previous is not None and previous != self.drafts.get(event.path):
                    revision=view.field(previous,"focus_revision")
                    updates.append(Update(previous,{"focus_revision":revision+1,"preedit":"","composing":False,
                        "preedit_cursor_utf16":0,"preedit_anchor_utf16":0}))
                changed = {**values}
                if event.path in self.drafts:
                    changed.update(focus_revision=values["focus_revision"]+1,preedit="",composing=False,
                                   preedit_cursor_utf16=0,preedit_anchor_utf16=0)
                if kind == "focus_caret":
                    position(values["text"],payload[0]);position(values["text"],payload[1])
                    changed.update(cursor_utf16=payload[0],anchor_utf16=payload[1])
                updates.append(Update(self.focus_ref,{"draft":self.drafts.get(event.path),
                    "selection":self.selections.get(event.path)}))
            elif kind in ("select", "focus_select"):
                if kind == "focus_select":
                    previous = view.field(self.focus_ref,"draft")
                    if previous is not None:
                        revision = view.field(previous,"focus_revision")
                        updates.append(Update(previous,{"focus_revision":revision+1,"preedit":"","composing":False,
                            "preedit_cursor_utf16":0,"preedit_anchor_utf16":0}))
                source = self.controls[event.path].source
                if payload[0] not in view.resolve_collection(source).members:
                    raise UiUnavailable("selection_conflict")
                changed = {**values,"resources":(resource_record(payload[0]),)}
                updates.append(Update(self.focus_ref,{"draft":None,"selection":event.target}))
            elif kind == "menu":
                changed = {**values,"opened":not values["opened"]}
            elif kind == "scroll":
                changed = {**values,"scroll_y":scroll_value}
            else:
                raise ValueError("Unsupported intention")
            updates.append(Update(event.target,changed))
            return PreparedChange(Patch(updates=tuple(updates)))
        result = self.coordinator.submit("ui-input-"+uuid4().hex,prepare,memory_only=True)
        if result.status != Status.COMMITTED:
            if len(self.conflicted_inputs) < self.input_limit:
                self.conflicted_inputs.append(event)
            return self.refuse("input_conflict" if result.status == Status.FAILED else "input_busy")
        if not already_accepted:
            self.accepted_inputs += 1
        self.set_outcome("input_committed")
        return "committed"

    def drain_one_input(self):
        """Persistent own lineage; exactly one retained event per shared step."""
        self.assert_owner()
        if not self.inputs or self.coordinator.phase != Phase.IDLE or self.broker.holding:
            return None
        event = self.inputs.popleft()
        start,current = self._input_lineage.get(event.target,(event.version,event.version))
        if event.version != start:
            self.conflicted_inputs.append(event)
            outcome = self.refuse("input_conflict")
        else:
            outcome = self._apply_intent(event,expected_version=current,already_accepted=True)
        if outcome == "committed":
            with self.coordinator.read() as cut:
                current = cut.view.resolve(event.target).version
            self._input_lineage[event.target]=(start,current)
        else:
            self._input_lineage[event.target]=(start,-1)
        if not self.inputs:
            self._input_lineage.clear()
        return outcome

    def drain_inputs(self):
        """Compatibility one-step boundary; never batch retained mutations."""
        return self.pump.dispatch(self.drain_one_input)

    def step_once(self):
        self.assert_owner()
        if self.coordinator.phase == Phase.IDLE and self._advance_native_extension():
            return
        if self.broker.stage not in ("ready", "failed"):
            return self.broker.step_once()
        if self.health_fault is not None or self.paint_error is not None:
            return None
        if self.inputs:
            return self.drain_one_input()
        if self._scroll_corrections and self.broker.ready:
            path = next(iter(self._scroll_corrections))
            return self._apply_intent(self._scroll_corrections.pop(path))
        return None

    def request_close(self):
        self.assert_owner()
        try:
            if self._mount_failed:
                # A committed mount that failed before bootstrap has no task.
                # Its exact issued registration can close ONLY its owned UI rows.
                return self.pump.driver.turn.dispatch(self.pump.registration,self.complete_close)
            return self.pump.dispatch(self.broker.close)
        except CoordinatorBusy:
            return self.refuse("close_waits_for_ack")

    def discard_conflicts(self):
        """Explicitly abandon retained input; never replay it on a new basis."""
        self.assert_owner()
        self.conflicted_inputs.clear()
        self.set_outcome("conflicts_discarded")

    def set_domain_busy(self,busy):
        self.assert_owner()
        for path,c in self.controls.items():
            if c.kind in ("button","number"):
                self.widgets[path].setEnabled(not busy and self.rows[path].effective_enabled)

    def draft_values(self,path):
        self.assert_owner()
        return self.displayed.view.resolve(self.drafts[path]).values

    def edit_key(self,path,key,text="",shift=False,select_all=False):
        self.assert_owner()
        if units(text)>4096:
            return self.refuse("input_payload_limit")
        return self._intent("key",path,(key,text,shift,select_all),self.drafts[path])

    def edit_ime(self,path,commit,preedit,start,length,cursor=None):
        self.assert_owner()
        if units(commit)+units(preedit)>4096:
            return self.refuse("input_payload_limit")
        return self._intent("ime",path,(commit,preedit,start,length,cursor),self.drafts[path])

    def text_selection(self,path,cursor,anchor):
        self.assert_owner()
        return self._intent("caret",path,(cursor,anchor),self.drafts[path])

    def focus_caret(self,path,cursor,anchor):
        return self._intent("focus_caret",path,(cursor,anchor),self.drafts[path])

    def focus_select(self,path,ref):
        # The old focus then select gesture is one patch, including departure
        # from an editor's composition owner. Plain select keeps its old API.
        return self.focus(path) if ref is None else self._intent("focus_select",path,(ref,),self.selections[path])

    def focus(self,path):
        self.assert_owner()
        return self._intent("focus",path,(),self.drafts.get(path) or self.selections[path])

    def cycle_focus(self,backwards=False):
        self.assert_owner()
        paths = [p for p,c in self.controls.items() if c.kind!="text" and
                 self.rows[p].effective_visible and self.rows[p].effective_enabled]
        if not paths:
            return
        current = self.root.focusWidget()
        active = next((p for p in paths if current is not None and (self.widgets[p] == current or
                       self.widgets[p].isAncestorOf(current))),None)
        at = paths.index(active) if active in paths else (-1 if not backwards else 0)
        self.focus(paths[(at+(-1 if backwards else 1))%len(paths)])

    def select(self,path,ref):
        self.assert_owner()
        return self._intent("select",path,(ref,),self.selections[path])

    def scroll_value(self,path):
        self.assert_owner()
        return self.rows[path].scroll_y

    def scroll(self,path,value):
        self.assert_owner()
        return self._intent("scroll",path,(int(value),),self.states[path])

    def toggle_menu(self,path):
        self.assert_owner()
        return self._intent("menu",path,(),self.states[path])

    def close_menus(self):
        self.assert_owner()
        try:
            result = self.pump.dispatch(self._close_menus)
        except CoordinatorBusy:
            return self.refuse("menu_busy")
        if result == "committed":
            self.refresh()
        return result

    def _close_menus(self):
        self.assert_owner()
        if self.health_fault is not None:
            return self.refuse("ui_unhealthy")
        if self.coordinator.phase != Phase.IDLE or self.broker.holding:
            return self.refuse("menu_busy")
        updates = tuple(Update(ref,{"opened":False}) for path,ref in self.states.items()
                        if self.controls[path].kind=="menu")
        result = self.coordinator.submit("ui-close-menus-"+uuid4().hex,
            lambda view:PreparedChange(Patch(updates=updates)),memory_only=True)
        return result.status.value

    def invoke(self,path,value=None):
        self.assert_owner()
        if self.applying:
            return self.refuse("ui_applying")
        try:
            result = self.pump.dispatch(self._invoke,path,value)
        except CoordinatorBusy:
            return self.refuse("busy")
        return result

    def _invoke(self,path,value=None):
        self.assert_owner()
        if self.readonly or self.controls[path].action is None:
            return self.refuse("readonly_view" if self.readonly else "control_has_no_action")
        if self.inputs or self.conflicted_inputs or self._scroll_corrections:
            return self.refuse("busy")
        if self.applying:
            return self.refuse("ui_applying")
        if self.health_fault is not None or self.paint_error is not None:
            return self.refuse("ui_unhealthy")
        row = self.rows[path]
        if not row.effective_visible or not row.effective_enabled:
            return self.refuse("hidden_or_disabled")
        return self.broker.invoke(self.controls[path].action,value)

    def submit_editor(self,path):
        self.assert_owner()
        def admitted():
            if self.inputs or self.conflicted_inputs:
                return self.refuse("busy")
            if self.controls[path].action is None:
                return self.refuse("editor_has_no_submit_command")
            values=self.draft_values(path)  # Canonical read only AFTER BEFORE/recheck.
            if values["composing"]:
                return self.refuse("composition_active")
            return self._invoke(path,values["text"])
        try:
            result=self.pump.dispatch(admitted)
        except CoordinatorBusy:
            return self.refuse("busy")
        return result

    def numeric(self,path,value):
        self.assert_owner()
        if self.applying:
            return
        c = self.controls[path]
        with QSignalBlocker(self.widgets[path]):
            self.widgets[path].setValue(self.displayed.view.field(c.source.ref,c.source.name))
        if c.action is None:
            return self.refuse("readonly_number")
        return self.invoke(path,int(value))

    def boolean(self,path,value):
        self.assert_owner()
        if self.applying:
            return
        control = self.controls[path]
        with QSignalBlocker(self.widgets[path]):
            self.widgets[path].setChecked(self.displayed.view.field(control.source.ref,control.source.name))
        if control.action is None:
            return self.refuse("readonly_boolean")
        return self.invoke(path,bool(value))

    def prepare_capture(self):
        """IDLE preflight. Polish/layout may change epoch; no render is done.

        C4 takes its guard AFTER this bounded preflight and calls the pure
        realization_snapshot(cut) before/after render to validate exact pairing.
        """
        self.assert_owner()
        if self.coordinator.phase != Phase.IDLE:
            raise UiUnavailable("Preflight requires idle authority")
        self.root.ensurePolished()
        content_widgets=(self.capture_root,*self.capture_root.findChildren(QWidget))
        for widget in content_widgets:
            widget.ensurePolished()
        # Finite synchronous stabilization; still only a readiness hint.
        # Hidden/lazy Qt internals may mutate on first render: C4 must reject
        # that attempt and retry outside its guard, never waive postchecks.
        for _ in range(2):
            for widget in reversed(content_widgets):
                if widget.layout() is not None:
                    widget.layout().activate()
        with self.coordinator.read() as cut:
            return self.realization_snapshot(cut)

    def stable_ui_applied_cut(self):
        """Backward-compatible IDLE preflight alias."""
        return self.prepare_capture()

    def realization_snapshot(self,guarded_cut):
        """Pure realization read in IDLE or under an existing CAPTURING guard.

        Does not polish, activate layouts, publish, render or yield. A caller
        must compare complete before/after metadata and keep its C2 guard.
        """
        self.assert_owner()
        if self.coordinator.phase not in (Phase.IDLE,Phase.CAPTURING):
            raise UiUnavailable("UI snapshot requires an idle/capture phase")
        if (self.broker.holding or self.dirty or self.applying or self.inputs or self.conflicted_inputs
                or self._scroll_corrections or self.paint_error is not None or self.health_fault is not None):
            raise UiUnavailable("UI cut is not stable")
        if not self.coordinator.validate_cut(guarded_cut) or not self.coordinator.validate_cut(self.displayed):
            raise UiUnavailable("UI lease expired or foreign")
        if not self.matches_cut(guarded_cut,self.displayed):
            raise UiUnavailable("UI is behind the supplied graph/derived cut")
        with self.coordinator.read() as current:
            if not self.matches_cut(current,guarded_cut):
                raise UiUnavailable("Supplied graph/derived cut is not current")
        with ReadTracker(guarded_cut.view) as reads:
            topology=resolve_view_bindings(reads.view,self.view_ref,self.index.sources)
        self._validate_topology(topology)
        if self.closed or topology.state!="open":
            raise UiUnavailable("Closed managed surface")
        if not isValid(self.root):
            raise UiUnavailable("Managed surface was destroyed by its host")
        if self.embedded and (not isValid(self._host_parent)
                or self.root.parentWidget() is not self._host_parent or self.root.isWindow()):
            raise UiUnavailable("Embedded surface was detached from its host")
        if self._adoption is not None:
            try:
                self._adoption.validate_paint(self.controls)
            except (ValueError, RuntimeError) as error:
                raise UiUnavailable(str(error)) from error
        expected=QRect(0,0,self.root.width(),max(0,self.root.height()-84))
        if (self.capture_root.parentWidget() is not self.root or self.chrome.parentWidget() is not self.root
                or self.capture_root.geometry()!=expected
                or self.chrome.geometry()!=QRect(0,max(0,self.root.height()-84),self.root.width(),min(84,self.root.height()))
                or self.status.parentWidget() is not self.chrome or self.conflict_button.parentWidget() is not self.chrome
                or any(not self.capture_root.isAncestorOf(w) for w in (*self.widgets.values(),*self.labels.values()))):
            raise UiUnavailable("Managed-content allocation changed")
        actual = {self.root,*self.root.findChildren(QWidget)}
        if any(widget.graphicsEffect() is not None
               for widget in (self.capture_root,*self.capture_root.findChildren(QWidget))):
            raise UiUnavailable("Unsupported content graphics effect")
        if actual != self.known_widgets or any(w.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
                                               for w in actual if w is not self.root):
            raise UiUnavailable("Unmanaged/native child surface")
        result = []
        root_rect = self.capture_root.rect()
        dpr = self.capture_root.devicePixelRatioF()
        def rects(widget,local=None):
            local = widget.rect() if local is None else local
            rect = QRect(widget.mapTo(self.capture_root,local.topLeft()),local.size())
            clipped = rect.intersected(root_rect).intersected(QRect(widget.mapTo(self.capture_root,QPoint(0,0)),widget.size()))
            parent = widget.parentWidget()
            while parent is not None and parent is not self.capture_root:
                region = QRect(parent.mapTo(self.capture_root,QPoint(0,0)),parent.size())
                clipped = clipped.intersected(region)
                parent = parent.parentWidget()
            return rect,clipped
        def provenance(path,source,slot):
            c=self.controls[path]
            membership=() if type(c.source) is not CollectionRef else (
                PaintProvenance("membership",c.source),)
            if slot=="row":
                return (*membership,PaintProvenance("field",source,(c.label_field,)))
            if slot=="rectangle":
                return (*membership,PaintProvenance("field",source,(*c.fields,c.label_field)))
            if c.kind=="editor":
                return (PaintProvenance("draft",source,(c.source.name,),self.drafts[path],("text","preedit")),)
            if c.kind=="toggle":
                return (PaintProvenance("field",source,(c.source.name,)),
                        PaintProvenance("literal",None,author_literal=c.label))
            if c.kind in ("text","number"):
                return (PaintProvenance("field",source,(c.source.name,)),)
            if c.kind in ("menu","button"):
                return (PaintProvenance("literal",source,author_literal=c.label),)
            return membership
        def mapped(path,widget,source,slot,local=None):
            row = self.rows[path]
            rect,clip = rects(widget,local)
            key = source.key if type(source) is Ref else source.owner if type(source) is CollectionRef else None
            result.append(NodeMapping((self.view_ref.key,self.bindings[path].key,path,key,slot),
                row.binding,source,row.source.parent,rect.getRect(),clip.getRect(),
                (1,0,0,1,0,0),dpr,len(result),row.effective_visible and not clip.isEmpty(),
                provenance(path,source,slot)))
        for path,c in self.controls.items():
            self._validate_control_source(c)
            widget,row = self.widgets[path],self.rows[path]
            expected_enabled = row.effective_enabled and (c.kind not in ("toggle","number") or c.action is not None)
            if widget.isVisibleTo(self.capture_root)!=row.effective_visible or widget.isEnabled()!=expected_enabled:
                raise UiUnavailable("Unmanaged visibility/enabled realization")
            if c.kind=="text" and widget.text()!=self.displayed.view.field(row.source.source,row.source.field):
                raise UiUnavailable("Unmanaged widget value")
            if c.kind=="editor":
                values=validated(self.draft_values(path))
                selection_start=widget.selectionStart()
                anchor=widget.cursorPosition() if selection_start<0 else (
                    selection_start+units(widget.selectedText()) if widget.cursorPosition()==selection_start else selection_start)
                if (widget.text()!=values["text"] or widget.cursorPosition()!=values["cursor_utf16"]
                        or anchor!=values["anchor_utf16"] or self._input_adapters.get(path,widget).preedit!=values["preedit"]):
                    raise UiUnavailable("Unmanaged editor value")
            if c.kind=="toggle" and widget.isChecked()!=self.displayed.view.field(row.source.source,row.source.field):
                raise UiUnavailable("Unmanaged boolean value")
            if c.kind=="number" and widget.value()!=self.displayed.view.field(row.source.source,row.source.field):
                raise UiUnavailable("Unmanaged numeric value")
            mapped(path,widget,row.source.source,"control")
            if path in self.labels:
                caption=self.labels[path]
                rect,clip=rects(caption)
                result.append(NodeMapping((self.view_ref.key,self.bindings[path].key,path,"caption"),
                    row.binding,None,row.source.parent,rect.getRect(),clip.getRect(),(1,0,0,1,0,0),
                    dpr,len(result),row.effective_visible and not clip.isEmpty(),
                    (PaintProvenance("literal",None,author_literal=c.label),)))
            if c.kind=="list":
                refs=self.displayed.view.resolve_collection(row.source.source).members
                if widget.verticalScrollBar().value()!=row.scroll_y:
                    raise UiUnavailable("Unmanaged scroll value")
                if widget.model() is not self.models[path] or tuple(refs)!=widget.model().refs:
                    raise UiUnavailable("Unmanaged list projection")
                selected = self.displayed.view.field(self.selections[path],"resources")
                from agent_native.context import identity
                expected_selected = identity(selected[0]) if selected else None
                if widget.model().ref_at(widget.currentIndex()) != expected_selected:
                    raise UiUnavailable("Unmanaged list selection")
                for i,ref in enumerate(refs):
                    mapped(path,widget.viewport(),ref,"row",widget.visualRect(widget.model().index(i,0)))
            elif c.kind=="canvas":
                from PySide6.QtCore import QRectF
                for ref,values in widget.rectangles():
                    mapped(path,widget,ref,"rectangle",QRectF(*values).toAlignedRect())
        if self.extensions:
            from agent_native.extensions.views import native_components
            current_views = {v["descriptor"]["activation"]: v for v in native_components(
                self.kernel, guarded_cut, principal=self.principal, request=self._extension_request())}
            if set(current_views) != set(self._derived_panels):
                raise UiUnavailable("Derived publication changed")
            for activation, panel in self._derived_panels.items():
                if panel.publication != current_views[activation] or not panel.matches():
                    raise UiUnavailable("Derived realization changed")
                rect, clip = rects(panel)
                result.append(NodeMapping((self.view_ref.key, activation.key, "extension"),
                    None, activation, None, rect.getRect(), clip.getRect(), (1,0,0,1,0,0),
                    dpr, len(result), panel.isVisibleTo(self.capture_root) and not clip.isEmpty(),
                    (PaintProvenance("extension", activation),)))
        def actual_tree(widget):
            data=[type(widget).__name__,widget.objectName(),widget.geometry().getRect(),
                  widget.isVisibleTo(self.capture_root),widget.isEnabled(),widget.hasFocus(),widget.font().toString(),
                  widget.styleSheet(),widget.palette().cacheKey(),widget.style().objectName()]
            if hasattr(widget,"text") and callable(widget.text):
                data.append(widget.text())
            editor_path = next((path for path,value in self.widgets.items()
                                if value is widget and self.controls[path].kind=="editor"),None)
            if editor_path is not None:
                data.extend((widget.cursorPosition(),widget.selectionStart(),widget.selectedText(),
                             self._input_adapters.get(editor_path,widget).preedit))
            if isinstance(widget,QCheckBox):
                data.append(widget.isChecked())
            if any(value is widget and self.controls[path].kind=="list" for path,value in self.widgets.items()):
                data.extend((widget.verticalScrollBar().value(),widget.verticalScrollBar().maximum()))
            return (tuple(data),tuple(actual_tree(child) for child in widget.children() if isinstance(child,QWidget)))
        fingerprint=hashlib.sha256(repr((actual_tree(self.capture_root),self.paint_error_generation)).encode()).hexdigest()
        return UiAppliedCut(self.displayed.identity,self.view_ref,self.surface_id,self.surface_generation,
            self.layout_epoch,tuple(result),"visible_unverified" if self.root.isVisible() else "rendered_offscreen",fingerprint,self._cache_stamp(guarded_cut),guarded_cut.derived_revision,"managed-content",("diagnostic-chrome",))

    def matches_cut(self,left,right):
        """Exact live issued graph AND complete derived metadata comparison.

        QueryKey equality preserves typed parameters. QueryVersion includes both
        cache incarnation and counter; string labels are not query identities.
        """
        self.assert_owner()
        try:
            if not self.coordinator.validate_cut(left) or not self.coordinator.validate_cut(right):
                return False
            return (left.identity==right.identity and left.derived_revision==right.derived_revision
                and {key:entry.version for key,entry in left.derived_entries.items()}==
                    {key:entry.version for key,entry in right.derived_entries.items()})
        except Exception:
            return False

    @staticmethod
    def _cache_stamp(cut):
        return tuple(sorted(((str(key),entry.version.cache_incarnation) for key,entry in cut.derived_entries.items())))

    def complete_close(self):
        self.assert_owner()
        result=self.coordinator.submit("ui-close-"+uuid4().hex,
            lambda view:PreparedChange(Patch(updates=(Update(self.view_ref,{"state":"closed"}),
                Update(self.focus_ref,{"view":None,"draft":None,"selection":None,"task":None})))),memory_only=True)
        if result.status!=Status.COMMITTED:
            raise UiUnavailable("Memory close failed")
        self.closed=True
        self.surface_generation+=1
        if isValid(self.root):
            self.root.close()

    def note_paint_error(self,error):
        """Qt can swallow Python virtual-method exceptions; C4 sees this fault."""
        self.assert_owner()
        self.paint_error=type(error).__name__
        self.health_fault="paint:"+type(error).__name__
        self.paint_error_generation+=1

    def dispose(self):
        """Release projection leases after the host has settled durable work."""
        self.assert_owner()
        broker = getattr(self,"broker",None)
        if self.coordinator.phase != Phase.IDLE or broker is not None and broker.holding or not self.closed:
            raise UiUnavailable("Close and settle/reconcile durable work before disposal")
        self.disposing=True
        for widget in tuple(self.known_widgets):
            if isValid(widget) and isValid(self.watch):
                widget.removeEventFilter(self.watch)
        if hasattr(self,"pump"):
            self.pump.dispose()
        if self.displayed is not None:
            self.displayed.close()
            self.displayed=None
        if self.embedded and not self._embedded_disposed:
            # The host owns its top-level window and all nonadopted controls.
            # Remove only our managed child panel before returning borrowed widgets.
            if isValid(self.root):
                self.root.hide()
                parent = self.root.parentWidget()
                if parent is not None and parent.layout() is not None:
                    parent.layout().removeWidget(self.root)
            for adapter in self._input_adapters.values():
                adapter.close()
            if self._adoption is not None:
                self._adoption.restore()
            if isValid(self.root):
                self.root.deleteLater()
            self._embedded_disposed = True
