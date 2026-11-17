# Alpha scope and limits

Merovinger UI is a Python 3.13 / PySide6 integration library. The current
distribution preserves the `agent_native` import namespace. Install it in its
own environment; do not co-install the older `agent-native-ui` or
`agent-native-runner` distributions that own the same namespace.

## Native surface

Adoption accepts exact `QLabel`, `QPushButton`, `QLineEdit`, `QListView`,
`QCheckBox` and `QSpinBox` types, not arbitrary subclasses. Each must occupy a
direct QBoxLayout slot below the supplied host parent. This is not a general
adapter for existing applications.

Labels/buttons are plain text. Adopted lists use a standard text delegate,
not embedded/custom cells. Editors are plain single-line controls; unsupported
presentation features are rejected. Bindings and typed commands are explicit.
A changed presentation that cannot be attributed safely can make capture fail.

## Read-only and drafts

Read-only views require declarations without Actions and receive a scoped read
grant. They may still have local draft/focus/selection state. A model cannot
obtain domain mutation rights from the presence of a rendered control.

## Context and pixels

Only registered managed surfaces participate. A denied or unproven paint
dependency currently withholds the **whole PNG surface**; it does not promise
per-region masking. Inspect `visual_status` and `images` rather than assuming
every packet contains an image.

Current policy and expiry are checked before byte handoff. Trusted Python can
copy returned bytes; the library cannot revoke those copies or count external
provider calls. A local bridge is not a sandbox for malicious host code.

Context/frame consistency concerns the published state cut. It does not mean
every external effect has succeeded. Intake acceptance and commit completion
remain distinct.

## Lifecycle and platform

Work is bounded and explicit. Closing can remain pending while a CPU encoder,
durable submission or borrowing view is alive. Keep the owning event loop
running during cleanup; do not treat cancellation as physical drain.

Windows validation is scoped to the tested Windows 11 LTSC environment.
Ubuntu offscreen tests exercise Qt behavior without proving every desktop
configuration. Native macOS, arbitrary widget families, accessibility coverage,
every IME/platform combination and production-scale workloads are not claimed.

Memory, program execution, model transports and a general external API are
outside this UI package. Bring your own model adapter.
