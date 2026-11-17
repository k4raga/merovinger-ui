**English** · [Русский](verification.ru.md)

# Verification

## Reproducible repository checks

Use a fresh Python 3.13 environment:

```console
python -m pip install . pytest==9.2.0 build
python -c "from pathlib import Path; Path('.runtime').mkdir(exist_ok=True)"
python -m pytest -q tests --basetemp .runtime/pytest
python examples/ui_agent/main.py --smoke --report .runtime/smoke.json
python -m build --wheel
```

For headless Linux, set `QT_QPA_PLATFORM=offscreen` before the test/smoke commands.
For native Windows validation, use an interactive desktop session and the
actual `windows` Qt backend, not offscreen or a service session.

The suite contains **9 standalone cases**:

| Area | Cases | Checks |
| --- | ---: | --- |
| Runtime | 4 | Durable typed command/read grant/restore; dependency isolation; live-owner refusal; late bootstrap acknowledgement |
| Bindings | 3 | Existing editor/list/checkbox/spinbox adoption and restoration; readonly read grants; Actions rejected in readonly views |
| Model observation | 2 | Authorized JSON/PNG and fresh typed change; hidden-field refusal; grant revocation; shared quotas; actual CPU cleanup |

Tests use local synthetic data. There is no model, WASM or external application
dependency.

## Inherited source-snapshot validation

Before export to this repository, the same library source passed the nine
standalone cases on **Windows 11 LTSC Evaluation, Python 3.13.17, Qt 6.12.0**,
in an interactive user session with Qt backend `windows`. The installed
package matched all 64 SDK Python files before and after the run. Result:
9 passed, no failures/errors/skips; no new surviving threads or optional SDK
imports. A separate native example checked context/PNG → typed change →
fresh packet and cleanup.

These are scoped results for the exported implementation, not a new Windows
run of the renamed distribution. They do not establish performance guarantees,
all-Windows support, or native macOS behavior.

The public suite intentionally omits fixtures for unrelated full-framework
modules. Those modules are not restored merely to inflate this package's test
count. Source identity is recorded in [provenance](provenance.json).

## Current export

On 2026-11-17, a fresh environment in this repository installed
`merovinger-ui 0.1.0a1` on Ubuntu with Python 3.13.18 and Qt 6.12.0:

- **9 tests passed** using the offscreen Qt backend.
- The self-contained example passed: authorized JSON/PNG, typed checkbox
  mutation, fresh packet, bridge/runtime cleanup, and zero provider calls.
- All 64 library source files matched the recorded upstream snapshot.

The repository workflow repeats these checks and builds a wheel. No public CI
result is claimed before the workflow runs.

## Linux host libraries

The Qt Python wheel still needs native system libraries. The offscreen CI job installs `libegl1 libopengl0 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1` on Ubuntu before importing Qt. `QT_QPA_PLATFORM=offscreen` removes the display-server requirement; it does not remove shared-library dependencies. The first public Actions run failed during collection because `libEGL.so.1` was absent; no tests ran in that failed job. The workflow now installs the libraries explicitly. Check the linked Actions history for the corrected run.

The following run exposed a separate clean-checkout issue: pytest needs the parent of an explicit `--basetemp` directory to exist. CI now creates `.runtime` before the test command; this changes only workflow preparation, not assertions or library behavior.

## Recorded public CI

[Run 44057163138](https://github.com/k4raga/merovinger-ui/actions/runs/44057163138)
at source revision 88664af succeeded: tests, context/PNG example and wheel build.
For later changes, inspect the current [Actions runs](https://github.com/k4raga/merovinger-ui/actions).
