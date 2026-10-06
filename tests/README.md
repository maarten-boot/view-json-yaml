# jy — test suite

```
make install-dev            # ruamel.yaml, pytest, ruff
make test                   # everything
make test-fast              # no display needed
make test-gui               # only the widget tests
make test-headless          # everything, on a machine with no display
make check                  # formatting, lint, then the display-free tests
```

`make help` lists every target. All of it works without make too: `pytest`,
`pytest -m "not gui"`, `xvfb-run -a pytest`.

`pytest` must run from the directory holding `jy.py`; `conftest.py` puts that directory
on the path itself, so no install step is needed.

## What is where

| file | needs a display | covers |
|------|-----------------|--------|
| `test_document.py` | no | rendering, block structure, paths, folding extents, status detection, both formats |
| `test_navigation.py` | no | JSON Pointer resolution, the `$ref` index, history semantics, the recent-file store |
| `test_gui.py` | **yes** | everything that can only be judged by asking the widgets |
| `test_examples.py` | no | the shipped `examples/` stay loadable and keep demonstrating their features |

The split matters. The first two files test the document model, which is pure data and
fast to run. `test_gui.py` builds a real `jy.App`, loads real files into it and clicks at
real coordinates — it is the only place where an elided fold, a style map, a tag range or
a clipboard round trip is actually checked rather than assumed.

## Why the GUI tests click instead of calling handlers

They use `widget.bbox(index)` to find where a character is on screen and then
`event_generate` there. Calling `app._on_text_click(fake_event)` would pass even if the
binding were attached to the wrong widget, the wrong sequence, or nothing at all. Clicking
proves the wiring.

Two Tk details the tests rely on:

- `text.count("1.0", "end", "displaylines")` counts what is *shown*, so an elided fold is
  observable. Comparing `get("1.0", "end")` would not notice folding at all.
- `ttk::style map` is queried through `app.tk.call`, because a selected `Treeview` row
  drawing its own colour is a property of the style map, not of the row.

## If something fails

- **Every GUI test skips** — no display. Use `xvfb-run -a pytest`.
- **`ModuleNotFoundError: ruamel`** — `make install`; YAML support needs it.
- **`ModuleNotFoundError: tkinter`** — it ships separately from Python on Linux:
  `sudo apt install python3-tk`, or `sudo dnf install python3-tkinter`. Even the
  display-free tests import `jy`, so they need it present, just not running.
- **The hover test hangs or fails** — it runs a short `mainloop` to let the tooltip's
  `after` timer fire. Under a very slow virtual display, raise the margin in
  `TestHover.test_hovering_a_coloured_item_names_its_origin`.
- **Clipboard tests fail on Linux** — Tk's clipboard needs a running window; under
  `xvfb-run` this works, but on some window managers the selection is only readable while
  the app is alive. These tests read it before destroying the app, which is the supported
  case.
- **A test complains a line is not visible** — `click()` calls `see()` first, but a very
  small window can still leave a line off screen. The `app` fixture sets `1200x800`.

## Adding tests

Reference lines by content, not by number:

```python
from conftest import line_of

line = line_of(document, '"licenses"')
```

The sample documents in `conftest.py` (`REPORT`, `SPEC`) are shaped like the real files —
an rl.json scan with components, violations and cross-references, and an OpenAPI spec with
`$ref`s, anchors, a merge key and a block scalar. Extending those is usually better than
inventing a new fixture, since every rule then gets exercised against the same document.

## Verified

The whole suite was run under `xvfb-run` before delivery: **177 passed**, GUI tests
included. `make check` is clean.
