"""Managed scene lifecycle over the original C1/C2 and Qt HostTurn."""
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from uuid import uuid4

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QBoxLayout, QWidget
from shiboken6 import isValid

from agent_native.context import ContextRequest, PolicyRules
from agent_native.context._projection import Direct, Member
from agent_native.context.bindings import resource_record
from agent_native.context.policy import ContextUnavailable, public_value
from agent_native.kernel import PreparedChange, Status
from agent_native.kernel.coordinator import BusyPolicy, CoordinatorBusy, Phase
from agent_native.kernel.permissions import PermissionError, resolve as resolve_permissions
from agent_native.schema import EntityKey, Ref
from agent_native.state import Create, Patch, Update
from agent_native.state.tracking import FieldRef

from .bridge import BridgeInvocation, BridgeStatus
from .declarations import Action
from .engine import EngineUi, UiUnavailable
from .host import GuiPump
from .interaction import InteractionDefinition, InteractionNode, _MAX_NODES, _frozen, _json, _thaw
from .interaction_renderer import InteractionRenderer, input_regions
from .session import UiSessionBroker


@dataclass(frozen=True, slots=True)
class InteractionTurn:
    """An immutable agent offer and optional PNG of the identical managed scene."""

    request_id: str
    offer_id: str
    sequence: int
    state: object
    scene: tuple
    actions: tuple
    image_png: bytes | None = None

    def as_dict(self):
        """Detached JSON. PNG is a separately owned immutable byte value."""
        return {
            "request_id": self.request_id,
            "offer_id": self.offer_id,
            "sequence": self.sequence,
            "state": _thaw(self.state),
            "scene": _thaw(self.scene),
            "actions": _thaw(self.actions),
        }


@dataclass(frozen=True, slots=True)
class _Offer:
    packet: InteractionTurn
    identity: object
    revision: int
    grant: Ref
    bindings: object


@dataclass(slots=True)
class _Call:
    fingerprint: object
    invocation: BridgeInvocation
    callbacks: list

    def notify(self, result):
        listeners = tuple(self.callbacks)
        if result.final:
            self.callbacks.clear()
        for callback in listeners:
            callback(result)

    def follow(self, callback):
        if callback is None:
            return
        result = self.invocation.result
        if result is None or not result.final:
            self.callbacks.append(callback)
        else:
            QTimer.singleShot(0, lambda: callback(result))


class InteractionSession:
    """One strict managed interface, attached to an existing UiRuntime.

    The only interaction state is a versioned, validated JSON envelope in a
    private C1 memory Draft. It uses a dedicated menu anchor, is never installed
    as ContextFocus.draft, and has no text/IME interpretation. Keeping that
    payload in the existing private schema preserves the durable manifest.
    """

    assert_owner = staticmethod(EngineUi.assert_owner)
    matches_cut = EngineUi.matches_cut
    command_resources = EngineUi.command_resources

    @classmethod
    def attach(cls, parent, *, coordinator, kernel, index, principal, definition,
               rules=None, destination="local"):
        """Join a host's already installed runtime; owns only the interaction."""
        return cls(parent, definition, PolicyRules() if rules is None else rules,
                   destination, coordinator, kernel, index, principal)

    def __init__(self, parent, definition, rules, destination, coordinator, kernel, index, principal):
        self.assert_owner()
        if not isinstance(parent, QWidget) or not isValid(parent) or not isinstance(parent.layout(), QBoxLayout):
            raise TypeError("Use a live managed container with a QBoxLayout")
        if parent.findChildren(QWidget):
            raise ValueError("The strict interaction container must start empty")
        if type(definition) is not InteractionDefinition or type(rules) is not PolicyRules:
            raise TypeError("Use a declared interaction and fixed export policy")
        if type(destination) is not str or not destination:
            raise TypeError("A destination is required")
        if (kernel.coordinator is not coordinator or kernel.system is not index.system
                or tuple(kernel.registries) != index.registries or type(principal) is not str or not principal):
            raise ValueError("Use the trusted principal and one installed coordinator/kernel/index")
        self.parent = parent
        self.definition, self.rules, self.destination = definition, rules, destination
        self.coordinator, self.index, self.kernel = coordinator, index, kernel
        self.principal, self.ui = principal, index.ui
        self._id = uuid4().hex
        self._actions = {a.name: a for a in definition.actions}
        self._closed = self._closing = self.closed = False
        self._fault = self.health_fault = None
        self._busy = False
        self.dirty = True
        self.displayed = None
        self._frame = ()
        self._active_bindings = {}
        self._offers = OrderedDict()
        self._calls = {}
        self._mount_committed = False
        self._renderer = InteractionRenderer(self, parent)
        root = self._ref(self.ui.Binding, "interaction")
        self.view_ref = self._ref(self.ui.EngineView, "view")
        self.focus_ref = self._ref(self.ui.ContextFocus, "focus")
        self._draft = self._ref(self.ui.Draft, "state")
        state = self._ref(self.ui.UiState, "interaction")
        bindings = [root]
        operations = [
            self._create(root, path="interaction-root", role="menu"),
            self._create(state, binding=root, opened=True),
            self._create(self._draft, binding=root, text=self._encode(definition.initial_state)),
        ]
        seeds = set()
        try:
            for action in definition.actions:
                if action.command is not None:
                    self.command_descriptor(action.command)
            for name, source in definition.sources.items():
                ref = self._ref(self.ui.Binding, "source-" + name)
                bindings.append(ref)
                if type(source) is FieldRef:
                    resource, role, field = source.ref, "value", source.name
                else:
                    resource, role, field = source.source, "collection", None
                self.index.sources.source(resource)
                seeds.add(resource)
                operations.append(self._create(ref, path="source-" + name, role=role,
                                               source=resource_record(resource), field=field))
            operations.extend((
                self._create(self.view_ref, principal=self.principal, bindings=tuple(bindings), states=(state,)),
                self._create(self.focus_ref, principal=self.principal, view=self.view_ref),
            ))
            self.resource_seeds = tuple(sorted(seeds, key=repr))
            with self.coordinator.read() as cut:
                resources = self.command_resources(cut)
            self.pump = GuiPump(self)
            self.broker = UiSessionBroker(self, resources, ())
            def mount():
                result = self.coordinator.submit("interaction-mount-" + self._id,
                    lambda current: PreparedChange(Patch(creates=tuple(operations))), busy=BusyPolicy.REJECT)
                self._mount_committed = result.status == Status.COMMITTED
                return result

            result = self.pump.driver.turn.dispatch(self.pump.registration, mount)
            if result.status != Status.COMMITTED:
                raise UiUnavailable("Interaction mount requires an idle coordinator")
            self.parent.layout().addWidget(self._renderer)
            self.pump.start()
        except BaseException:
            if hasattr(self, "pump") and not self._mount_committed:
                self.pump._abandon_unmounted()
            self._renderer.deleteLater()
            raise

    def _ref(self, entity, suffix):
        return Ref(EntityKey(entity.__entity_type__, self._id + "-" + suffix, 1))

    @staticmethod
    def _create(ref, **values):
        return Create(ref.key.entity_type, ref.key.entity_id, values, "memory")

    @staticmethod
    def _encode(state, source_stamp=None):
        return json.dumps({"format": "merovinger.interaction/2", "state": _json(_thaw(state)),
                           "source_stamp": source_stamp},
                          sort_keys=True, ensure_ascii=False, allow_nan=False)

    def _state(self, view, source_stamp):
        record = view.resolve(self._draft)
        envelope = json.loads(record.values["text"])
        if (record.partition != "memory" or set(envelope) != {"format", "state", "source_stamp"}
                or envelope["format"] != "merovinger.interaction/2"):
            raise UiUnavailable("Invalid private interaction state")
        if envelope["source_stamp"] is None or envelope["source_stamp"] != source_stamp:
            # Initial state has no C1-derived values. Bound working state may
            # contain copied author values: discard all of it on scope drift
            # before invoking the scene callback or publishing to either side.
            return self.definition.initial_state
        schema = self.definition._state
        return _frozen(schema.data(schema.parse(envelope["state"])))

    @property
    def status(self):
        return BridgeStatus(self.broker.ready and not self._closing and self.health_fault is None,
                            self.broker.stage, self.broker.last_result,
                            self._closing, self._closed, self.health_fault)

    def command_descriptor(self, identity):
        try:
            return self.index.commands[identity][1]
        except KeyError as error:
            raise ValueError("Use a definition ID from the original Registry") from error

    def attach_session(self, task):
        changed = self.coordinator.submit("interaction-focus-" + uuid4().hex,
            lambda current: PreparedChange(Patch(updates=(Update(self.focus_ref, {"task": task}),))),
            busy=BusyPolicy.REJECT)
        if changed.status != Status.COMMITTED:
            raise UiUnavailable("Could not publish the interaction scope")
        self.dirty = True
        return changed

    def step_once(self):
        return self.broker.step_once() if self.broker.stage not in ("ready", "failed") else None

    def set_domain_busy(self, value):
        self._busy = bool(value)
        if isValid(self._renderer):
            self._renderer.input_available(not self._busy and not self._closing)

    def set_outcome(self, error):
        self._fault = error
        return error

    def refuse(self, error):
        return self.set_outcome(error)

    def complete_close(self):
        result = self.coordinator.submit("interaction-close-" + self._id, lambda current:
            PreparedChange(Patch(updates=(
                Update(self.view_ref, {"state": "closed"}),
                Update(self.focus_ref, {"view": None, "task": None}),
            ))), busy=BusyPolicy.REJECT)
        if result.status != Status.COMMITTED:
            raise UiUnavailable("Interaction close did not publish")
        self.closed = True
        if isValid(self._renderer):
            self._renderer.hide()

    def close(self):
        self.assert_owner()
        if self._closed:
            return True
        self._closing = True
        self._offers.clear()
        self._renderer.input_available(False)
        if not self.closed and self.broker.stage in ("ready", "failed"):
            try:
                self.pump.dispatch(self.broker.close)
            except CoordinatorBusy:
                return False
        if not self.closed or self.coordinator.phase != Phase.IDLE:
            return False
        if self.displayed is not None:
            self.displayed.close()
            self.displayed = None
        self.pump.dispose()
        self._renderer.teardown()
        driver = self.pump.driver
        if not driver.views and not driver.closed and driver.drained:
            driver._discard_empty()
        self._calls.clear()
        self._closed = True
        return True

    def _scope(self):
        if self._closing or self.health_fault or not self.broker.ready:
            raise UiUnavailable("Interaction session is unavailable")
        self._renderer.check_topology()
        return ContextRequest(self.principal, self.broker.task, self.broker.grant,
                              self.destination, self.focus_ref, rules=self.rules)

    def _can_command(self, action, resources):
        if action.command is None:
            return True
        descriptor = self.command_descriptor(action.command)
        if not self.rules.schema(descriptor):
            return False
        try:
            resolve_permissions(self.kernel._permission_profiles, self.kernel._command_policy,
                principal=self.principal, channel="ui", task=self.broker.task,
                commands=(action.command,), resources=resources)
        except PermissionError:
            return False
        return True

    def _sources(self, projector, cut, request):
        values, versions = {}, {}
        for name, source in self.definition.sources.items():
            if type(source) is FieldRef:
                route = projector.route_for(source.ref)
                decision = projector.field(source.ref, source.name, via=route) if route is not None else None
                if decision is None or not decision.allowed:
                    raise ContextUnavailable()
                values[name] = public_value(decision.value)
                versions[name] = cut.view.field_version(source.ref, source.name)
            else:
                members = projector.membership(source.source, via=Direct())
                if not members.allowed or len(members.value) > _MAX_NODES:
                    raise ContextUnavailable()
                items, fields = [], []
                for ref in members.value:
                    row = {"ref": ref.key.as_dict()}
                    row_versions = {}
                    via = Member(source.source, Direct())
                    for field in source.fields:
                        decision = projector.field(ref, field, via=via)
                        if not decision.allowed:
                            raise ContextUnavailable()
                        row[field] = public_value(decision.value)
                        row_versions[field] = cut.view.field_version(ref, field)
                    items.append(row)
                    fields.append(row_versions)
                values[name] = items
                versions[name] = (cut.view.collection_version(Ref(source.source.owner), source.source.field), fields)
        values = _json(values)
        # Source versions are sampled only after the exact existing projector
        # authorizes each source. Unrelated C1 ticks and grant rotation with
        # identical authority don't discard drafts. Changed membership, field
        # writes (including write-and-return) and policy versions do.
        scope = (cut.identity.epoch, cut.identity.policy_revision, request.task.key.as_dict(),
                 request.actor, request.destination, values, versions)
        stamp = sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        return _frozen(values), stamp

    @staticmethod
    def _slot(node, event):
        return f"{node.id}:{event}"

    def _compile(self, cut, request):
        resources = self.command_resources(cut)
        with self.index.projection(cut, request) as projector:
            if not projector._policy.allowed:
                raise ContextUnavailable()
            sources, source_stamp = self._sources(projector, cut, request)
        state = self._state(cut.view, source_stamp)
        nodes = self.definition.scene(state, sources)
        if type(nodes) is not tuple or len(nodes) > _MAX_NODES or any(type(n) is not InteractionNode for n in nodes):
            raise UiUnavailable("The scene must return a finite tuple of registered primitives")
        if len({node.id for node in nodes}) != len(nodes):
            raise UiUnavailable("Duplicate interaction node")
        shortcuts = [node.shortcut for node in nodes if node.shortcut is not None]
        if len(set(shortcuts)) != len(shortcuts):
            raise UiUnavailable("Duplicate interaction shortcut")
        scene, actions, bindings, enabled = [], [], {}, {}
        regions = input_regions(nodes)
        width, height = self.definition.size
        for node in nodes:
            x, y, w, h = node.rect
            if x + w > width or y + h > height:
                raise UiUnavailable("Interaction node falls outside its surface")
            events = {}
            for event, binding in node.bindings():
                action = self._actions.get(binding.action)
                if action is None:
                    raise UiUnavailable("Unregistered interaction action")
                native_inputs = {"value"} if event == "edit" else (
                    {"x", "y", "dx", "dy"} if event.startswith("drag") else set())
                names = tuple(source for source, _ in binding.inputs)
                if set(names) - native_inputs or len(set(names)) != len(names):
                    raise UiUnavailable("Unowned interaction input")
                editable = {arg for _, arg in binding.inputs}
                declared = set(binding.fixed) | editable
                if not action._input.required <= declared <= action._input.keys:
                    raise UiUnavailable("Required action parameters need scene or native input")
                slot = self._slot(node, event)
                if action.name not in enabled:
                    enabled[action.name] = self._can_command(action, resources)
                available = (binding.enabled and enabled[action.name]
                             and (not regions[node.id].isEmpty() or event == "activate" and node.shortcut is not None))
                events[event] = available
                if available:
                    bindings[slot] = (action, binding)
                    actions.append({"id": slot, "name": action.name, "label": node.label,
                                    "input_schema": action._input.public(editable)})
            scene.append({"id": node.id, "kind": node.kind, "rect": list(node.rect), "label": node.label,
                          "outline": list(node.outline), "value": node.value,
                          "selected": node.selected, "events": events})
        # Check the aggregate, not just individual title/value bounds.
        _json({"state": _thaw(state), "scene": scene, "actions": actions})
        return nodes, bindings, state, sources, _frozen(scene), _frozen(actions), source_stamp

    def refresh(self):
        self.assert_owner()
        if self._closing or not self.broker.ready or self.coordinator.phase != Phase.IDLE:
            return
        try:
            request = self._scope()
            with self.coordinator.capture("interaction-render") as cut:
                nodes, bindings, *_ = self._compile(cut, request)
                native_matches = self._renderer.matches(nodes, bindings)
                if native_matches and self.displayed is not None and self.matches_cut(cut, self.displayed):
                    self.dirty = False
                    return
                if not native_matches:
                    self._renderer.apply(nodes, bindings)
            current = self.coordinator.read()
        except (ContextUnavailable, CoordinatorBusy):
            self._active_bindings = {}
            self._renderer.apply((), {})
            self.dirty = True
            return
        if self.displayed is not None:
            self.displayed.close()
        self.displayed = current
        self._frame, self._active_bindings, self.dirty = nodes, bindings, False

    def prepare_turn(self, request_id, *, frame=False):
        """Issue one agent offer for the current scope and revision.

        Physical GUI input and tools refer to the same slot IDs. Callers retain
        the offer identity with the model request and discard it when finished.
        """
        self.assert_owner()
        if type(request_id) is not str or not request_id or len(request_id) > 160 or type(frame) is not bool:
            raise ValueError("Invalid interaction turn identity")
        request = self._scope()
        with self.coordinator.capture("interaction-turn") as cut:
            nodes, bindings, state, _, scene, actions, _ = self._compile(cut, request)
            if (self.displayed is None or not self.matches_cut(cut, self.displayed)
                    or not self._renderer.matches(nodes, bindings)):
                raise UiUnavailable("Wait for the native surface to publish this interaction revision")
            image = self._renderer.frame() if frame else None
            packet = InteractionTurn(request_id, uuid4().hex, cut.identity.sequence, state, scene, actions, image)
            record = _Offer(packet, cut.identity, cut.derived_revision, request.grant, MappingProxyType(bindings))
        self._offers[packet.offer_id] = record
        if len(self._offers) > 16:
            self._offers.popitem(last=False)
        return packet

    def finish_turn(self, request_id):
        """Retire offers; a pending invocation still drains through C2."""
        self.assert_owner()
        self._offers = OrderedDict((key, offer) for key, offer in self._offers.items()
                                   if offer.packet.request_id != request_id)
        # The broker and invocation callback retain accepted work independently.
        # Only the retired turn's dedup entries are dropped here.
        self._calls = {key: call for key, call in self._calls.items() if key[0] != request_id}

    @staticmethod
    def _failed(invocation, reason):
        invocation._intake = reason
        invocation._publish("failed_no_effect", reason)
        return invocation

    def invoke(self, slot, arguments, *, on_result=None):
        """Apply a human interaction to the current displayed scene."""
        self.assert_owner()
        return self._invoke(slot, arguments, BridgeInvocation(on_result), None)

    def invoke_tool(self, request_id, offer_id, call_id, slot, arguments, *, on_result=None):
        """Apply one model call with offer, scope, liveness and duplicate checks."""
        self.assert_owner()
        if any(type(v) is not str or not v or len(v) > 160 for v in (request_id, offer_id, call_id, slot)):
            raise ValueError("Invalid interaction call identity")
        key = (request_id, call_id)
        try:
            fingerprint = (offer_id, slot, json.dumps(_json(arguments), sort_keys=True))
        except (TypeError, ValueError):
            return self._failed(BridgeInvocation(on_result), "invalid_arguments")
        existing = self._calls.get(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                return self._failed(BridgeInvocation(on_result), "call_identity_conflict")
            existing.follow(on_result)
            return existing.invocation
        offer = self._offers.get(offer_id)
        if offer is None or offer.packet.request_id != request_id or slot not in offer.bindings:
            return self._failed(BridgeInvocation(on_result), "interaction_offer_unavailable")
        if len(self._calls) >= 256:
            return self._failed(BridgeInvocation(on_result), "interaction_call_limit")
        invocation = BridgeInvocation(None)
        call = _Call(fingerprint, invocation, [on_result] if on_result is not None else [])
        invocation._on_result = call.notify
        self._calls[key] = call
        return self._invoke(slot, arguments, invocation, offer)

    def _invoke(self, slot, arguments, invocation, offer):
        try:
            result = self.pump.dispatch(self._dispatch, slot, arguments, invocation, offer)
        except CoordinatorBusy:
            return self._failed(invocation, "busy")
        except (ContextUnavailable, UiUnavailable):
            return self._failed(invocation, "interaction_scope_unavailable")
        current = invocation.result
        if current is not None and current.outcome == "applied_local":
            try:
                self.refresh()
            except Exception as error:
                # A presentation error after publication cannot change intake
                # or convert a successful local transition to a refusal.
                self.pump._fault(error)
        return result

    def _dispatch(self, slot, arguments, invocation, offer):
        request = self._scope()
        with self.coordinator.read() as cut:
            if self.displayed is None or not self.matches_cut(cut, self.displayed):
                return self._failed(invocation, "stale_interaction")
            if offer is not None and (offer.identity != cut.identity or offer.revision != cut.derived_revision
                                      or offer.grant != request.grant):
                return self._failed(invocation, "stale_interaction")
            nodes, bindings, state, sources, scene, actions, source_stamp = self._compile(cut, request)
            if not self._renderer.matches(nodes, bindings):
                return self._failed(invocation, "stale_interaction")
            if offer is not None and (offer.bindings != bindings or offer.packet.scene != scene
                                      or offer.packet.actions != actions or offer.packet.state != state):
                return self._failed(invocation, "stale_interaction")
            if slot not in bindings or slot not in self._active_bindings:
                return self._failed(invocation, "interaction_unavailable")
            action, binding = bindings[slot]
            if offer is not None and offer.bindings[slot] != (action, binding):
                return self._failed(invocation, "stale_interaction")
            if type(arguments) is not dict or set(arguments) - {arg for _, arg in binding.inputs}:
                return self._failed(invocation, "invalid_arguments")
            try:
                supplied = action._input.parse({**_thaw(binding.fixed), **arguments})
                output = action.handler(supplied, state, sources)
                self.coordinator._reject_async(output)
                if action.command is None:
                    schema = self.definition._state
                    updated = self._encode(schema.data(schema.parse(output)), source_stamp)
                else:
                    if type(output) is not dict:
                        raise TypeError("A command mapper requires named arguments")
                    from .session import wire
                    command_arguments = _json(wire(output, None))
            except (TypeError, ValueError, KeyError):
                return self._failed(invocation, "invalid_interaction")
        if action.command is None:
            result = self.coordinator.submit("interaction-" + invocation.invocation_id,
                lambda current: PreparedChange(Patch(updates=(Update(self._draft, {"text": updated}),))),
                busy=BusyPolicy.REJECT)
            if result.status == Status.COMMITTED:
                invocation._intake = "accepted"
                invocation._publish("applied_local")
                self.dirty = True
            elif result.status == Status.RECONCILING:
                invocation._intake = "accepted"
                invocation._publish("unknown", result.error)
            else:
                self._failed(invocation, "interaction_input_refused")
        else:
            self.broker.commands = (action.command,)
            invocation._intake = self.broker.invoke(Action(action.command, command_arguments), _invocation=invocation)
            if not invocation.accepted and invocation.result is None:
                invocation._publish("failed_no_effect", invocation.intake)
        return invocation
