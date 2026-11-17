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

Your adapter receives these values and chooses a declared action.
Call `bridge.invoke(path, value)`, then wait until `bridge.status.ready` and
check its outcome or observe a fresh packet. `"accepted"` is intake acceptance,
not a fabricated durable result. The example's local agent chooses the
`enabled` toggle, then verifies both context and PNG changed.

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
