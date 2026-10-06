# jy

A viewer for large JSON and YAML documents, built with tkinter. It exists for two jobs:
reading ReversingLabs `report.rl.json` scans, and finding your way around OpenAPI
specifications.

```
pip install jy-viewer     # once published; installs the `jy` command
jy --file=report.rl.json
```

From a checkout:

```
make install-dev
make run FILE=examples/report.rl.json
make demo
```

`jy.py` is a single file with one dependency (`ruamel.yaml`, for YAML). `jq` is **not**
required: JSON is formatted the way `jq --indent 2 -r .` would, in pure Python.

## What it does

**Reads either format.** JSON is rendered; YAML keeps the file's own text, so comments,
quoting, anchors and formatting survive exactly as written.

**Folds anything.** Every container collapses, and so does a YAML block scalar (`|`, `>`).
Empty containers are left alone. Clicking anywhere on a line that opens or closes a block
shades the whole block.

**Colours by status.** A `status` of `pass`, `warning` or `fail` colours the scalar members
of its block green, orange or red, and tints the dict keys leading down to it, so a failure
deep in a file is visible from the top. `fail` outranks `warning` outranks `pass`. Hovering
a coloured item names every `status` field that contributed to its colour.

**Follows references.** In an OpenAPI spec, a `$ref` pointing inside the document is a
link. Click it to jump; Back and Forward, a dropdown of everywhere you have been, and
`Alt+Left` / `Alt+Right` get you home again — which matters, because schema references are
routinely cyclic. External refs are marked but never followed.

**Two trees.** On the left, every `status` path in the document, coloured and searchable,
with a `NOT` button to invert the filter. On the right, everywhere a word appears: click a
quoted string, a YAML key or a plain value and it fills the search box.

**Copies things out.** `Copy Current Block` gives you a dedented, standalone fragment that
another tool will accept — JSON without its key or trailing comma, YAML with its comments
intact. Also copy the selection, the current line, or the path.

## Keys

| | |
|---|---|
| `Ctrl+O` | open |
| `Ctrl+C` / `Ctrl+B` / `Ctrl+Shift+C` | copy selection / current block / path |
| `Ctrl+A` | select all |
| `Alt+Left` / `Alt+Right` | back / forward |
| `Ctrl+Q` | quit |

Click the gutter or double-click a bracket to fold. Right-click for the copy menu.

## Command line

```
python3 jy.py                                  # start empty
python3 jy.py --file=examples/openapi.yaml     # open a file straight away
```

Anything that is not `.json`, `.yaml` or `.yml` is refused with a warning. Files over 1 MB
show a progress window naming each step; a 5 MB report takes a couple of seconds.

The last 25 files opened are remembered in `~/.jy/recent.txt` — the directory is named
after the script, so renaming `jy.py` moves it.

## Layout of the repository

| path | |
|------|--|
| `jy.py` | the application, one file |
| `jy_spec.md` | the specification: numbered rules, the decisions behind them, and the known limits |
| `tests/` | 177 tests; see `tests/README.md`. They also run flat beside `jy.py` |
| `examples/` | a sample scan and a sample spec, both used by the tests |
| `Makefile` | `make help` lists everything |
| `pyproject.toml` | packaging; hatchling, with the version read from `jy.py` |

## Development

```
make check         # formatting, lint, then the tests that need no display
make test          # everything, including the widget tests
make test-headless # everything, on a machine with no display
```

Code rules live in `ruff.toml`: 120 columns, four spaces, `ruff format` clean.

## Packaging

```
make build          # wheel and sdist into dist/
make publish-test   # TestPyPI
make publish        # PyPI
```

The distribution is **`jy-viewer`**; the import name and the command are both `jy`. They
differ because `jy` on PyPI is already an unrelated JavaScript-interop package. The version
lives in one place, `__version__` in `jy.py`, and hatchling reads it from there.

Before the first upload: choose a licence, add the file, and uncomment the `license` and
`project.urls` lines in `pyproject.toml`. Then rehearse on TestPyPI —

```
make build && make publish-test
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple jy-viewer
```

`tkinter` cannot be installed by pip. A system without it gets a clear message naming the
package to install rather than a bare `ModuleNotFoundError`.

## Not handled

Recorded in `jy_spec.md` §B, briefly: only the first document of a multi-document YAML
file is navigable; YAML flow style (`{a: 1, b: 2}`) puts several members on one line and
only the first is indexed; external `$ref`s are never loaded; numeric `rule_id`s are not
cross-referenced.
