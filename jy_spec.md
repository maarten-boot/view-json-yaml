# jy — Specification

`jy` reads JSON and YAML.

A Tkinter application for reading large structured documents: ReversingLabs `report.rl.json`
scans, and OpenAPI YAML specifications. Requirements are numbered so an iteration can name
one directly ("change T9").

- **§1 Code rules**
- **§2 Window** — layout
- **§3 Document model** — how a file becomes something the window can show
- **§4 Loading** — opening files, arguments, progress, recent files
- **§5 Text window** — rendering, folding, breadcrumbs, shading, clipboard
- **§6 Navigation** — references and history
- **§7 Status colouring** — for documents that carry `status` fields
- **§8 Left tree** — status paths, search
- **§9 Right tree** — string search
- **§10 report.rl.json** — rules specific to that shape
- **§11 Cross-cutting** — colours, line numbers
- **§A Decisions** — choices the implementation forced
- **§B Known limits** — what is deliberately not handled yet

---

## §1 Code rules

| id | rule |
|----|------|
| C1 | Indent is 4 spaces |
| C2 | Line length is 120 |
| C3 | The code passes `ruff format` unchanged |

## §2 Window

| id | rule |
|----|------|
| W1 | A Tkinter app with a `menu line` at the top and a `status line` at the bottom |
| W2 | A `content frame` between them |
| W3 | The content frame holds, left to right: a scrollable `left tree list`, the `text window`, a scrollable `right tree list` |
| W4 | Text is `black` on a `white` background by default |

## §3 Document model

Everything the window needs comes from one object, built once per file. Structure is
emitted while the document is prepared, never scanned back out of the displayed text.

| id | rule |
|----|------|
| D1 | A `Document` carries: the lines to display, a node tree, line→path, path→line, block ranges, key and value spans per line, and the scalar members of each block |
| D2 | JSON is rendered from its parsed form the way `jq --indent 2 -r .` would, in pure Python; jq is not required at runtime |
| D3 | YAML is **not** re-rendered: the file's own lines are displayed, so comments, quoting, anchors and formatting survive |
| D4 | `current_json: list[str]` is the displayed text, line by line, and is what every line number refers to |
| D5 | A node knows its kind (mapping, sequence, scalar), its key, its path, its first line and the last line it reaches |
| D6 | A block scalar (`|`, `|-`, `>`) reaches past its own line: its text is indented deeper than its key, and it ends at the first line back at or left of that key |

## §4 Loading

| id | rule |
|----|------|
| L1 | `Menu ▸ Files ▸ Open` opens a file selector for `*.json`, `*.yaml`, `*.yml` |
| L2 | `--file=<path>` processes that file directly at startup |
| L3 | A file whose name has none of those extensions produces a warning and is ignored |
| L4 | A file that does have one is added to the recent list |
| L5 | A file over 1 MB shows "Please be patient ... Processing a large file" until processing finishes |
| L6 | Where possible, show a percentage or name the processing step under way |

### Recent files

| id | rule |
|----|------|
| R1 | Remember the last 25 files opened, by absolute path |
| R2 | Store them in a hidden directory in `$HOME` named after the app — `basename argv[0]` without the `.py` extension, so `jy.py` uses `~/.jy` |
| R3 | Keep the list of recently opened files in that directory |

## §5 Text window

Applies to any document, whatever its format.

### Rendering and folding

| id | rule |
|----|------|
| T1 | Show the document line by line |
| T2 | Every container is collapsible, and so is a block scalar (D6) |
| T3 | Empty lists and empty dicts are not collapsible |
| T4 | A JSON fold stops at the closing bracket, leaving `"key": [ … ],` readable; a YAML fold runs to the end of the block's last line, there being no bracket to keep |

### Breadcrumbs

| id | rule |
|----|------|
| T5 | Above the text window, show the path to the current line as breadcrumbs |
| T6 | Each element is clickable and moves the text window there |
| T7 | The breadcrumb bar scrolls horizontally when the path is too long |

### Selection shading

| id | rule |
|----|------|
| T8 | Clicking a `dict key` sets the background of that key's entire value to `very light gray` |
| T9 | Clicking **anywhere on a line that opens or closes a block** shades that block, from where its value starts to its last line; a single bracket is too small a target, and YAML has no bracket to aim at |
| T10 | An empty container shades just the `{}` or `[]` pair |
| T11 | Where several blocks end on the same line, as YAML often does, the innermost one is shaded |

### Clipboard

| id | rule |
|----|------|
| T12 | A `Menu ▸ Edit` item copies data out to other tools; the same items appear on a right click in the text window |
| T13 | **Copy** — the current selection, or the current line when nothing is selected |
| T14 | **Copy Current Block** — the enclosing block, dedented to stand alone. JSON comes back without its key and without the trailing comma, so another tool accepts it; YAML comes back as its own lines, comments included |
| T15 | **Copy Path** — the path of the current line |
| T16 | Copy from the document, so fold marks never reach the clipboard and a collapsed block still yields its real content |

## §6 Navigation

| id | rule |
|----|------|
| N1 | A `$ref` pointing inside the same document is drawn as a link and jumps to its target when clicked |
| N2 | The pointer is a JSON Pointer resolved from the root, `~1` and `~0` unescaped, array indices followed |
| N3 | An external `$ref` (`other.yaml#/X`) is marked but not followed: the file may not exist |
| N4 | An internal `$ref` naming something absent is marked as unresolved, distinctly from an external one |
| N5 | Hovering a `$ref` names its target and how many other references point at the same place |
| N6 | A history bar above the text window holds Back, Forward and a dropdown of everywhere visited |
| N7 | History entries are **paths**, not line numbers, and resolve to a line at the moment of navigation |
| N8 | Following a `$ref` records the place jumped from, so Back returns to the reference |
| N9 | Navigating after going back truncates whatever was ahead |
| N10 | Navigating to where you already stand adds no entry and does not discard the forward trail |
| N11 | Back and Forward are also on `Alt+Left` / `Alt+Right` and in the View menu |
| N12 | History is per document and is cleared when a file is closed |

## §7 Status colouring

Applies to documents that carry `status` fields, in either format.

| id | rule |
|----|------|
| S1 | From each `status: pass`, colour `green` every item in the same block that is not a list value or a dict value |
| S2 | The same for `status: warning` in `orange` |
| S3 | The same for `status: fail` in `red` |
| S4 | On conflict, `fail` outranks every other colour, and `warning` overrides only `pass` |
| S5 | From the block just coloured, travel up the tree and colour each dict `key` leading to it in the same colour — the key only |
| S6 | Status fields are found structurally, by key name, not by matching the text |

## §8 Left tree

| id | rule |
|----|------|
| F1 | Remember the path of every `status` field valued `pass`, `warning` or `fail`, and show it in the left tree |
| F2 | Colour those paths `green`, `orange`, `red` respectively |
| F3 | Selecting a path expands and shows the corresponding item in the text window |
| F4 | Use a tree, so shared path prefixes collapse into one branch |
| F5 | When the left tree is empty, minimise the frame holding it |
| F6 | A search entry above the tree limits what it shows |
| F7 | A `NOT` button before the entry inverts it, listing paths that do **not** contain the pattern |
| F8 | Selecting a line in the text window selects the matching path in the tree |
| F9 | If that exact path is absent, try the enclosing block, and the blocks above it, until one matches |
| F10 | If the match is hidden by a collapsed parent, unfold whatever is needed to make it visible |

## §9 Right tree

| id | rule |
|----|------|
| G1 | The right tree sits right of the text window, with a search entry above it |
| G2 | Text placed in that entry finds every line containing it and shows those paths as a tree |
| G3 | The path of the search item itself is included |
| G4 | Selecting a word in the text window places it in the entry and runs the search: a quoted string, or — YAML writing most of them bare — a dict key or a plain scalar value |
| G6 | Punctuation is not a search term: brackets, empty containers, block-scalar markers (`|-`, `>`) and anchors are ignored |
| G5 | Rows carry the colour the text window gives that hit |

## §10 report.rl.json rules

| id | rule |
|----|------|
| P1 | By default collapse the `component` and `references` lists, but not the `components` dict |
| P2 | For each component in `report.metadata.components.<component_uuid>` that has a colour, colour every other mention of that `component_uuid` the same |
| P3 | For each violation in `report.metadata.violations.<violation_uuid>`, take the colour of its `rule_id` and colour every other mention of that `rule_id` the same |
| P4 | `rule_id`s are strings only, and unique per `<violation_uuid>` |
| P5 | For each coloured item, remember where its colour came from; hovering shows the paths of every origin `status` field that contributed |

## §11 Cross-cutting

| id | rule |
|----|------|
| X1 | `pass` is `green`, `warning` is `orange`, `fail` is `red`, everywhere: text window, left tree, right tree |
| X2 | Severity order is `pass` < `warning` < `fail`, applied wherever colours compete |
| X3 | The selected row in both tree lists is `very light gray`, and its text keeps the colour it had before selection |
| X4 | Every line number shown is a true line number in the displayed document |

---

## §A Decisions

| topic | decision |
|-------|----------|
| `golden` | Not a Tk colour name, and `gold` is illegible on white. `orange` is used, `goldenrod` a one-line swap in `STATUS_COLORS` |
| Empty search box | Filters nothing, even with `NOT` latched — every string contains the empty string, so negating it would blank the tree |
| P1 vs P2/P3 | A list holding a coloured reference stays expanded; folding it would hide what the colouring rules just marked. `KEEP_MARKED_LISTS_OPEN` turns this off |
| Left tree size | Capped at `MAX_TREE_ROWS` (2000) with a "… N more" row; a Treeview of tens of thousands of rows is unusable |
| S1 scope | "Items in the same block" means the block's own scalar members; nested containers keep the colour their own status gives them |
| F5 trigger | Keyed on whether the document has any status paths at all, not on the current filter, so a fruitless search does not make the pane vanish mid-keystroke |
| jq | Not required; `json.dumps(indent=2, ensure_ascii=False)` reproduces its layout. `NaN`/`Infinity` are rejected as invalid, and large integers keep full precision |
| Right-tree search | Debounced ~250ms, since each search scans the whole document; `Return` runs it at once |
| Cut | Not offered. The text window is a viewer, so cutting would mean editing the document |
| YAML root | Owns no line of its own — the first line already belongs to a member or a comment — so it is neither foldable nor a path |
| Merge keys | `<<: *anchor` members are reported by the parser but are not written at that place in the file, so they are skipped |
| Aliases | A reused anchor is a leaf: recursing would put the anchor's line numbers at the alias's location, where the text is only `*name` |

## §B Known limits

| topic | limit |
|-------|-------|
| Multi-document YAML | Only the first document is navigable; the rest displays, with a warning on the status line |
| Flow style | `{a: 1, b: 2}` puts several members on one line and everything here is keyed by line, so only the first is indexed — a `status` inside a flow mapping is not found. Options if this matters: re-render flow style as block YAML (losing the original formatting), or key the model by position rather than line |
| External `$ref` | Marked, never loaded |
| Numeric `rule_id` | Not tracked; matching bare numbers across a document would light up too much |
