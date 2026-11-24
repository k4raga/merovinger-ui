**English** · [Русский](quickstart.ru.md)

# Quickstart

Use Python 3.13 and a virtual environment. From a clone of this repository:

```console
python -m venv .venv
```

Activate with `source .venv/bin/activate` on a POSIX shell, or
`.venv\Scripts\Activate.ps1` in PowerShell, then:

```console
python -m pip install .
```

## Register a symbiotic interface

The [form and layer editor](../examples/symbiotic/main.py) show two
independent components built from an `InteractionDefinition`.
Run the examples from the repository root:

```console
python examples/symbiotic/main.py form
python examples/symbiotic/main.py layers
```

Mount the definition with `runtime.attach_interaction(parent, definition=definition)`.
The definition links a typed interaction state, original domain references,
semantic scene nodes and their actions. The SDK owns the scene's input and
publishes its current revision. The examples share the same runtime APIs.
The editor adds its own layer, selection and transformation semantics.

An agent adapter opens a turn with `session.prepare_turn(request_id)`.
The returned snapshot contains the current state and permitted actions.
Pass `frame=True` when the model needs a frame of that scene. Submit an
action with `session.invoke_tool(request_id, offer_id, call_id, slot, arguments)`
and read its tracked outcome. Obtain a new offer after a transition and
call `session.finish_turn(request_id)` when the model turn ends.

## Existing AppBridge integrations

The existing AppBridge example remains available for applications using bound controls.

```console
python examples/ui_agent/main.py --smoke --report .runtime/demo.json
```

The window performs one local-agent interaction and closes. The JSON report
records the result and runtime cleanup. No provider account is needed.
The script creates a temporary SQLite database unless `--database ABSOLUTE_PATH`
is supplied. Choose a fresh path for a reproducible first run.

## Define once

The [complete script](../examples/ui_agent/main.py) is the runnable reference.
Its domain is an ordinary Registry; a command returns a Patch:

```python
@domain.entity("board")
class Board:
    title: str
    enabled: bool
    items: Owned[Item]

@domain.command("set_enabled")
def set_enabled(ctx, board: Ref[Board], enabled: bool):
    return Plan(Patch(updates=(Update(board, {"enabled": enabled}),)))
```

`UiRuntime.open(registries=..., path=..., initial_patch=..., command_policy=...)`
owns the original state/kernel/SQLite runtime. The application supplies its
principal and command policy; the example permits only its two declared local
UI commands. Opening a runtime does not create a QApplication.

## Bind native controls

Create the application's QApplication, parent widget and QBoxLayout.
`runtime.attach(parent, declarations=..., widgets=...)` returns an AppBridge.
The optional mapping associates declared paths with supported existing widgets.
Omit it to generate controls.

`Action(command_id, arguments)` binds an existing command.
`INPUT` substitutes the typed event value. Fields use `FieldRef`; lists use
`CollectionRef`. There is no second copy of domain state in the bindings.

Set `readonly=True` and supply declarations with no Actions for a read-only
view. Local drafts, focus and selection can still change; domain commands cannot.

## Give your agent a packet

`observe_for_model(destination=Destination(...))` returns a pending owner.
From the host's normal timer, call `poll()`. Once ready, `take()` returns one
context-managed payload with `context_json`, `images`, `cut` and `visual_status`.
Each image has `png` bytes and the original capture stamp.

Your adapter receives these values and chooses a declared action. Use
`invoke_tracked` to handle that particular call in the application's Qt loop:

```python
def on_result(result):
    if result.outcome == "committed_local":
        show_saved(result.invocation_id)
    elif result.outcome == "failed_no_effect":
        show_refusal(result.error)
    else:
        show_status(result.outcome)

operation = bridge.invoke_tracked("enabled", False, on_result=on_result)
```

`operation.invocation_id` matches each callback's `result.invocation_id`.
`operation.accepted` reports intake and `operation.intake` is the same string
returned by the existing `bridge.invoke(path, value)`. `operation.result` is
`None` while awaiting its first outcome, then holds the latest snapshot.

- `committed_local` carries the original kernel receipt for a confirmed local write.
- `failed_no_effect` reports a refusal or confirmed rollback, with an error code.
- `unknown` means the bridge could not establish the outcome. Keep the state
  unresolved. With `final=False`, the same invocation can still report a later
  result. `final=True` marks the last report from this handle.
- `approval_required` and `pending_external`, when reported by the kernel,
  carry the original receipt for work awaiting approval or an external result.

The optional callback runs asynchronously on the Qt thread and receives the
latest snapshot if several updates arrive before notification. Keep the
application event loop running. The example's local agent verifies both
context and PNG after the typed change.

An editor's draft is independent of its committed field. Invoking its command
directly does not silently overwrite an unsaved local draft.

## Close what you own

Close each payload and pending observation. Keep the Qt event loop alive while
retrying `bridge.close()` until it returns `True`, then call `runtime.close()`.
A pending CPU encoder may keep close incomplete; cancellation is not evidence
that it has stopped. A bridge borrowing another bridge's capture service can
keep the owner waiting until the borrower closes.

Merovinger does not close the application's QApplication, host window or
unrelated event-loop clients.
