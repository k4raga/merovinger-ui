**English** · [Русский](README.ru.md)

# Merovinger UI

**Natural-language commands for Python desktop apps.**

Merovinger UI is an open-source PySide6 (Qt) library for adding AI agents to
desktop applications. A user can give a request such as “Rename this entry” to
the current screen. Your model adapter selects a typed action from that screen,
and Merovinger applies it through the same application state and command
handlers used by the native controls.

Python 3.13 · PySide6 · Alpha · [MIT](LICENSE)

![Merovinger UI shows the same application state to a person and an AI agent](docs/assets/hero.svg)

## What you can build

- **Forms with AI assistance.** A person can start filling out a form, an agent
  can continue the same draft, and the person can review it before saving.
- **Interactive editors.** A layer editor can expose selection, movement,
  preview, confirmation, and cancellation to both the user and the agent.
- **AI actions in an existing Qt application.** Bind supported Qt widgets to
  application fields and commands, then give your model access to those actions.

The [form and layer editor examples](examples/symbiotic/main.py) demonstrate the
first two scenarios. The [Qt widget example](examples/ui_agent/main.py) shows
how to attach an agent to supported controls in an existing window.

## How a symbiotic interface works

A **symbiotic interface** gives people and AI agents a shared interaction model
for the current screen. An [interaction definition](docs/quickstart.md#register-a-symbiotic-interface)
declares the state, semantic scene, and typed actions that drive both the native
view and the agent's available actions.

![The three-step mechanism](docs/assets/mechanics.svg)

1. **Declare** application state, validation, and command handlers.
2. **Bind** Qt controls to those fields and actions. The agent receives
   policy-filtered JSON and can request a PNG of the managed surface.
3. **Observe, act, and verify.** Validate the proposed action against the current
   screen and use its invocation result to confirm a committed change.

## Try it

Install from this repository; no PyPI release is required.

```bash
git clone https://github.com/k4raga/merovinger-ui.git
cd merovinger-ui
python -m pip install .
python examples/ui_agent/main.py --smoke --report .runtime/demo.json
```

Use **Python 3.13** in a virtual environment. Or install the library directly:

```bash
python -m pip install "git+https://github.com/k4raga/merovinger-ui.git"
```

The [self-contained example](examples/ui_agent/main.py) opens a real Qt window
with a line edit, checkbox, and list. A local test agent reads JSON and PNG,
toggles the checkbox through a typed command, and verifies the updated state.
The example runs locally with a scripted agent. For natural-language requests,
connect a model through your application's own adapter.

![PySide6 desktop demo with a text field, checkbox, and list bound to application state](docs/assets/demo.png)

To open a managed form or an interactive layer editor, run either example:

```bash
python examples/symbiotic/main.py form
python examples/symbiotic/main.py layers
```

The distribution is `merovinger-ui`; Python imports remain `agent_native`.

## Small API, explicit ownership

Inside your existing Qt event loop:

```python
from agent_native.ui import Destination

pending = bridge.observe_for_model(destination=Destination(
    "my-agent", "custom-python", "owner", "adapter-v1"
))

# Call from a later host timer tick; poll() does not run a hidden event loop.
if pending.poll():
    with pending.take() as packet:
        my_adapter(packet.context_json, packet.images)  # image.png is bytes

# Agent output uses a declared control and its existing typed command.
bridge.invoke("enabled", False)
```

`take()` checks current authorization and expiry before handing bytes to the
trusted host. Intake acceptance is not commit completion. Close packet/observation
owners and keep polling `bridge.close()` until it returns `True`, then close the
runtime. See the [quickstart](docs/quickstart.md) for the complete lifecycle.

## Supported surface

Generated controls and explicit adoption of these **six exact Qt classes**:

| Qt widget | Declaration |
| --- | --- |
| `QLabel` | `text` |
| `QPushButton` | `button` |
| `QLineEdit` | `editor` |
| `QListView` | `list_view` |
| `QCheckBox` | `toggle` |
| `QSpinBox` | `number` |

Existing widgets must occupy direct `QBoxLayout` slots beneath the host parent.
This is an integration library: state, commands and bindings are declared by the
application, not inferred from arbitrary existing windows.

- SQLite-backed state and typed command admission.
- Read-only views with real scoped read grants.
- Context and PNG tied to one published state revision.
- Current-policy checks, bounded retained assets and explicit cleanup.
- No required Runner, model SDK, Memory or Programs package.

## Verification and scope

The standalone bindings snapshot passed **9 UI cases on Windows 11 LTSC** with
the native Qt `windows` backend. The managed form and layer-editor scenarios
passed on macOS with Qt offscreen. [Verification](docs/verification.md) documents
the tested configurations, Ubuntu offscreen checks, and commands to reproduce
them. This release is **alpha**. See the [supported scope and limits](docs/limits.md).

[Quickstart](docs/quickstart.md) · [Architecture](docs/architecture.md) ·
[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) ·
[Memory library](https://github.com/k4raga/merovinger-memory)
