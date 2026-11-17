"""Standalone persistence, typed commands and bounded lifecycle."""
import subprocess
import sys
from time import monotonic, sleep

import pytest
from agent_native.schema import Registry, Ref, EntityKey
from agent_native.state import Patch, Create, Update
from agent_native.kernel import Status, Phase
from agent_native.kernel.commands import Plan, AdmissionError
from agent_native.ui.runtime import UiRuntime, UiRuntimeOpenError


def domain():
    app = Registry("standalone.test", 1)
    @app.entity("item")
    class Item:
        value: int
    @app.command("set")
    def set_value(ctx, item: Ref[Item], value: int):
        return Plan(Patch(updates=(Update(item, {"value": value}),)))
    return app, Item, Ref(EntityKey(Item.__entity_type__, "one", 1))


def settle(runtime, submission):
    end = monotonic() + 5
    while runtime.coordinator.phase != Phase.IDLE and monotonic() < end:
        runtime.turn.tick()
        sleep(.001)
    assert runtime.coordinator.phase == Phase.IDLE
    assert submission.status in (Status.AWAITING_DB, Status.COMMITTED)


def test_durable_ui_command_read_grant_restore(tmp_path):
    app, Item, item = domain()
    path = tmp_path / "ui.sqlite"
    runtime = UiRuntime.open(registries=(app,), path=path,
        initial_patch=Patch(creates=(Create(Item.__entity_type__, "one", {"value": 0}),)))
    try:
        task = runtime.kernel.create_task(runtime.origin)
        settle(runtime, task.submission)
        read = runtime.kernel.issue_read_grant(runtime.origin, task.ref, resources=(item,))
        settle(runtime, read.submission)
        assert read.permissions.resources == (item,)
        assert read.permissions.commands == read.permissions.actions == read.permissions.create_types == ()
        assert not read.permissions.allow_activity_transfer
        with runtime.coordinator.read() as cut:
            actual = cut.view.resolve(read.ref).values
            assert actual["resources"][0]["entity_type"] == item.key.entity_type and actual["resources"][0]["entity_id"] == "one" and actual["actions"] == ()
        descriptor = app.command_descriptor("set")
        grant = runtime.kernel.issue_grant(runtime.origin, task.ref,
            resources=(item,), actions=(descriptor.definition_id,))
        settle(runtime, grant.submission)
        basis = runtime.kernel.issue_basis(runtime.origin, task.ref, "ui-request", kind="ui")
        opened = runtime.kernel.open_request(runtime.origin, basis)
        settle(runtime, opened.submission)
        origin = runtime.kernel.authenticated(runtime.principal, channel="ui")
        delivery = runtime.kernel.delivery_context(origin, basis, grant.ref)
        result = runtime.kernel.deliver(origin, delivery, {
            "command": descriptor.definition_id, "schema_revision": descriptor.revision,
            "schema_fingerprint": descriptor.fingerprint, "basis": basis.handle,
            "arguments": {"item": item.key.as_dict(), "value": 7}})
        if result.submission is not None:
            settle(runtime, result.submission)
        with runtime.coordinator.read() as cut:
            assert cut.view.field(item, "value") == 7
        with pytest.raises(AdmissionError, match="unsupported_system_profile"):
            runtime.kernel.create_conversation(runtime.origin)
        assert runtime.close()
    finally:
        runtime.close()
    reopened = UiRuntime.open(registries=(app,), path=path,
        initial_patch=Patch(creates=(Create(Item.__entity_type__, "ignored", {"value": 99}),)))
    try:
        assert reopened.restored
        with reopened.coordinator.read() as cut:
            assert cut.view.field(item, "value") == 7
    finally:
        assert reopened.close()


def test_import_blocker_fresh_restore_no_qapplication(tmp_path):
    script = """
import sys
class Block:
 def find_spec(self, fullname, path=None, target=None):
  if any(fullname == x or fullname.startswith(x + ".") for x in
    ("agent_native.runner", "agent_native.programming", "agent_native.actions",
     "agent_native.rules", "agent_native.extensions", "agent_native.skills",
     "pydantic_ai", "wasmtime")):
   raise RuntimeError("optional import: " + fullname)
sys.meta_path.insert(0, Block())
from agent_native.ui.runtime import UiRuntime
from agent_native.schema import Registry, Ref, EntityKey
from agent_native.state import Patch, Create, Update
from agent_native.kernel.commands import Plan
from agent_native.kernel import Phase
from time import monotonic, sleep
from PySide6.QtWidgets import QApplication
app = Registry("blocked.ui", 1)
@app.entity("item")
class Item:
 value: int
@app.command("set")
def set_value(ctx, item: Ref[Item], value: int):
 return Plan(Patch(updates=(Update(item, {"value": value}),)))
def settle(r):
 end=monotonic()+5
 while r.coordinator.phase != Phase.IDLE and monotonic()<end:
  r.turn.tick()
  sleep(.001)
 assert r.coordinator.phase == Phase.IDLE
for i in range(2):
 r = UiRuntime.open(registries=(app,), path=sys.argv[1],
   initial_patch=Patch(creates=(Create(Item.__entity_type__, "one", {"value": 2}),)))
 assert r.restored == bool(i)
 item=Ref(EntityKey(Item.__entity_type__, "one", 1))
 if i == 0:
  task=r.kernel.create_task(r.origin); settle(r)
  read=r.kernel.issue_read_grant(r.origin,task.ref,resources=(item,)); settle(r)
  assert read.permissions.commands == ()
  d=app.command_descriptor("set")
  grant=r.kernel.issue_grant(r.origin,task.ref,resources=(item,),actions=(d.definition_id,)); settle(r)
  basis=r.kernel.issue_basis(r.origin,task.ref,"request",kind="ui")
  r.kernel.open_request(r.origin,basis); settle(r)
  origin=r.kernel.authenticated(r.principal,channel="ui")
  delivery=r.kernel.delivery_context(origin,basis,grant.ref)
  r.kernel.deliver(origin,delivery,{"command":d.definition_id,"schema_revision":d.revision,
   "schema_fingerprint":d.fingerprint,"basis":basis.handle,"arguments":{"item":item.key.as_dict(),"value":8}})
  settle(r)
 with r.coordinator.read() as cut:
  assert cut.view.field(item,"value") == 8
 assert QApplication.instance() is None
 assert r.close()
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "blocked.sqlite")],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr

def test_close_refuses_live_registration_and_pending_commit(tmp_path):
    app, Item, item = domain()
    runtime = UiRuntime.open(registries=(app,), path=tmp_path / "busy.sqlite")
    registration = runtime.turn.register_participant(object())
    try:
        assert runtime.close() is False and runtime._port_close is None
    finally:
        runtime.turn.unregister(registration)
    task = runtime.kernel.create_task(runtime.origin)
    assert task.submission.status == Status.AWAITING_DB
    assert runtime.close() is False and runtime._port_close is None
    settle(runtime, task.submission)
    assert runtime.close() is True


def test_failed_bootstrap_late_ack_close_preserves_original_publication(tmp_path, monkeypatch):
    from queue import Queue
    from agent_native.persistence import SQLiteDurablePort
    app, Item, item = domain()
    acknowledgements = Queue()
    sinks = []
    original_bind = SQLiteDurablePort.bind_outcome_sink
    def hold_original_outcome(port, sink):
        sinks.append(sink)
        return original_bind(port, acknowledgements.put)
    monkeypatch.setattr(SQLiteDurablePort, "bind_outcome_sink", hold_original_outcome)
    path = tmp_path / "late.sqlite"
    with pytest.raises(UiRuntimeOpenError) as caught:
        UiRuntime.open(registries=(app,), path=path, timeout=.2,
            initial_patch=Patch(creates=(Create(Item.__entity_type__, "one", {"value": 63}),)))
    runtime = caught.value.runtime
    try:
        assert runtime._bootstrap_failed and not runtime.closed
        assert runtime.coordinator.phase == Phase.AWAITING_DB
        outcome = acknowledgements.get(timeout=5)
        assert outcome.transaction_id == runtime._bootstrap_transaction
        # A real foreign owner still blocks bootstrap draining.
        registration = runtime.turn.register_participant(object())
        sinks[0](outcome)
        assert runtime.close(timeout=.2) is False
        assert runtime.coordinator.phase == Phase.AWAITING_DB
        runtime.turn.unregister(registration)
        assert runtime.close(timeout=5) is True
        assert runtime.port._thread is None or not runtime.port._thread.is_alive()
    finally:
        runtime.close(timeout=5)
    monkeypatch.setattr(SQLiteDurablePort, "bind_outcome_sink", original_bind)
    restored = UiRuntime.open(registries=(app,), path=path)
    try:
        assert restored.restored
        with restored.coordinator.read() as cut:
            assert cut.view.field(item, "value") == 63
        assert acknowledgements.empty()
    finally:
        assert restored.close()
