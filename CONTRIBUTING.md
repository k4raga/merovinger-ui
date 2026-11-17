# Contributing

Use Python 3.13 and a fresh virtual environment. Install the checkout and tests:

```console
python -m pip install . pytest==9.2.0 build
python -m pytest -q tests --basetemp .runtime/pytest
```

On headless Linux set `QT_QPA_PLATFORM=offscreen`. Reinstall after changing
library source so tests exercise the intended installed version.

Keep changes within the UI package. Prefer one meaningful test for changed
behavior over a broad new matrix. Preserve typed command admission, current
export policy, explicit ownership and cleanup. Do not introduce a second state
store, hidden event loop or model dependency.

For a pull request, explain the concrete problem, resulting behavior and
validation. Keep English and Russian READMEs consistent. Report the OS, Python
and Qt backend with platform-specific issues, using a small synthetic example.
Never attach credentials, private screenshots or unrelated application data.

Code and original documentation/assets are offered under the repository's
[MIT license](LICENSE). This alpha API can change; discuss a public API change
before expanding it.
