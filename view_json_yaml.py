#!/usr/bin/env python3
"""view-json-yaml - a Tkinter viewer for JSON and YAML, with a status-path list, folding, and status colouring.

Layout (top to bottom):
    * menu bar      - Files / View / Help
    * content frame - status-path tree left, breadcrumb + fold gutter + text window centre, word matches right
    * status line   - message on the left, counters on the right

A JSON file is formatted the way `jq --indent 2 -r .` would, in pure Python; a YAML file keeps its own text so
comments and quoting survive. Either way the result is kept in `current_json`, every line
carrying a "status" key is indexed in `status_lines`, its jq path is recorded in `status_paths`, and every
non-empty {...} / [...] pair becomes a foldable block. Containers named by AUTO_COLLAPSE_KEYS start folded.

A "status" of pass / warning / fail colours the scalar items of its enclosing block - members whose value is not a
list or a dict, so nested containers keep whatever colour their own status gives them. The dict keys leading down to
that block are coloured too, so a failure stays visible in the breadcrumb even when its block is folded away.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError as exc:  # pip cannot supply tkinter; the system package manager has to
    raise SystemExit(
        "view-json-yaml needs tkinter, which ships separately from Python on most Linux distributions:\n"
        "  Debian/Ubuntu   sudo apt install python3-tk\n"
        "  Fedora          sudo dnf install python3-tkinter\n"
        "  macOS/Windows   use a python.org build, which includes it"
    ) from exc
from typing import NamedTuple

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

__version__ = "0.1.0"

APP_NAME = "view-json-yaml"
# L: the last opened files live in ~/.<script name>/recent.txt, newest first.
RECENT_LIMIT = 25
RECENT_FILE = "recent.txt"
FALLBACK_SLUG = "view-json-yaml"
# O: past this size the load runs behind a progress window, reporting each stage below.
LARGE_FILE_BYTES = 1_000_000
PROCESSING_STEPS = (
    "Reading file",
    "Indexing status lines",
    "Resolving JSON paths",
    "Scanning blocks",
    "Filling the text window",
    "Colouring",
    "Applying default folds",
    "Building the path trees",
)
JSON_INDENT = 2

FG_COLOR = "black"
BG_COLOR = "white"
GUTTER_FG = "#666666"
GUTTER_BG = "#f0f0f0"
REVEAL_BG = "#ffe9a8"
# "very light gray", shared by the dict-value wash and the selected row in either tree. Darken it here if it reads
# too faint on your display; both uses are meant to be noticed without competing with the status colours.
VERY_LIGHT_GRAY = "#eeeeee"
VALUE_BG = VERY_LIGHT_GRAY
LINK_COLOR = "#1a5fb4"
# A selected tree row is washed in the same very light grey and keeps whatever colour its status gave it.
TREE_SELECT_BG = VERY_LIGHT_GRAY
TREE_STYLE = "Status.Treeview"
# A tree with tens of thousands of rows is slow to build and unusable to scroll: cap it and say so.
MAX_TREE_ROWS = 2000
# How long the pointer must rest on a coloured item before its origin is named.
HOVER_DELAY_MS = 400
# Typing in the match box scans the whole buffer, so wait for a pause before running it.
MATCH_SEARCH_DELAY_MS = 250
# A hover names every status that contributed; past this many the rest are summarised.
MAX_HOVER_ORIGINS = 6
# How many places the history bar remembers, and how a $ref is drawn.
HISTORY_LIMIT = 50
REF_COLOR = LINK_COLOR
DEAD_REF_COLOR = "#8a6d3b"
TOOLTIP_BG = "#ffffe0"

# Lowest priority first, so a key leading to both a pass and a fail ends up red. Tk's "orange" (#FFA500) is light
# against white: swap in "goldenrod" (#DAA520) here if it reads faint on your display.
STATUS_COLORS = {"pass": "green", "warning": "orange", "fail": "red"}
SEVERITY = {value: rank for rank, value in enumerate(STATUS_COLORS)}

# Swap for "-" / "+" if the fixed-width font lacks the arrows.
ARROW_OPEN = "\u25be"
ARROW_CLOSED = "\u25b8"
FOLD_MARK = " \u2026"

OPENERS = "{["
CLOSERS = "}]"

# Lists under these keys start folded, so a long inventory does not bury the rest of the document. Only lists:
# a dict sharing the name (say "components") keeps its contents on screen.
AUTO_COLLAPSE_KEYS = ("component", "references")

# A coloured component is keyed by its uuid at <anything>.metadata.components.<component_uuid>; every occurrence of
# that uuid anywhere in the buffer is painted the component's colour. The path is matched by its tail, so a wrapper
# such as report.metadata.components... works the same as metadata.components... at the root.
METADATA_KEY = "metadata"
COMPONENTS_KEY = "components"
UUID_KEY = "uuid"
# Violations are keyed the same way, but the token to chase is their rule_id: a string, unique per violation.
VIOLATIONS_KEY = "violations"
RULE_ID_KEY = "rule_id"

REFERENCE_TAGS = {value: f"ref_{value}" for value in STATUS_COLORS}
# Set True to underline references as well as colour them.
UNDERLINE_REFERENCES = False
# A list holding a coloured reference stays open, so rule H's default folds cannot hide it. Set False to let H win.
KEEP_MARKED_LISTS_OPEN = True

STATUS_KEY = "status"
# A $ref pointing inside the same document becomes a link; anything else is shown but not followed.
REF_KEY = "$ref"
# Splits an object member into its key and whatever follows the colon.
MEMBER_RE = re.compile(r'^"(?P<key>(?:[^"\\]|\\.)*)"\s*:\s*(?P<rest>.*)$')
# A dict member split into its key, its value and any trailing comma, used to shade a value on demand.
VALUE_RE = re.compile(r'^(?P<lead>\s*"(?:[^"\\]|\\.)*"\s*:\s*)(?P<value>.*?),?\s*$')
# The quoted key at the head of a line, used to colour breadcrumbs.
KEY_RE = re.compile(r'^(?P<indent>\s*)(?P<key>"(?:[^"\\]|\\.)*")\s*:')
# A key that can be written as .key rather than ["key"].
PLAIN_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Cheap pre-filter: a line with no bracket cannot open or close a block.
BRACKET_RE = re.compile(r"[{}\[\]]")
# Word-ish runs, used to find tracked ids without a giant alternation. The character class is also the boundary rule:
# an id counts only where it fills a whole run, so RULE-A never claims the head of RULE-A2.
CANDIDATE_RE = re.compile(r"[\w-]+")
SIMPLE_TOKEN_RE = re.compile(r"^[\w-]+$")


class DocumentError(RuntimeError):
    """The file could not be read, or does not hold a valid document."""


def app_directory() -> Path:
    """The app's hidden directory in HOME, named after the command.

    Started as `view-json-yaml` the stem is already the command; started as `python3 view_json_yaml.py` it is the
    module name. Underscores fold to hyphens so both land in the same directory instead of keeping two lists.
    """
    slug = Path(sys.argv[0]).stem.replace("_", "-") or FALLBACK_SLUG
    return Path.home() / f".{slug}"


class RecentFiles:
    """The last few absolute paths opened, newest first, kept in a plain one-path-per-line file.

    Every filesystem error is swallowed and recorded in `error`: a viewer that cannot write its history should still
    open documents. Paths are resolved before storing, so the same file reached two ways counts once.
    """

    def __init__(self, directory: Path, limit: int = RECENT_LIMIT) -> None:
        self.path = directory / RECENT_FILE
        self.limit = limit
        self.error: str | None = None
        self.paths: list[Path] = self._read()

    def _read(self) -> list[Path]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            self.error = f"Could not read {self.path}: {exc}"
            return []

        found: list[Path] = []
        for entry in text.splitlines():
            candidate = Path(entry.strip())
            if entry.strip() and candidate not in found:
                found.append(candidate)
        return found[: self.limit]

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(str(entry) for entry in self.paths) + "\n", encoding="utf-8")
            self.error = None
        except OSError as exc:
            self.error = f"Could not save {self.path}: {exc}"

    def add(self, path: Path) -> None:
        """Put `path` at the front, dropping any earlier copy and anything past the limit."""
        resolved = path.expanduser().resolve()
        self.paths = [resolved, *(entry for entry in self.paths if entry != resolved)][: self.limit]
        self._write()

    def remove(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        remaining = [entry for entry in self.paths if entry != resolved]
        if remaining != self.paths:
            self.paths = remaining
            self._write()

    def clear(self) -> None:
        self.paths = []
        self._write()


JSON_SUFFIXES = (".json",)
YAML_SUFFIXES = (".yaml", ".yml")


def is_supported_file(path: Path) -> bool:
    """A file is taken on its extension alone, which is all --file promises to check."""
    return path.suffix.lower() in JSON_SUFFIXES + YAML_SUFFIXES


def file_size(path: Path) -> int:
    """Size in bytes, or 0 when it cannot be measured - a missing file is reported by the loader, not here."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Read --file=<path>. Unknown arguments are left alone rather than killing the app before it draws."""
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Browse a JSON or YAML document.")
    parser.add_argument("--file", dest="file", default=None, help="JSON or YAML file to open at startup")
    known, _unknown = parser.parse_known_args(argv)
    return known


def without_selected(entries: list) -> list:
    """Drop every style-map entry that fires in the selected state.

    ttk paints a selected row using the style's selected-state colours, which would bury the status colour the row
    carries as a tag. Removing those entries lets the tag win in both states. The ("!disabled", "!selected", colour)
    pairs Tk ships since 8.6.9 go too: they are why tag colours are ignored in a Treeview outright on that release.
    """
    kept = []
    for entry in entries:
        states = [str(part) for part in entry[:-1]]
        if not any("selected" in state for state in states):
            kept.append(entry)
    return kept


def shorten_home(path: Path) -> str:
    """Display form of an absolute path, with the home directory folded back to ~."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


class History:
    """Where the reader has been, with a cursor like a browser's.

    Entries are paths, not line numbers: a path still means something after the document is reloaded or re-rendered,
    and following a $ref can land far from where the line numbers were when the entry was made.
    """

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self.entries: list[tuple[tuple[str, ...], str]] = []
        self.index = -1
        self.limit = limit

    def visit(self, parts: tuple[str, ...], label: str) -> None:
        """Record a new location, dropping anything that was ahead of the cursor."""
        if self.entries and self.entries[self.index][0] == parts:
            return  # already standing there
        del self.entries[self.index + 1 :]
        self.entries.append((parts, label))
        if len(self.entries) > self.limit:
            self.entries.pop(0)
        self.index = len(self.entries) - 1

    def back(self) -> tuple[str, ...] | None:
        if self.index <= 0:
            return None
        self.index -= 1
        return self.entries[self.index][0]

    def forward(self) -> tuple[str, ...] | None:
        if self.index < 0 or self.index >= len(self.entries) - 1:
            return None
        self.index += 1
        return self.entries[self.index][0]

    def go(self, index: int) -> tuple[str, ...] | None:
        if 0 <= index < len(self.entries):
            self.index = index
            return self.entries[self.index][0]
        return None

    def labels(self) -> list[str]:
        return [label for _parts, label in self.entries]

    def clear(self) -> None:
        self.entries.clear()
        self.index = -1


class Tooltip:
    """A borderless label that follows the pointer, used to name where a colour came from."""

    def __init__(self, master: tk.Misc) -> None:
        self.master = master
        self.window: tk.Toplevel | None = None
        self.text = tk.StringVar()

    def show(self, text: str, x: int, y: int) -> None:
        if self.window is None:
            self.window = tk.Toplevel(self.master)
            self.window.overrideredirect(True)  # no title bar: it is a label, not a window
            self.window.attributes("-topmost", True)
            tk.Label(
                self.window,
                textvariable=self.text,
                background=TOOLTIP_BG,
                foreground=FG_COLOR,
                relief="solid",
                borderwidth=1,
                justify="left",
                padx=6,
                pady=3,
            ).pack()
        self.text.set(text)
        self.window.geometry(f"+{x + 14}+{y + 18}")
        self.window.deiconify()

    def hide(self) -> None:
        if self.window is not None:
            self.window.withdraw()


class ProgressWindow:
    """A small window shown while a large file is processed.

    Tk is single threaded, so the work stays on the main thread and this repaints between stages. The grab keeps the
    main window out of reach while it is up, which also stops a second load being started mid-flight.
    """

    def __init__(self, master: tk.Misc, total: int) -> None:
        self.total = max(1, total)
        self.done = 0

        self.window = tk.Toplevel(master)
        self.window.title(APP_NAME)
        self.window.resizable(False, False)
        self.window.transient(master)

        frame = ttk.Frame(self.window, padding=16)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="Please be patient \u2026 Processing a large file").grid(sticky="w")

        self.detail = tk.StringVar(value="Starting \u2026")
        self.bar = ttk.Progressbar(frame, mode="determinate", maximum=100, length=340)
        self.bar.grid(pady=(10, 6), sticky="ew")
        ttk.Label(frame, textvariable=self.detail, foreground=GUTTER_FG).grid(sticky="w")

        self.window.update_idletasks()
        self._center_on(master)
        self.window.grab_set()
        self.window.update()

    def _center_on(self, master: tk.Misc) -> None:
        x = master.winfo_rootx() + (master.winfo_width() - self.window.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.window.winfo_height()) // 3
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")

    def step(self, label: str) -> None:
        self.done += 1
        percent = min(100, round(self.done / self.total * 100))
        self.detail.set(f"{label} \u2026 {percent}%")
        self.bar["value"] = percent
        self.window.update()

    def close(self) -> None:
        self.window.grab_release()
        self.window.destroy()


class JsonPath(NamedTuple):
    """A path in both forms: the text to display, and the segments used to build the trees."""

    text: str
    parts: tuple[str, ...]


class ColorSpan(NamedTuple):
    """A coloured stretch of one line, and the status field that decided its colour."""

    start: int
    end: int
    value: str
    origin_path: str
    origin_line: int


class TreeIndex(NamedTuple):
    """What a built tree can be asked afterwards: where a node points, and which node holds a path."""

    lines: dict[str, int]  # tree item -> line in the buffer
    items: dict[tuple[str, ...], str]  # path segments -> tree item


class TreeRow(NamedTuple):
    """One row to render in either path tree: where it hangs, what it says, and where it points."""

    parts: tuple[str, ...]
    label: str
    value: str | None
    line: int


class StatusEntry(NamedTuple):
    """One "status" field: where it sits, what it is called, and what it says."""

    line: int
    path: str
    parts: tuple[str, ...]
    value: str


def _reject_constant(name: str) -> object:
    """json accepts NaN and Infinity by default; real JSON does not, and neither does jq."""
    raise ValueError(f"{name} is not valid JSON")


def format_json(text: str) -> list[str]:
    """Format JSON the way `jq --indent 2 -r .` does, with no jq on the machine.

    json.dumps already matches jq's layout at indent=2: two-space nesting, one member per line, and empty containers
    kept inline as {} / []. ensure_ascii=False leaves non-ASCII readable the way jq writes it, and a document that is
    just a string is printed raw, which is what -r means.
    """
    return build_document(parse_json(text)).lines


# Text that says how a value is written, or that it is empty, rather than what it is: not worth searching for.
UNSEARCHABLE = ("|", "|-", "|+", ">", ">-", ">+", "{", "}", "[", "]", "{}", "[]")


def _plain_word(text: str) -> str | None:
    """A value as a search term, or None when it is only YAML punctuation."""
    word = text.strip().strip("\"'").strip()
    if not word or word in UNSEARCHABLE or word[0] in "&*":
        return None
    return word


def _yaml_key_span(line: str, column: int) -> tuple[int, int]:
    """Columns of the key token written at `column`, quoted or bare."""
    if column < len(line) and line[column] in "\"'":
        quote = line[column]
        end = line.find(quote, column + 1)
        return (column, end + 1) if end > 0 else (column, len(line))
    end = line.find(":", column)
    return column, len(line[column:end].rstrip()) + column if end > 0 else len(line)


def _lc_position(container: object, index: object, kind: str) -> tuple[int, int] | None:
    """Where a key, value or element sits, or None when this part of the text does not hold it.

    Keys pulled in by a merge key (`<<: *anchor`) are reported by ruamel as members but have no position here: they
    live at the anchor. Asking for their position raises, and the honest answer is that they are not on screen.
    """
    marks = getattr(container, "lc", None)
    if marks is None:
        return None
    try:
        return getattr(marks, kind)(index)
    except (KeyError, IndexError, AttributeError, TypeError):
        return None


def _yaml_value_end(line: str, start: int) -> int:
    """Where a value written on one line ends: before any trailing comment, and never inside a quoted string."""
    quote = ""
    for column in range(start, len(line)):
        char = line[column]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and column > start and line[column - 1] in " \t":
            return len(line[:column].rstrip())
    return len(line.rstrip())


def _scalar_extent(lines: list[str], line: int) -> int:
    """The last line a scalar written at `line` occupies.

    A block scalar ("description: |-") keeps its text on the following lines, indented deeper than its own key.
    Anything indented past that key belongs to it, blank lines included when more text follows; the first line back
    at or left of the key ends it. For an ordinary one-line scalar this returns the line itself.
    """
    body = lines[line - 1]
    indent = len(body) - len(body.lstrip())
    last = line
    for number in range(line + 1, len(lines) + 1):
        row = lines[number - 1]
        if not row.strip():
            continue  # a blank line inside block text does not end it
        if len(row) - len(row.lstrip()) <= indent:
            break
        last = number
    return last


def build_yaml_document(text: str) -> Document:
    """Structure a YAML file without re-rendering it.

    The file's own lines are what the window shows, so comments, quoting, anchors and blank lines all survive
    untouched; ruamel supplies the position of every key and value, which is all the structure the window needs.
    Only the first document of a multi-document file is structured - the rest is still displayed. Flow style
    ("{a: 1, b: 2}") puts several members on one line; since everything here is keyed by line, only the first of
    them is indexed, and a status hidden inside a flow mapping is not found.
    """
    yaml = YAML()
    try:
        loaded = list(yaml.load_all(text))
    except YAMLError as exc:  # every ruamel parse failure derives from this
        raise DocumentError(f"Not valid YAML: {exc}") from None

    document = Document(lines=text.splitlines() or [""], style="yaml")
    if len(loaded) > 1:
        document.warning = f"{len(loaded)} documents in this file; only the first is navigable"
    data = loaded[0] if loaded else None

    containers: list[tuple[int, int, int]] = []  # (opening line, closing line, depth), outermost first
    seen: set[int] = set()

    def visit(
        value: object,
        parts: tuple[str, ...],
        path: str,
        prefix: str,
        key: str | None,
        line: int,
        depth: int,
        root: bool = False,
    ) -> Node:
        """Record the value that starts on `line`; return its node with the last line it reaches.

        The root is a special case: in YAML it owns no line of its own - the first line of the file already belongs
        to a member or a comment - so it claims neither a path nor a fold, and only its children are recorded.
        """
        kind = "mapping" if isinstance(value, dict) else "sequence" if isinstance(value, list) else "scalar"
        alias = kind != "scalar" and id(value) in seen  # an anchor reused: the text here is just *name
        node = Node(kind if not alias else "scalar", parts, path, key, line, value=value if kind == "scalar" else None)
        if not root:
            document.nodes.setdefault(line, node)
            document.paths.setdefault(line, JsonPath(path, parts))
            document.line_of_parts.setdefault(parts, line)

        if kind == "scalar" or alias or not value:
            if not root:  # block text ("|-", ">") runs past its key and folds like any other block
                extent = _scalar_extent(document.lines, line)
                if extent > line:
                    node.close_line = extent
                    document.blocks.close_of[line] = extent
                    document.blocks.open_of.setdefault(extent, line)
                    containers.append((line, extent, depth))
            return node

        seen.add(id(value))
        last = line
        members = value.items() if isinstance(value, dict) else enumerate(value)
        for index, item in members:
            if isinstance(value, dict):
                child_key = str(index)
                position = _lc_position(value, index, "key")
                if position is None:  # merged in from an anchor: not written at this place in the file
                    continue
                child_parts = (*parts, child_key)
                child_path = _path_text(prefix, child_key)
                child_line = position[0] + 1
                document.key_spans[child_line] = _yaml_key_span(document.lines[child_line - 1], position[1])
                where = _lc_position(value, index, "value")
                if where and where[0] == position[0]:  # "key: value" on one line
                    body = document.lines[child_line - 1]
                    document.value_spans[child_line] = (where[1], _yaml_value_end(body, where[1]))
                else:  # a block that starts underneath its key
                    tail = len(document.lines[child_line - 1].rstrip())
                    document.value_spans[child_line] = (tail, tail)
                child = visit(item, child_parts, child_path, child_path, child_key, child_line, depth + 1)
            else:
                position = _lc_position(value, index, "item")
                child_parts = _element_parts(parts, index)
                child_path = f"{prefix}[{index}]"
                child_line = (position[0] + 1) if position else last
                child = visit(item, child_parts, child_path, child_path, None, child_line, depth + 1)
            node.children.append(child)
            last = max(last, child.close_line or child.line)

        if last > line and not root:
            node.close_line = last
            document.blocks.close_of[line] = last
            # Children are recorded first, so setdefault keeps the innermost block for a shared last line.
            document.blocks.open_of.setdefault(last, line)
            containers.append((line, last, depth))
        document.scalar_items[line] = [child.line for child in node.children if child.is_scalar]
        return node

    document.root = visit(data, (), ".", "", None, 1, 0, root=True)
    document.line_of_parts[()] = 1

    # Which block holds each line: assign outermost first so a deeper container overwrites its parent.
    for line in range(1, len(document.lines) + 1):
        document.blocks.enclosing[line] = 0
    for opened, closed, _depth in sorted(containers, key=lambda entry: entry[2]):
        for line in range(opened + 1, closed + 1):
            document.blocks.enclosing[line] = opened
    index_refs(document)
    return document


def parse_json(text: str) -> object:
    """Parse JSON strictly: NaN and Infinity are not JSON, whatever Python's decoder allows."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        raise DocumentError(f"Not valid JSON: {exc}") from None


def load_document(path: Path) -> Document:
    """Read `path` and structure it according to its extension.

    JSON is rendered from its parsed form the way jq would; YAML keeps the file's own text and is only indexed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DocumentError(f"Could not read {path.name}: {exc}") from None
    except UnicodeDecodeError as exc:
        raise DocumentError(f"{path.name} is not UTF-8 text: {exc}") from None

    if path.suffix.lower() in YAML_SUFFIXES:
        return build_yaml_document(text)
    return build_document(parse_json(text))


def collect_status_lines(document: Document) -> dict[str, list[int]]:
    """Map every distinct value of a "status" member to the 1-based lines it appears on.

    Read from the parsed tree rather than matched against the text, so the same rule serves JSON and YAML alike.
    """
    found: dict[str, list[int]] = {}
    for line, node in sorted(document.nodes.items()):
        if node.key == STATUS_KEY:
            found.setdefault(_status_text(node), []).append(line)
    return found


def _status_text(node: Node) -> str:
    """How a status value is named, spelled the way it appears in JSON."""
    if node.kind in ("mapping", "sequence"):
        opener, empty = ("{", "{}") if node.kind == "mapping" else ("[", "[]")
        return opener if node.children else empty
    if isinstance(node.value, str):
        return node.value
    try:
        return json.dumps(node.value)
    except TypeError:  # a YAML scalar json cannot spell, a date say
        return str(node.value)


def _base_segment(part: str) -> str:
    """A path segment without its array subscript: "component[3]" -> "component"."""
    return part.split("[", 1)[0]


def inside_string(line: str, column: int) -> bool:
    """True when `column` falls inside a quoted string, where a bracket is text rather than structure."""
    return any(start <= column <= end for start, end, _ in quoted_spans(line))


def quoted_spans(line: str) -> list[tuple[int, int, str]]:
    """Every double-quoted string on the line as (opening quote column, closing quote column, inner text).

    Escapes are honoured, so a \\" inside a value does not end the span early.
    """
    spans: list[tuple[int, int, str]] = []
    start: int | None = None
    escaped = False
    for column, char in enumerate(line):
        if start is None:
            if char == '"':
                start = column
            continue
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            spans.append((start, column, line[start + 1 : column]))
            start = None
    return spans


def quoted_token_at(line: str, column: int) -> str | None:
    """The quoted word `column` falls inside, or None when the click missed every string on the line."""
    for start, end, text in quoted_spans(line):
        if start <= column <= end:
            return text or None
    return None


def _element_parts(parts: tuple[str, ...], index: int) -> tuple[str, ...]:
    """Segments for an array element: the index rides along with the key that named the array.

    ("suites",) at index 0 becomes ("suites[0]",), which keeps the tree one level shallower than splitting the
    subscript into a node of its own.
    """
    if not parts:
        return (f"[{index}]",)
    return (*parts[:-1], f"{parts[-1]}[{index}]")


def collect_status_paths(paths: dict[int, JsonPath], status_lines: dict[str, list[int]]) -> list[StatusEntry]:
    """Pair each pass / warning / fail line with its jq path, in document order.

    Only those three values are kept. `status_lines` still holds every "status" line whatever it says, but a status
    of "skip", null or 200 carries no colour, so listing it in the tree would add rows nothing can act on.
    """
    unknown = JsonPath("?", ("?",))
    entries = [
        StatusEntry(line, paths.get(line, unknown).text, paths.get(line, unknown).parts, value)
        for value, numbers in status_lines.items()
        if value in STATUS_COLORS
        for line in numbers
    ]
    return sorted(entries)


@dataclass
class Node:
    """One value in the document, and where it landed in the rendered text."""

    kind: str  # "mapping", "sequence" or "scalar"
    parts: tuple[str, ...]
    path: str
    key: str | None  # None for array elements and the root
    line: int
    close_line: int | None = None  # None for scalars and for containers kept on one line
    value: object = None  # scalars only
    children: list[Node] = field(default_factory=list)

    @property
    def is_scalar(self) -> bool:
        return self.kind == "scalar"

    def child(self, key: str) -> Node | None:
        for node in self.children:
            if node.key == key:
                return node
        return None


class Blocks:
    """The block structure of a rendered document.

    close_of    -- opening line -> closing line, for containers that span more than one line
    open_of     -- closing line -> opening line
    enclosing   -- any line -> the opening line of the innermost block containing it (0 at top level)

    Built by the renderer rather than recovered by scanning the text, so a bracket inside a string or an empty
    container needs no special handling: neither ever reaches this.
    """

    def __init__(
        self,
        close_of: dict[int, int] | None = None,
        open_of: dict[int, int] | None = None,
        enclosing: dict[int, int] | None = None,
    ) -> None:
        self.close_of = close_of or {}
        self.open_of = open_of or {}
        self.enclosing = enclosing or {}

    def is_foldable(self, line: int) -> bool:
        return line in self.close_of or line in self.open_of

    def opening_line(self, line: int) -> int | None:
        """The opening line of the block whose bracket sits on `line`, either end."""
        if line in self.close_of:
            return line
        return self.open_of.get(line)

    def span_around(self, line: int) -> tuple[int, int] | None:
        """First and last line of the innermost block enclosing `line`."""
        opened = self.enclosing.get(line, 0)
        closed = self.close_of.get(opened)
        if opened == 0 or closed is None:
            return None
        return opened, closed

    def ancestors(self, line: int) -> list[int]:
        """Opening lines of every block containing `line`, outermost first."""
        chain: list[int] = []
        current = self.enclosing.get(line, 0)
        while current:
            chain.append(current)
            current = self.enclosing.get(current, 0)
        return list(reversed(chain))


@dataclass
class Ref:
    """A $ref written somewhere in the document, and where it points."""

    line: int
    span: tuple[int, int]  # columns of the pointer on that line
    pointer: str
    target_line: int | None = None
    target_parts: tuple[str, ...] | None = None

    @property
    def internal(self) -> bool:
        return self.pointer.startswith("#")

    @property
    def resolved(self) -> bool:
        return self.target_line is not None


@dataclass
class Document:
    """A parsed document together with everything the window needs to display and navigate it.

    Structure is emitted while rendering instead of being scanned back out of the text afterwards. Everything keyed
    by line uses true line numbers into `lines`, which is what the text window shows.
    """

    lines: list[str] = field(default_factory=list)
    root: Node | None = None
    nodes: dict[int, Node] = field(default_factory=dict)  # line -> the value introduced there
    paths: dict[int, JsonPath] = field(default_factory=dict)
    line_of_parts: dict[tuple[str, ...], int] = field(default_factory=dict)
    blocks: Blocks = field(default_factory=Blocks)
    key_spans: dict[int, tuple[int, int]] = field(default_factory=dict)
    value_spans: dict[int, tuple[int, int]] = field(default_factory=dict)  # members only; an element has no key
    scalar_items: dict[int, list[int]] = field(default_factory=dict)  # block opening line -> its scalar members
    closing_column: dict[int, int] = field(default_factory=dict)  # closing line -> column of its bracket (JSON only)
    style: str = "json"
    warning: str | None = None
    refs: list[Ref] = field(default_factory=list)
    refs_by_line: dict[int, list[Ref]] = field(default_factory=dict)
    used_by: dict[tuple[str, ...], list[Ref]] = field(default_factory=dict)  # target path -> refs pointing at it

    def key_of(self, line: int) -> str | None:
        node = self.nodes.get(line)
        return node.key if node is not None else None

    def resolve_pointer(self, pointer: str) -> Node | None:
        """Follow a JSON Pointer from the root, e.g. '#/components/schemas/Pet'.

        Walking the node tree rather than the path text avoids having to reverse the way array indices are folded
        into their parent segment: a pointer says /servers/0, a path says servers[0].
        """
        if not pointer.startswith("#"):
            return None
        body = pointer[1:].lstrip("/")
        node = self.root
        if not body:
            return node

        for raw in body.split("/"):
            token = raw.replace("~1", "/").replace("~0", "~")  # in this order: ~01 must decode to ~1
            if node is None:
                return None
            if node.kind == "mapping":
                node = node.child(token)
            elif node.kind == "sequence" and token.isdigit() and int(token) < len(node.children):
                node = node.children[int(token)]
            else:
                return None
        return node

    def ref_at(self, line: int, column: int) -> Ref | None:
        for ref in self.refs_by_line.get(line, []):
            if ref.span[0] <= column < ref.span[1]:
                return ref
        return None

    def fold_end(self, opened: int) -> str | None:
        """Where a fold of this block should stop eliding.

        JSON stops at the closing bracket, leaving `"key": [ ... ],` readable on one line. YAML has no closing
        bracket to keep, so the fold runs to the end of the block's last line.
        """
        closed = self.blocks.close_of.get(opened)
        if closed is None:
            return None
        column = self.closing_column.get(closed)
        return f"{closed}.{column}" if column is not None else f"{closed}.end"


def index_refs(document: Document) -> None:
    """Find every $ref and resolve the internal ones, recording who points at what.

    Runs once the whole tree exists, since a pointer may name something defined further down the file.
    """
    for line, node in sorted(document.nodes.items()):
        if node.key != REF_KEY or not isinstance(node.value, str):
            continue
        span = document.value_spans.get(line) or (0, len(document.lines[line - 1]))
        ref = Ref(line, span, node.value)
        target = document.resolve_pointer(node.value)
        if target is not None:
            ref.target_line, ref.target_parts = target.line, target.parts
            document.used_by.setdefault(target.parts, []).append(ref)
        document.refs.append(ref)
        document.refs_by_line.setdefault(line, []).append(ref)


def _scalar_text(value: object) -> str:
    """A scalar the way json.dumps writes it, so the rendering matches jq character for character."""
    return json.dumps(value, ensure_ascii=False)


def _path_text(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if PLAIN_KEY_RE.match(key) else f'{prefix}["{key}"]'


def build_document(data: object) -> Document:
    """Render `data` and record its structure in the same walk.

    The layout is jq's, which is also json.dumps(indent=2): one member per line, two-space nesting, and an empty
    container kept inline. Scalars are handed to json.dumps so escaping and number formatting cannot drift.
    """
    document = Document()

    if isinstance(data, str):  # `jq -r .` prints a document that is just a string raw, over as many lines as it has
        document.lines = data.splitlines() or [""]
        document.root = Node("scalar", (), ".", None, 1, value=data)
        for line in range(1, len(document.lines) + 1):
            document.nodes[line] = document.root
            document.paths[line] = JsonPath(".", ())
            document.blocks.enclosing[line] = 0
        document.line_of_parts[()] = 1
        return document

    def emit(
        value: object, parts: tuple[str, ...], path: str, prefix: str, key: str | None, indent: int, parent: int
    ) -> Node:
        pad = " " * indent
        head = f"{pad}{_scalar_text(key)}: " if key is not None else pad
        line = len(document.lines) + 1
        document.blocks.enclosing[line] = parent
        if key is not None:
            document.key_spans[line] = (len(pad), len(pad) + len(_scalar_text(key)))

        if isinstance(value, dict) and value:
            node = Node("mapping", parts, path, key, line)
            document.lines.append(f"{head}{{")
            if key is not None:
                document.value_spans[line] = (len(head), len(head) + 1)
            for position, (child_key, child_value) in enumerate(value.items()):
                child = emit(
                    child_value,
                    (*parts, child_key),
                    _path_text(prefix, child_key),
                    _path_text(prefix, child_key),
                    child_key,
                    indent + JSON_INDENT,
                    line,
                )
                node.children.append(child)
                if position < len(value) - 1:
                    document.lines[-1] += ","
            node.close_line = len(document.lines) + 1
            document.blocks.enclosing[node.close_line] = line
            document.closing_column[node.close_line] = len(pad)
            document.lines.append(f"{pad}}}")
        elif isinstance(value, list) and value:
            node = Node("sequence", parts, path, key, line)
            document.lines.append(f"{head}[")
            if key is not None:
                document.value_spans[line] = (len(head), len(head) + 1)
            for position, item in enumerate(value):
                element = _element_parts(parts, position)
                child = emit(
                    item,
                    element,
                    f"{prefix}[{position}]",
                    f"{prefix}[{position}]",
                    None,
                    indent + JSON_INDENT,
                    line,
                )
                node.children.append(child)
                if position < len(value) - 1:
                    document.lines[-1] += ","
            node.close_line = len(document.lines) + 1
            document.blocks.enclosing[node.close_line] = line
            document.closing_column[node.close_line] = len(pad)
            document.lines.append(f"{pad}]")
        else:  # a scalar, or a container jq keeps on one line because it is empty
            if isinstance(value, dict):
                kind, body = "mapping", "{}"
            elif isinstance(value, list):
                kind, body = "sequence", "[]"
            else:
                kind, body = "scalar", _scalar_text(value)
            node = Node(kind, parts, path, key, line, value=value if kind == "scalar" else None)
            document.lines.append(head + body)
            if key is not None:
                document.value_spans[line] = (len(head), len(head) + len(body))

        document.nodes[line] = node
        document.paths[line] = JsonPath(path, parts)
        document.line_of_parts.setdefault(parts, line)
        if node.close_line is not None:
            document.blocks.close_of[line] = node.close_line
            document.blocks.open_of[node.close_line] = line
            document.scalar_items[line] = [child.line for child in node.children if child.is_scalar]
        return node

    document.root = emit(data, (), ".", "", None, 0, 0)
    index_refs(document)
    return document


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.geometry("1200x680")
        self.minsize(620, 300)

        self.current_json: list[str] = []
        self.status_lines: dict[str, list[int]] = {}
        self.status_paths: list[StatusEntry] = []
        self.worst_by_parts: dict[tuple[str, ...], StatusEntry] = {}
        self.color_origin: dict[int, list[ColorSpan]] = {}
        self.paths: dict[int, JsonPath] = {}
        self.value_of_line: dict[int, str] = {}
        self.line_of_parts: dict[tuple[str, ...], int] = {}
        self._tree_lines: dict[str, int] = {}
        self._tree_items: dict[tuple[str, ...], str] = {}
        self._syncing = False
        self._hover_after: str | None = None
        self._hover_key: object = None
        self.history = History()
        self._history_lock = False
        self._match_after: str | None = None
        self._match_origin: int | None = None
        self._setting_search = False
        self._match_lines: dict[str, int] = {}
        self.document = Document()
        self.blocks = self.document.blocks
        self.folded: set[int] = set()
        self.current_line: int | None = None
        self.reference_marks = 0
        self.marked_lines: set[int] = set()
        self._marked_sorted: list[int] = []
        self.duplicate_tokens: list[str] = []
        self._progress: ProgressWindow | None = None
        self.recent = RecentFiles(app_directory())
        self._path: Path | None = None

        # Only the content frame grows when the window is resized.
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=0)
        self.columnconfigure(0, weight=1)

        self._build_menu()
        self._configure_tree_style()
        self._build_content()
        self._build_status()

        self.bind("<Control-c>", lambda _event: self.copy_selection())
        self.bind("<Control-b>", lambda _event: self.copy_block())
        self.bind("<Control-C>", lambda _event: self.copy_path())  # Ctrl+Shift+C
        self.bind("<Control-a>", lambda _event: self.select_all())
        self.bind("<Alt-Left>", lambda _event: self.go_back())
        self.bind("<Alt-Right>", lambda _event: self.go_forward())
        self.bind("<Control-o>", lambda _event: self.open_json())
        self.bind("<Control-q>", lambda _event: self.destroy())

        self._update_title()
        self._update_counters()
        self.set_status(self.recent.error or "Ready \u2014 Files \u203a Open to load a JSON file")

    def _build_menu(self) -> None:
        menubar = tk.Menu(self, tearoff=False)

        files_menu = tk.Menu(menubar, tearoff=False)
        files_menu.add_command(label="Open\u2026", accelerator="Ctrl+O", command=self.open_json)
        self.recent_menu = tk.Menu(files_menu, tearoff=False)
        files_menu.add_cascade(label="Open Recent", menu=self.recent_menu)
        files_menu.add_command(label="Close", command=self.close_json)
        files_menu.add_separator()
        files_menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.destroy)
        menubar.add_cascade(label="Files", menu=files_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="Copy", accelerator="Ctrl+C", command=self.copy_selection)
        edit_menu.add_command(label="Copy Current Block", accelerator="Ctrl+B", command=self.copy_block)
        edit_menu.add_command(label="Copy Path", accelerator="Ctrl+Shift+C", command=self.copy_path)
        edit_menu.add_separator()
        edit_menu.add_command(label="Select All", accelerator="Ctrl+A", command=self.select_all)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_command(label="Back", accelerator="Alt+Left", command=self.go_back)
        view_menu.add_command(label="Forward", accelerator="Alt+Right", command=self.go_forward)
        view_menu.add_separator()
        view_menu.add_command(label="Collapse All", command=self.collapse_all)
        view_menu.add_command(label="Expand All", command=self.expand_all)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)
        self._rebuild_recent_menu()

    def _configure_tree_style(self) -> None:
        """Give both trees a style whose selection is light grey and leaves the row's own colour alone."""
        style = ttk.Style(self)
        try:
            inherited_background = style.map("Treeview", query_opt="background")
            inherited_foreground = style.map("Treeview", query_opt="foreground")
        except tk.TclError:  # a theme that exposes no map at all
            inherited_background, inherited_foreground = [], []

        style.configure(TREE_STYLE, background=BG_COLOR, fieldbackground=BG_COLOR, foreground=FG_COLOR)
        style.map(
            TREE_STYLE,
            background=[*without_selected(inherited_background), ("selected", TREE_SELECT_BG)],
            foreground=without_selected(inherited_foreground),
        )

    def _build_content(self) -> None:
        """The content frame: draggable splits, status paths left, text window centre, same-text matches right."""
        self.content = ttk.PanedWindow(self, orient="horizontal")
        self.content.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        self.font = ("TkFixedFont", 11)
        self.left_pane = ttk.Frame(self.content)
        middle = ttk.Frame(self.content)
        right = ttk.Frame(self.content)
        self.content.add(self.left_pane, weight=1)
        self.content.add(middle, weight=3)
        self.content.add(right, weight=1)

        self._build_list(self.left_pane)
        self._build_text(middle)
        self._build_matches(right)

    def _build_list(self, parent: ttk.Frame) -> None:
        """Search entry on top, path tree underneath."""
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        search = ttk.Frame(parent, padding=(0, 0, 0, 4))
        search.grid(row=0, column=0, columnspan=2, sticky="ew")
        search.columnconfigure(1, weight=1)
        self.search_negate = tk.BooleanVar(value=False)
        # Toolbutton makes a checkbutton look and latch like a button, which is what a NOT switch wants to be.
        ttk.Checkbutton(
            search,
            text="NOT",
            variable=self.search_negate,
            style="Toolbutton",
            width=4,
            command=self._on_search,
        ).grid(row=0, column=0, padx=(0, 4))
        self.search_var = tk.StringVar()
        ttk.Entry(search, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(search, text="\u2715", width=3, command=lambda: self.search_var.set("")).grid(row=0, column=2)
        self.search_var.trace_add("write", self._on_search)

        self.tree = ttk.Treeview(parent, show="tree", selectmode="browse", style=TREE_STYLE)
        self.tree.grid(row=1, column=0, sticky="nsew")

        tree_vbar = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        tree_vbar.grid(row=1, column=1, sticky="ns")
        tree_hbar = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        tree_hbar.grid(row=2, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=tree_vbar.set, xscrollcommand=tree_hbar.set)

        for value, color in STATUS_COLORS.items():
            self.tree.tag_configure(value, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _build_matches(self, parent: ttk.Frame) -> None:
        """Search entry on top, then the tree of every line holding the searched text."""
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)

        search = ttk.Frame(parent, padding=(0, 0, 0, 4))
        search.grid(row=0, column=0, columnspan=2, sticky="ew")
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="Find").grid(row=0, column=0, padx=(0, 4))
        self.match_search_var = tk.StringVar()
        entry = ttk.Entry(search, textvariable=self.match_search_var)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _event: self._run_match_search())
        ttk.Button(search, text="\u2715", width=3, command=lambda: self.match_search_var.set("")).grid(row=0, column=2)
        self.match_search_var.trace_add("write", self._on_match_search)

        self.match_caption = tk.StringVar(value="Type a word, or click one in quotes")
        ttk.Label(parent, textvariable=self.match_caption, anchor="w", padding=(0, 0, 0, 4)).grid(
            row=1, column=0, columnspan=2, sticky="ew"
        )

        self.matches_tree = ttk.Treeview(parent, show="tree", selectmode="browse", style=TREE_STYLE)
        self.matches_tree.grid(row=2, column=0, sticky="nsew")

        match_vbar = ttk.Scrollbar(parent, orient="vertical", command=self.matches_tree.yview)
        match_vbar.grid(row=2, column=1, sticky="ns")
        match_hbar = ttk.Scrollbar(parent, orient="horizontal", command=self.matches_tree.xview)
        match_hbar.grid(row=3, column=0, sticky="ew")
        self.matches_tree.configure(yscrollcommand=match_vbar.set, xscrollcommand=match_hbar.set)

        for value, color in STATUS_COLORS.items():
            self.matches_tree.tag_configure(value, foreground=color)
        self.matches_tree.bind("<<TreeviewSelect>>", self._on_match_select)

    def _build_text(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(3, weight=1)
        parent.columnconfigure(1, weight=1)

        bar = ttk.Frame(parent, padding=(0, 0, 0, 2))
        bar.grid(row=0, column=0, columnspan=3, sticky="ew")
        bar.columnconfigure(2, weight=1)
        self.back_button = ttk.Button(bar, text="\u25c0", width=3, command=self.go_back)
        self.back_button.grid(row=0, column=0)
        self.forward_button = ttk.Button(bar, text="\u25b6", width=3, command=self.go_forward)
        self.forward_button.grid(row=0, column=1, padx=(2, 6))
        self.history_choice = tk.StringVar()
        self.history_box = ttk.Combobox(bar, textvariable=self.history_choice, state="readonly", values=[])
        self.history_box.grid(row=0, column=2, sticky="ew")
        self.history_box.bind("<<ComboboxSelected>>", self._on_history_pick)

        # Rows 1-2 hold the clickable path of the current line and its scrollbar; the text starts on row 3. The
        # crumbs live in a frame inside a canvas, which is what lets a deep path scroll sideways instead of clipping.
        self.crumb_canvas = tk.Canvas(
            parent, height=22, highlightthickness=0, borderwidth=0, background=self.cget("background")
        )
        self.crumb_canvas.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.crumb_bar = ttk.Scrollbar(parent, orient="horizontal", command=self.crumb_canvas.xview)
        self.crumb_bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.crumb_canvas.configure(xscrollcommand=self.crumb_bar.set)

        self.breadcrumb = ttk.Frame(self.crumb_canvas, padding=(2, 0, 2, 2))
        self.crumb_canvas.create_window((0, 0), window=self.breadcrumb, anchor="nw")
        self.breadcrumb.bind("<Configure>", self._on_crumb_configure)
        self.crumb_canvas.bind("<Configure>", self._on_crumb_configure)

        # The gutter mirrors the text widget line for line, folds included, so the two scroll in lockstep.
        self.gutter = tk.Text(
            parent,
            width=6,
            wrap="none",
            font=self.font,
            foreground=GUTTER_FG,
            background=GUTTER_BG,
            cursor="hand2",
            takefocus=False,
            padx=4,
            pady=4,
            state="disabled",
            borderwidth=0,
            highlightthickness=0,
        )
        self.gutter.grid(row=3, column=0, sticky="ns")

        self.text = tk.Text(
            parent,
            wrap="none",  # keep the JSON indentation readable
            font=self.font,
            foreground=FG_COLOR,
            background=BG_COLOR,
            insertbackground=FG_COLOR,
            padx=6,
            pady=4,
            state="disabled",  # a viewer, not an editor
            borderwidth=0,
            highlightthickness=0,
        )
        self.text.grid(row=3, column=1, sticky="nsew")

        self.vbar = ttk.Scrollbar(parent, orient="vertical", command=self._on_vbar)
        self.vbar.grid(row=3, column=2, sticky="ns")
        self.hbar = ttk.Scrollbar(parent, orient="horizontal", command=self.text.xview)
        self.hbar.grid(row=4, column=1, sticky="ew")
        self.text.configure(yscrollcommand=self._on_text_scroll, xscrollcommand=self.hbar.set)

        # Stack the status tags lowest-first, so every overlap resolves to the worst status present: a block that is
        # both pass and fail reads red, and so does a key leading to both.
        previous = ""
        for value, color in STATUS_COLORS.items():
            self.text.tag_configure(value, foreground=color)
            if previous:
                self.text.tag_raise(value, previous)
            previous = value
        # Component references, raised above every status colour so the tie to the component wins on a line some
        # other status has already coloured.
        for value, color in STATUS_COLORS.items():
            self.text.tag_configure(REFERENCE_TAGS[value], foreground=color, underline=UNDERLINE_REFERENCES)
            self.text.tag_raise(REFERENCE_TAGS[value])
        # A $ref that resolves is drawn as a link; one that does not is marked but not inviting.
        self.text.tag_configure("reflink", foreground=REF_COLOR, underline=True)
        self.text.tag_configure("refdead", foreground=DEAD_REF_COLOR)
        self.text.tag_raise("reflink")
        self.text.tag_raise("refdead")
        # Both of these paint only a background, so they sit on top without disturbing the foreground priority.
        self.text.tag_configure("reveal", background=REVEAL_BG)
        self.text.tag_configure("dictvalue", background=VALUE_BG)

        self.gutter.bind("<Button-1>", self._on_gutter_click)
        self.text.bind("<Double-Button-1>", self._on_text_double_click)
        self.text.bind("<ButtonRelease-1>", self._on_text_click)
        self.tooltip = Tooltip(self)
        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="Copy", command=self.copy_selection)
        self.context_menu.add_command(label="Copy Current Block", command=self.copy_block)
        self.context_menu.add_command(label="Copy Path", command=self.copy_path)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Select All", command=self.select_all)
        for sequence in ("<Button-3>", "<Button-2>"):  # right button, and the middle one on some window managers
            self.text.bind(sequence, self._on_context_menu)

        self.text.bind("<Motion>", self._on_text_motion)
        self.text.bind("<Leave>", lambda _event: self._cancel_hover())
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.gutter.bind(sequence, self._on_gutter_wheel)

    def _build_status(self) -> None:
        bar = ttk.Frame(self, relief="sunken", padding=(6, 2))
        bar.grid(row=1, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)

        self._message = tk.StringVar(value="")
        self._counters = tk.StringVar(value="")

        ttk.Label(bar, textvariable=self._message, anchor="w").grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, textvariable=self._counters, anchor="e").grid(row=0, column=1, sticky="e")

    # -------------------------------------------------------------------------------------------------- state --

    def set_status(self, message: str) -> None:
        self._message.set(message)

    def _update_counters(self) -> None:
        tally = ", ".join(f"{len(self.status_lines.get(value, []))} {value}" for value in STATUS_COLORS)
        hits = sum(len(numbers) for numbers in self.status_lines.values())
        total = len(self.current_json)
        where = f"line {self.current_line} of {total}" if self.current_line else f"{total} lines"
        self._counters.set(f"{where} \u2022 {hits} status ({tally}) \u2022 {len(self.folded)} folded")

    def _update_title(self) -> None:
        name = self._path.name if self._path else "no file"
        self.title(f"{name} \u2014 {APP_NAME}")

    # ------------------------------------------------------------------------------------------------ loading --

    def open_json(self) -> None:
        """Ask for a file, starting wherever the last one came from."""
        name = filedialog.askopenfilename(
            title="Open JSON or YAML file",
            initialdir=str(self.recent.paths[0].parent) if self.recent.paths else "",
            filetypes=[
                ("JSON and YAML", "*.json *.yaml *.yml"),
                ("JSON", "*.json"),
                ("YAML", "*.yaml *.yml"),
                ("All files", "*.*"),
            ],
        )
        if not name:
            self.set_status("Open cancelled")
            return
        self.open_path(Path(name))

    def open_path(self, path: Path) -> None:
        """Load `path`, or drop it from the recent list if it will not open any more."""
        self._progress = ProgressWindow(self, len(PROCESSING_STEPS)) if file_size(path) > LARGE_FILE_BYTES else None
        try:
            self._step("Reading file")
            try:
                document = load_document(path)
            except DocumentError as exc:
                messagebox.showerror(APP_NAME, f"Could not open {path.name}:\n\n{exc}")
                self.set_status(f"Failed: {path.name}")
                self.recent.remove(path)
                self._rebuild_recent_menu()
                return

            lines, paths = document.lines, document.paths

            self._step("Indexing status lines")
            self.status_lines = collect_status_lines(document)
            self.value_of_line = {line: value for value, numbers in self.status_lines.items() for line in numbers}

            self._step("Resolving JSON paths")
            self.status_paths = collect_status_paths(paths, self.status_lines)
            # Recorded as the document was rendered; the earliest line wins, so an ancestor node jumps to where its
            # container opens.
            self.line_of_parts = document.line_of_parts

            self._index_severity()

            self._step("Reading the structure")
            self.current_json = lines
            self.paths = paths
            self.document = document
            self.blocks = document.blocks
            self.folded = set()
            self._path = path

            collapsed = self._render()
        finally:
            self._end_progress()

        self._update_title()
        self._update_counters()
        self.recent.add(path)
        self._rebuild_recent_menu()
        self.set_status(f"Opened {path} \u2014 {self._load_report(collapsed)}")

    def open_argument(self, path: Path) -> None:
        """Open the file named by --file, refusing anything view-json-yaml does not read."""
        if not is_supported_file(path):
            accepted = ", ".join(JSON_SUFFIXES + YAML_SUFFIXES)
            messagebox.showwarning(APP_NAME, f"Ignoring {path}:\n\nonly {accepted} files are accepted.")
            self.set_status(f"Ignored {path.name}: not one of {accepted}")
            return
        self.open_path(path)

    def _step(self, label: str) -> None:
        """Report a stage, when there is a progress window listening."""
        if self._progress is not None:
            self._progress.step(label)

    def _end_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None

    def _rebuild_recent_menu(self) -> None:
        """Redraw the Open Recent submenu from the store."""
        self.recent_menu.delete(0, "end")
        if not self.recent.paths:
            self.recent_menu.add_command(label="(nothing yet)", state="disabled")
            return
        for position, entry in enumerate(self.recent.paths, start=1):
            self.recent_menu.add_command(
                label=f"{position}  {shorten_home(entry)}",
                command=lambda target=entry: self.open_path(target),
            )
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Clear List", command=self._clear_recent)

    def _clear_recent(self) -> None:
        self.recent.clear()
        self._rebuild_recent_menu()
        self.set_status(self.recent.error or "Recent file list cleared")

    def _load_report(self, collapsed: int) -> str:
        """A short account of what the load did, so an unmarked file says why rather than just looking plain."""
        notes = [f"collapsed {collapsed}"] if collapsed else []
        for container in (COMPONENTS_KEY, VIOLATIONS_KEY):
            entries = self._metadata_entries(container)
            if not entries:
                notes.append(f"no {METADATA_KEY}.{container}")
                continue
            coloured = sum(1 for parts, _, _ in entries if self._worst_status_under(parts) is not None)
            notes.append(f"{coloured}/{len(entries)} {container} coloured")
        plural = "reference" if self.reference_marks == 1 else "references"
        notes.append(f"{self.reference_marks} {plural} coloured")
        if self.duplicate_tokens:
            notes.append(f"{len(self.duplicate_tokens)} ids used twice: {', '.join(self.duplicate_tokens[:3])}")
        return ", ".join(notes)

    def close_json(self) -> None:
        self.current_json = []
        self.status_lines = {}
        self.status_paths = []
        self.worst_by_parts = {}
        self.paths = {}
        self.value_of_line = {}
        self.line_of_parts = {}
        self.document = Document()
        self.blocks = self.document.blocks
        self.folded = set()
        self._path = None

        self._render()
        self._update_title()
        self._update_counters()
        self.set_status("Closed")

    def _render(self) -> int:
        """Fill both windows from current_json, colour them, and apply the default folds. Returns folds applied."""
        self._step("Filling the text window")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        # Joined rather than one insert per line: no trailing blank line, so widget line N stays current_json[N-1]
        # and the gutter matches line for line.
        self.text.insert("1.0", "\n".join(self.current_json))
        self.text.mark_set("insert", "1.0")
        self.text.see("1.0")
        self.text.configure(state="disabled")

        self._step("Colouring")
        self._apply_colors()
        self._step("Applying default folds")
        collapsed = self._collapse_defaults()
        self._step("Building the path trees")
        self._fill_tree()
        self._clear_matches("Type a word, or click one in quotes")
        self._set_current_line(None)
        self.history.clear()
        self._refresh_history()
        self._refresh_gutter()
        return collapsed

    def _collapse_defaults(self) -> int:
        """Fold every container opened by one of AUTO_COLLAPSE_KEYS.

        Only lists qualify: the opening line has to end in "[", so a dict under the same name stays expanded. A list
        holding a coloured reference is left open too - folding it would hide the very cross-reference rule K draws.
        Runs after _apply_colors so each fold mark inherits its key's colour, and innermost first so every insert
        happens while the enclosing blocks are still expanded.
        """
        targets = [
            opened
            for opened in sorted(self.blocks.close_of, reverse=True)
            if self.document.key_of(opened) in AUTO_COLLAPSE_KEYS
            and self.current_json[opened - 1].rstrip().endswith("[")
            and not self._holds_reference(opened, self.blocks.close_of[opened])
        ]
        for opened in targets:
            self.fold(opened)
        return len(targets)

    def _apply_colors(self) -> None:
        """Colour each status block, then the dict keys on the way down to it.

        Every status paints with its own tag and the priority set in _build_text settles the overlaps, so a failing
        block nested inside a passing one stays red without any special-casing here. Ranges are gathered per tag and
        applied in a single tag_add: on a large report this is the difference between six Tcl calls and a hundred
        thousand of them.
        """
        self._index_severity()
        for tag in (*STATUS_COLORS, "reveal", "dictvalue"):
            self.text.tag_remove(tag, "1.0", "end")

        ranges: dict[str, list[str]] = {value: [] for value in STATUS_COLORS}
        self.color_origin = {}
        for value in STATUS_COLORS:
            for line in self.status_lines.get(value, []):
                origin = self.paths.get(line)
                origin_text = origin.text if origin is not None else "?"
                span = self.blocks.span_around(line)
                if span is None:  # a status outside any block: colour the line itself and stop
                    ranges[value] += [f"{line}.0", f"{line}.end"]
                    self._record_origin(line, 0, len(self.current_json[line - 1]), value, origin_text, line)
                    continue
                first, last = span
                for item in self._scalar_items(first, last):
                    ranges[value] += [f"{item}.0", f"{item}.end"]
                    self._record_origin(item, 0, len(self.current_json[item - 1]), value, origin_text, line)
                for ancestor, start, end in self._breadcrumb_spans(first):
                    ranges[value] += [f"{ancestor}.{start}", f"{ancestor}.{end}"]
                    self._record_origin(ancestor, start, end, value, origin_text, line)

        for value, indices in ranges.items():
            if indices:
                self.text.tag_add(value, *indices)

        self.reference_marks = self._color_references()
        self._mark_refs()

    def _scalar_items(self, block_open: int, block_close: int) -> list[int]:
        """Lines of the block's own items whose value is not a list or a dict.

        Nested containers are left alone: their lines belong to a deeper block and take their colour from that
        block's own status, which is what keeps a failing sub-tree from being repainted by a passing parent.
        """
        return self.document.scalar_items.get(block_open, [])

    def _reference_targets(self) -> dict[str, tuple[str, StatusEntry]]:
        """Every token to chase through the buffer, mapped to the status colour it should take.

        Each token maps to its colour and to the status field that chose it, so a hover over a reference can name
        the status responsible even though it sits far away in the document.

        Components contribute their uuid, which is the key they hang from; violations contribute their rule_id, a
        string that is unique per violation. Uniqueness is the caller's guarantee, not something to rely on blindly:
        a repeat is recorded in `duplicate_tokens` and settled with the usual ladder rather than silently taking
        whichever came last.
        """
        targets: dict[str, tuple[str, StatusEntry]] = {}
        self.duplicate_tokens = []
        sources = (
            (COMPONENTS_KEY, self._component_uuid),
            (VIOLATIONS_KEY, lambda parts, first, last: self._member_value(first, last, RULE_ID_KEY)),
        )
        for container, identify in sources:
            for parts, first, last in self._metadata_entries(container):
                origin = self._origin_under(parts)
                token = identify(parts, first, last)
                if origin is None or not token:
                    continue
                held = targets.get(token)
                if held is not None and token not in self.duplicate_tokens:
                    self.duplicate_tokens.append(token)
                if held is None or SEVERITY[origin.value] > SEVERITY[held[0]]:
                    targets[token] = (origin.value, origin)
        return targets

    def _color_references(self) -> int:
        """Paint every occurrence of a tracked token, wherever it appears in the buffer.

        Most ids are word-ish, so the buffer is scanned for word runs and each run looked up in a dict: one pass,
        independent of how many components a report carries. An alternation would be quadratic here - Python's re
        tries every branch at every position, and ten thousand branches over a large file takes minutes. Ids holding
        punctuation (a purl, say) cannot be found that way and fall back to a small alternation of their own.
        """
        for tag in REFERENCE_TAGS.values():
            self.text.tag_remove(tag, "1.0", "end")
        self.marked_lines = set()
        self._marked_sorted = []

        targets = self._reference_targets()
        if not targets:
            return 0

        simple = {token: pair for token, pair in targets.items() if SIMPLE_TOKEN_RE.match(token)}
        awkward = sorted((token for token in targets if token not in simple), key=len, reverse=True)
        pattern = None
        if awkward:
            alternation = "|".join(re.escape(token) for token in awkward)
            pattern = re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])")

        ranges: dict[str, list[str]] = {value: [] for value in STATUS_COLORS}
        marks = 0
        for number, line in enumerate(self.current_json, start=1):
            for found in CANDIDATE_RE.finditer(line):
                pair = simple.get(found.group())
                if pair is not None:
                    value, origin = pair
                    ranges[value] += [f"{number}.{found.start()}", f"{number}.{found.end()}"]
                    self._record_origin(number, found.start(), found.end(), value, origin.path, origin.line)
                    self.marked_lines.add(number)
                    marks += 1
            if pattern is not None:
                for found in pattern.finditer(line):
                    value, origin = targets[found.group()]
                    ranges[value] += [f"{number}.{found.start()}", f"{number}.{found.end()}"]
                    self._record_origin(number, found.start(), found.end(), value, origin.path, origin.line)
                    self.marked_lines.add(number)
                    marks += 1

        # One call per tag: a report with tens of thousands of references would otherwise pay that many Tcl trips.
        for value, indices in ranges.items():
            if indices:
                self.text.tag_add(REFERENCE_TAGS[value], *indices)

        self._marked_sorted = sorted(self.marked_lines)
        return marks

    def _component_uuid(self, parts: tuple[str, ...], block_open: int, block_close: int) -> str | None:
        """The component's uuid: the dict key it hangs from, or a "uuid" member when components is a list."""
        if len(parts) >= 3 and parts[-2] == COMPONENTS_KEY and parts[-3] == METADATA_KEY:
            return parts[-1]  # ....metadata.components.<component_uuid>
        return self._member_value(block_open, block_close, UUID_KEY)

    def _metadata_entries(self, container: str) -> list[tuple[tuple[str, ...], int, int]]:
        """Every block inside a metadata.<container>, as (path segments, opening line, closing line).

        The path is matched on its tail, so report.metadata.components.<uuid> and a bare metadata.components.<uuid>
        both qualify. Both container shapes are accepted: a dict keyed by uuid, and a list of objects.
        """
        found = []
        for number, path in sorted(self.paths.items()):
            parts = path.parts
            keyed = len(parts) >= 3 and parts[-2] == container and parts[-3] == METADATA_KEY
            element = (
                len(parts) >= 2
                and parts[-2] == METADATA_KEY
                and _base_segment(parts[-1]) == container
                and parts[-1] != container
            )
            closed = self.blocks.close_of.get(number)
            if (keyed or element) and closed is not None:
                found.append((parts, number, closed))
        return found

    def _index_severity(self) -> None:
        """Precompute the worst status under every path prefix.

        Scanning the status list per component is quadratic - ten thousand components against ten thousand statuses
        is a hundred million comparisons, enough to hang the window on a real report. Walking each status up its own
        prefixes instead costs one pass and turns the lookup into a dict hit.
        """
        worst: dict[tuple[str, ...], StatusEntry] = {}
        for entry in self.status_paths:
            if entry.value not in SEVERITY:
                continue
            for depth in range(len(entry.parts) + 1):
                prefix = entry.parts[:depth]
                held = worst.get(prefix)
                if held is None or SEVERITY[entry.value] > SEVERITY[held.value]:
                    worst[prefix] = entry
        self.worst_by_parts = worst

    def _worst_status_under(self, parts: tuple[str, ...]) -> str | None:
        """The most severe status anywhere inside the subtree at `parts`, or None if it holds no status at all."""
        entry = self.worst_by_parts.get(parts)
        return entry.value if entry is not None else None

    def _origin_under(self, parts: tuple[str, ...]) -> StatusEntry | None:
        """The status field that decided the colour of the subtree at `parts`."""
        return self.worst_by_parts.get(parts)

    def _member_value(self, block_open: int, block_close: int, key: str) -> str | None:
        """The string value of `key` in the block: a direct member if there is one, otherwise anywhere below.

        Read from the parsed tree, so a value is whatever the document said it was - no re-reading of the rendered
        line, and no confusion over quoting or escapes.
        """
        node = self.document.nodes.get(block_open)
        if node is None:
            return None

        direct = node.child(key)
        if direct is not None:
            return direct.value if isinstance(direct.value, str) else None

        pending = list(node.children)
        while pending:  # breadth first, so the shallowest match wins
            current = pending.pop(0)
            if current.key == key and isinstance(current.value, str):
                return current.value
            pending.extend(current.children)
        return None

    def _mark_refs(self) -> None:
        """Underline every $ref that resolves inside this document, and mark the ones that do not."""
        for tag in ("reflink", "refdead"):
            self.text.tag_remove(tag, "1.0", "end")

        ranges: dict[str, list[str]] = {"reflink": [], "refdead": []}
        for ref in self.document.refs:
            tag = "reflink" if ref.resolved else "refdead"
            ranges[tag] += [f"{ref.line}.{ref.span[0]}", f"{ref.line}.{ref.span[1]}"]
        for tag, indices in ranges.items():
            if indices:
                self.text.tag_add(tag, *indices)

    def _breadcrumb_spans(self, block_open: int) -> list[tuple[int, int, int]]:
        """(line, start column, end column) for the "key" token of the coloured block and every block enclosing it.

        The block's own line comes first, so a dict reached by a key keeps that key coloured; array elements and the
        document root open with a bare bracket, contribute no key, and are skipped.
        """
        found = []
        for ancestor in (block_open, *self.blocks.ancestors(block_open)):
            span = self.document.key_spans.get(ancestor)
            if span is not None:
                found.append((ancestor, span[0], span[1]))
        return found

    def _record_origin(self, line: int, start: int, end: int, value: str, origin_path: str, origin_line: int) -> None:
        """Note which status field coloured a stretch of a line, for the hover to report later."""
        self.color_origin.setdefault(line, []).append(ColorSpan(start, end, value, origin_path, origin_line))

    def _fill_path_tree(self, tree: ttk.Treeview, rows: list[TreeRow]) -> TreeIndex:
        """Render `rows` as a path tree in `tree`, returning the item -> line map its selection handler needs.

        Shared by both panes: nodes are created per path prefix, so entries with a common ancestry collapse into one
        branch, and an ancestor node takes the worst status below it, matching the breadcrumb colours in the text.
        Only the first MAX_TREE_ROWS entries are built; a report can carry far more statuses than a tree can usefully
        show, and the rest are reachable through the search box.
        """
        tree.delete(*tree.get_children())
        nodes: dict[tuple[str, ...], str] = {(): ""}  # "" is the Treeview root
        rank: dict[tuple[str, ...], str] = {}
        lines: dict[str, int] = {}

        shown, dropped = rows[:MAX_TREE_ROWS], max(0, len(rows) - MAX_TREE_ROWS)
        for row in shown:
            for depth in range(1, len(row.parts) + 1):
                prefix = row.parts[:depth]
                if prefix not in nodes:
                    leaf = depth == len(row.parts)
                    nodes[prefix] = tree.insert(nodes[prefix[:-1]], "end", text=row.label if leaf else prefix[-1])
                    tree.item(nodes[prefix], open=True)
                    line = row.line if leaf else self.line_of_parts.get(prefix)
                    if line is not None:
                        lines[nodes[prefix]] = line
                if row.value in SEVERITY and SEVERITY[row.value] > SEVERITY.get(rank.get(prefix, ""), -1):
                    rank[prefix] = row.value

        for prefix, item in nodes.items():
            if prefix in rank:
                tree.item(item, tags=(rank[prefix],))
        if dropped:
            tree.insert("", "end", text=f"\u2026 {dropped:,} more, narrow with the search box")
        # The empty path maps to "", the Treeview's own root, which is not a selectable item: leave it out.
        return TreeIndex(lines, {parts: item for parts, item in nodes.items() if parts})

    def _show_left_pane(self, wanted: bool) -> None:
        """Give the left pane back its space when there is nothing to list.

        Driven by whether the document holds any status paths at all, not by the search filter: a query that matches
        nothing should leave the pane in place rather than have it vanish mid-keystroke.
        """
        present = str(self.left_pane) in self.content.panes()
        if wanted and not present:
            self.content.insert(0, self.left_pane, weight=1)
        elif not wanted and present:
            self.content.forget(self.left_pane)

    def _fill_tree(self) -> int:
        """Rebuild the status-path tree from the entries the search keeps.

        With NOT latched the test is inverted, so the tree lists the paths that do *not* mention the pattern - the
        way to ask "which components are not npm", say. An empty box filters nothing either way: every line contains
        the empty string, so negating it would blank the tree rather than mean anything.
        """
        pattern = self.search_var.get().strip().lower()
        negate = bool(self.search_negate.get())
        rows = [
            TreeRow(entry.parts, f"{entry.parts[-1]} = {entry.value}  (line {entry.line})", entry.value, entry.line)
            for entry in self.status_paths
            if entry.parts and (not pattern or (pattern in f"{entry.path} = {entry.value}".lower()) != negate)
        ]
        index = self._fill_path_tree(self.tree, rows)
        self._tree_lines, self._tree_items = index.lines, index.items
        self._show_left_pane(bool(self.status_paths))
        return len(rows)

    def _on_match_search(self, *_args: str) -> None:
        """Queue a search. Typed text has no "search item", so any marker from an earlier click is dropped."""
        if not self._setting_search:
            self._match_origin = None
        if self._match_after is not None:
            self.after_cancel(self._match_after)
        self._match_after = self.after(MATCH_SEARCH_DELAY_MS, self._run_match_search)

    def _run_match_search(self) -> None:
        """Run whatever is in the box now, cancelling any queued run."""
        if self._match_after is not None:
            self.after_cancel(self._match_after)
            self._match_after = None
        needle = self.match_search_var.get().strip().strip('"')
        if not needle:
            self._clear_matches("Type a word, or click one in quotes")
            return
        self._show_matches(needle, self._match_origin)

    def _search_for(self, needle: str, origin: int | None) -> None:
        """Put `needle` in the box and search at once, without waiting for the typing pause."""
        self._match_origin = origin
        self._setting_search = True
        self.match_search_var.set(needle)
        self._setting_search = False
        self._run_match_search()

    def _show_matches(self, needle: str, origin: int | None) -> None:
        """Fill the right-hand tree with the path of every line containing `needle`, the selected one included.

        Matching is plain containment against the raw line, so the word is found whether it is used as a key or as a
        value. Lines that name nothing - a bare closing bracket - have no path and are skipped.
        """
        rows: list[TreeRow] = []
        for number, line in enumerate(self.current_json, start=1):
            column = line.find(needle)
            if column < 0:
                continue
            path = self.paths.get(number)
            if path is None or not path.parts:
                continue
            mark = "  \u25c2 selected" if origin is not None and number == origin else ""
            label = f"{path.parts[-1]}  (line {number}){mark}"
            rows.append(TreeRow(path.parts, label, self._match_color(number, column), number))

        self._match_lines = self._fill_path_tree(self.matches_tree, rows).lines
        shortened = needle if len(needle) <= 32 else needle[:31] + "\u2026"
        phrase = "line contains" if len(rows) == 1 else "lines contain"
        self.match_caption.set(f'{len(rows)} {phrase} "{shortened}"')
        self.set_status(f'{len(rows)} {phrase} "{needle}"')

    def _match_color(self, line: int, column: int) -> str | None:
        """The colour this hit carries in the text window, so the tree reads the same way.

        The colour on the matched word wins, since that is what the eye lands on; failing that the line's own status
        is used, which covers a hit on a line the text leaves black.
        """
        spans = self.origins_at(line, column)
        if spans:
            return spans[0].value
        return self.value_of_line.get(line)

    def _clear_matches(self, caption: str) -> None:
        self.matches_tree.delete(*self.matches_tree.get_children())
        self._match_lines = {}
        self.match_caption.set(caption)

    def _on_search(self, *_args: str) -> None:
        shown = self._fill_tree()
        total = len(self.status_paths)
        pattern = self.search_var.get().strip()
        if not pattern:
            self.set_status(f"{total} status paths")
            return
        sense = "not matching" if self.search_negate.get() else "matching"
        self.set_status(f"{shown} of {total} status paths {sense} {pattern!r}")

    # ------------------------------------------------------------------------------------------------ folding --

    def _fold_tag(self, opened: int) -> str:
        return f"fold{opened}"

    def toggle_fold(self, line: int) -> None:
        opened = self.blocks.opening_line(line)
        if opened is None:
            return
        if opened in self.folded:
            self.unfold(opened)
        else:
            self.fold(opened)
        self._set_gutter_arrow(opened)
        self._update_counters()

    def fold(self, opened: int) -> None:
        closed = self.blocks.close_of.get(opened)
        if closed is None or opened in self.folded:
            return

        fold_end = self.document.fold_end(opened)
        if fold_end is None:
            return
        tag = self._fold_tag(opened)

        self.text.configure(state="normal")
        # Take the colour from the key, which is the only coloured part of a container's opening line.
        span = self.document.key_spans.get(opened)
        probe = f"{opened}.{span[0]}" if span is not None else f"{opened}.end - 1c"
        inherited = [name for name in self.text.tag_names(probe) if name in STATUS_COLORS]
        self.text.insert(f"{opened}.end", FOLD_MARK, inherited)
        # Elide up to the closing bracket, not past it: the collapsed line reads `"cases": [ ... ],`.
        self.text.tag_add(tag, f"{opened}.end", fold_end)
        self.text.tag_configure(tag, elide=True)
        self.text.configure(state="disabled")

        self.gutter.tag_add(tag, f"{opened}.end", f"{closed}.end")
        self.gutter.tag_configure(tag, elide=True)
        self.folded.add(opened)

    def unfold(self, opened: int) -> None:
        if opened not in self.folded:
            return

        tag = self._fold_tag(opened)
        self.text.configure(state="normal")
        self.text.tag_delete(tag)
        # The mark sits at the end of the opening logical line, so plain index arithmetic finds it.
        self.text.delete(f"{opened}.end - {len(FOLD_MARK)}c", f"{opened}.end")
        self.text.configure(state="disabled")

        self.gutter.tag_delete(tag)
        self.folded.discard(opened)

    def collapse_all(self) -> None:
        # Innermost first, so every insert happens while the outer blocks are still expanded.
        for opened in sorted(self.blocks.close_of, reverse=True):
            self.fold(opened)
        self._refresh_gutter()
        self._update_counters()
        self.set_status(f"Collapsed {len(self.folded)} blocks")

    def expand_all(self) -> None:
        count = len(self.folded)
        for opened in sorted(self.folded):
            self.unfold(opened)
        self._refresh_gutter()
        self._update_counters()
        self.set_status(f"Expanded {count} blocks")

    def _holds_reference(self, block_open: int, block_close: int) -> bool:
        """Whether any marked line falls inside the block, by binary search rather than a scan per candidate."""
        if not KEEP_MARKED_LISTS_OPEN or not self._marked_sorted:
            return False
        position = bisect.bisect_left(self._marked_sorted, block_open)
        return position < len(self._marked_sorted) and self._marked_sorted[position] <= block_close

    def _set_gutter_arrow(self, line: int) -> None:
        """Repaint one fold arrow in place.

        Rewriting the whole gutter costs a full delete and re-insert of every line number, which is measurable on a
        large report; a single fold only ever changes one character.
        """
        arrow = ARROW_CLOSED if line in self.folded else ARROW_OPEN
        self.gutter.configure(state="normal")
        self.gutter.delete(f"{line}.0", f"{line}.1")
        self.gutter.insert(f"{line}.0", arrow)
        self.gutter.configure(state="disabled")

    def _refresh_gutter(self) -> None:
        """Redraw the line numbers and fold arrows, then mirror the current folds."""
        width = max(2, len(str(len(self.current_json))))
        rows: list[str] = []
        for number in range(1, len(self.current_json) + 1):
            opened = self.blocks.opening_line(number)
            if opened is None:
                arrow = " "
            elif opened in self.folded:
                arrow = ARROW_CLOSED
            else:
                arrow = ARROW_OPEN
            rows.append(f"{arrow}{number:>{width}} ")

        self.gutter.configure(state="normal", width=width + 2)
        self.gutter.delete("1.0", "end")
        self.gutter.insert("1.0", "\n".join(rows))
        for opened in self.folded:
            closed = self.blocks.close_of[opened]
            tag = self._fold_tag(opened)
            self.gutter.tag_add(tag, f"{opened}.end", f"{closed}.end")
            self.gutter.tag_configure(tag, elide=True)
        self.gutter.configure(state="disabled")
        self.gutter.yview_moveto(self.text.yview()[0])

    # --------------------------------------------------------------------------------------------- breadcrumb --

    def breadcrumb_items(self, line: int | None) -> list[tuple[str, int | None]]:
        """The path of `line` as (label, line to jump to) pairs, root first.

        A line that names nothing of its own - a closing bracket, say - borrows the path of the block holding it, so
        the bar never goes blank while scrolling through a large document.
        """
        if line is None:
            return []
        items: list[tuple[str, int | None]] = [(".", 1 if self.current_json else None)]
        for depth in range(1, len(self._parts_for_line(line)) + 1):
            prefix = self._parts_for_line(line)[:depth]
            items.append((prefix[-1], self.line_of_parts.get(prefix)))
        return items

    def _parts_for_line(self, line: int) -> tuple[str, ...]:
        path = self.paths.get(line)
        if path is not None:
            return path.parts
        enclosing = self.blocks.enclosing.get(line, 0)
        while enclosing:
            path = self.paths.get(enclosing)
            if path is not None:
                return path.parts
            enclosing = self.blocks.enclosing.get(enclosing, 0)
        return ()

    def _update_breadcrumb(self, line: int | None) -> None:
        """Redraw the path bar, one clickable label per element."""
        for child in self.breadcrumb.winfo_children():
            child.destroy()

        items = self.breadcrumb_items(line)
        if not items:
            ttk.Label(self.breadcrumb, text="No line selected", foreground=GUTTER_FG).pack(side="left")
            return

        for position, (label, target) in enumerate(items):
            if position:
                ttk.Label(self.breadcrumb, text=" \u203a ", foreground=GUTTER_FG).pack(side="left")
            crumb = ttk.Label(
                self.breadcrumb,
                text=label,
                foreground=LINK_COLOR if target else FG_COLOR,
                cursor="hand2" if target else "",
            )
            crumb.pack(side="left")
            if target is not None:
                # Deferred: the handler rebuilds this bar, so the label must not be destroyed inside its own callback.
                crumb.bind("<Button-1>", lambda _event, jump=target: self.after_idle(self.go_to, jump))

        self.after_idle(self._on_crumb_configure)

    def _on_crumb_configure(self, _event: tk.Event | None = None) -> None:
        """Resize the canvas to its crumbs and show the scrollbar only when they overflow."""
        width = self.breadcrumb.winfo_reqwidth()
        height = self.breadcrumb.winfo_reqheight()
        self.crumb_canvas.configure(scrollregion=(0, 0, width, height), height=height)
        if width > self.crumb_canvas.winfo_width():
            self.crumb_bar.grid()
        else:
            self.crumb_bar.grid_remove()
            self.crumb_canvas.xview_moveto(0)

    # ---------------------------------------------------------------------------------------------- revealing --

    def reveal_line(self, line: int) -> None:
        """Unfold everything hiding `line`, scroll to it, and mark it."""
        reopened = [opened for opened in self.blocks.ancestors(line) if opened in self.folded]
        for opened in reopened:
            self.unfold(opened)
        for opened in reopened:
            self._set_gutter_arrow(opened)
        self._update_counters()

        self._set_current_line(line)
        self.text.mark_set("insert", f"{line}.0")
        self.text.see(f"{line}.0")

    def _set_current_line(self, line: int | None) -> None:
        """Mark `line` as the selection and put its path in the breadcrumb bar."""
        self.current_line = line
        self.text.tag_remove("reveal", "1.0", "end")
        if line is not None:
            self.text.tag_add("reveal", f"{line}.0", f"{line}.end")
        self._update_breadcrumb(line)
        self._update_counters()

    def _sync_tree_to_line(self, line: int) -> bool:
        """Select the left-tree node for `line`, or for the nearest block above it that has one.

        The tree only holds paths that carry a status, and it may be filtered or capped, so an exact hit is the
        exception. Trimming one segment at a time walks back up the JSON tree until a node exists.
        """
        parts = self._parts_for_line(line)
        while True:
            item = self._tree_items.get(parts)
            if item is not None:
                self._select_quietly(item)  # opens whatever was folded over it
                return True
            if not parts:
                return False
            parts = parts[:-1]

    def _select_quietly(self, item: str) -> int:
        """Move the left-tree selection without letting it scroll the text window back.

        Any ancestor the user has collapsed is opened first, otherwise the row would be selected while still hidden.
        Returns how many nodes had to be unfolded.
        """
        unfolded = self._unfold_to(item)
        self._syncing = True
        self.tree.selection_set(item)
        self.tree.see(item)
        self.after_idle(self._release_sync)
        return unfolded

    def _unfold_to(self, item: str) -> int:
        """Open every collapsed ancestor of `item`, innermost last, so the row becomes visible."""
        ancestors = []
        parent = self.tree.parent(item)
        while parent:
            ancestors.append(parent)
            parent = self.tree.parent(parent)

        unfolded = 0
        for ancestor in reversed(ancestors):
            if not self._tree_is_open(ancestor):
                self.tree.item(ancestor, open=True)
                unfolded += 1
        return unfolded

    def _tree_is_open(self, item: str) -> bool:
        """Tk reports this as 1/0, "1"/"0" or a bool depending on the build, so normalise before trusting it."""
        return str(self.tree.item(item, "open")).lower() in ("1", "true")

    def _release_sync(self) -> None:
        self._syncing = False
        self._hover_after: str | None = None
        self._hover_key: object = None
        self.history = History()
        self._history_lock = False
        self._match_after: str | None = None
        self._match_origin: int | None = None
        self._setting_search = False

    def _on_tree_select(self, _event: tk.Event) -> None:
        if self._syncing:  # the selection came from the text window, not from the user
            return
        self._select_from(self.tree, self._tree_lines)

    def _on_match_select(self, _event: tk.Event) -> None:
        self._select_from(self.matches_tree, self._match_lines)

    def go_to(self, line: int, record: bool = True) -> None:
        """Show `line`, and remember having been there unless we are walking the history itself."""
        self.reveal_line(line)
        if record:
            self._record_visit(line)
        self._refresh_history()

    def _record_visit(self, line: int) -> None:
        path = self.paths.get(line)
        label = path.text if path is not None else ".".join(self._parts_for_line(line)) or "."
        self.history.visit(self._parts_for_line(line), f"{label}  (line {line})")

    def go_back(self) -> None:
        self._walk_history(self.history.back())

    def go_forward(self) -> None:
        self._walk_history(self.history.forward())

    def _walk_history(self, parts: tuple[str, ...] | None) -> None:
        if parts is None:
            return
        line = self.line_of_parts.get(parts)
        if line is None:  # the document changed under the entry
            self.set_status("That place is not in this document")
            self._refresh_history()
            return
        self.go_to(line, record=False)

    def _on_history_pick(self, _event: tk.Event) -> None:
        if self._history_lock:
            return
        self._walk_history(self.history.go(self.history_box.current()))

    def _refresh_history(self) -> None:
        """Redraw the bar: the buttons follow the cursor, and the box shows where we are."""
        self._history_lock = True
        self.history_box.configure(values=self.history.labels())
        if self.history.index >= 0:
            self.history_box.current(self.history.index)
        else:
            self.history_choice.set("")
        self._history_lock = False
        self.back_button.configure(state="normal" if self.history.index > 0 else "disabled")
        ahead = 0 <= self.history.index < len(self.history.entries) - 1
        self.forward_button.configure(state="normal" if ahead else "disabled")

    def follow_ref(self, ref: Ref) -> None:
        """Jump to what a $ref points at, or explain why we cannot."""
        if not ref.internal:
            self.set_status(f"External reference, not followed: {ref.pointer}")
            return
        if not ref.resolved:
            self.set_status(f"Unresolved reference: {ref.pointer}")
            return

        # Record where the jump was made from, so Back returns to the $ref rather than to nowhere.
        self._record_visit(ref.line)
        self.go_to(ref.target_line)
        others = len(self.document.used_by.get(ref.target_parts, [])) - 1
        tail = f", {others} other reference{'' if others == 1 else 's'} point here" if others > 0 else ""
        self.set_status(f"Followed {ref.pointer} to line {ref.target_line}{tail}")

    def _select_from(self, tree: ttk.Treeview, lines: dict[str, int]) -> None:
        """Jump to whatever the selected node names: a leaf's own line, or the container an ancestor stands for."""
        selection = tree.selection()
        if not selection:
            return
        line = lines.get(selection[0])
        if line is None:
            return
        self.go_to(line)
        self.set_status(f"{tree.item(selection[0], 'text')}  (line {line})")

    def origins_at(self, line: int, column: int) -> list[ColorSpan]:
        """Every status that contributed a colour to this position, the one actually drawn first.

        A spot can be covered several times over: a block colour from the status beside it, and a reference colour
        from a component or violation defined elsewhere. All of them are reported, worst first, because the colour on
        screen only ever shows the winner and the rest are exactly what a reader cannot otherwise see.
        """
        unique: dict[tuple[str, str, int], ColorSpan] = {}
        for span in self.color_origin.get(line, []):
            if span.start <= column < span.end:
                unique.setdefault((span.value, span.origin_path, span.origin_line), span)
        return sorted(unique.values(), key=lambda span: (-SEVERITY[span.value], span.origin_line))

    def _origin_tooltip(self, spans: list[ColorSpan]) -> str:
        """One line per contributing status, the drawn colour marked."""
        drawn, *overridden = spans
        rows = [f"{drawn.value} \u2190 {drawn.origin_path}  (line {drawn.origin_line})"]
        for span in overridden[: MAX_HOVER_ORIGINS - 1]:
            rows.append(f"also {span.value}: {span.origin_path}  (line {span.origin_line})")
        remaining = len(overridden) - (MAX_HOVER_ORIGINS - 1)
        if remaining > 0:
            rows.append(f"\u2026 and {remaining} more")
        return "\n".join(rows)

    def selection_bounds(self) -> tuple[int, int, int, int] | None:
        """The selection as (first line, first column, last line, last column), or None when nothing is selected."""
        if not self.text.tag_ranges("sel"):
            return None
        first_line, first_column = (int(part) for part in self.text.index("sel.first").split("."))
        last_line, last_column = (int(part) for part in self.text.index("sel.last").split("."))
        return first_line, first_column, last_line, last_column

    def selected_text(self) -> str | None:
        """The selection, rebuilt from current_json.

        Reading the buffer rather than the widget keeps fold marks out of the clipboard and returns the real content
        of any collapsed block the selection spans - what a folded region hides is still part of the document.
        """
        bounds = self.selection_bounds()
        if bounds is None:
            return None

        first_line, first_column, last_line, last_column = bounds
        if first_line == last_line:
            return self.current_json[first_line - 1][first_column:last_column]

        pieces = [self.current_json[first_line - 1][first_column:]]
        pieces += self.current_json[first_line : last_line - 1]
        pieces.append(self.current_json[last_line - 1][:last_column])
        return "\n".join(pieces)

    def block_json(self, line: int) -> str | None:
        """The value of the block around `line`, dedented so it stands alone.

        JSON gives back its brackets, without the "key": part and without the trailing comma, so another tool will
        accept it. YAML gives back the block's own lines, comments and all, dedented to the left margin.
        """
        opened = self.blocks.opening_line(line)
        if opened is None:
            opened = self.blocks.enclosing.get(line, 0)
        closed = self.blocks.close_of.get(opened)
        if not opened or closed is None:
            return None

        if self.document.style == "yaml":  # the body is the block; the key line stays behind
            body = self.current_json[opened:closed]
            if not body:
                return None
            indent = len(body[0]) - len(body[0].lstrip())
            return "\n".join(row[indent:] for row in body).rstrip()

        head = self.current_json[opened - 1]
        span = self.document.value_spans.get(opened)
        closing = self.current_json[closed - 1]
        indent = len(closing) - len(closing.lstrip())

        rows = [head[span[0] :] if span is not None else head[indent:]]
        rows += [self.current_json[number - 1][indent:] for number in range(opened + 1, closed + 1)]
        return "\n".join(rows).rstrip().rstrip(",")

    def _to_clipboard(self, text: str, what: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        lines = text.count("\n") + 1
        self.set_status(f"Copied {what} \u2014 {lines} line{'' if lines == 1 else 's'}, {len(text):,} characters")

    def copy_selection(self) -> str:
        """Copy the selection, or the current line when there is none."""
        text = self.selected_text()
        if text is None:
            line = self.current_line
            if line is None:
                self.set_status("Nothing selected")
                return "break"
            text = self.current_json[line - 1]
            self._to_clipboard(text, f"line {line}")
            return "break"
        self._to_clipboard(text, "selection")
        return "break"

    def copy_block(self) -> str:
        """Copy the block the cursor sits in, ready to paste elsewhere."""
        line = self.current_line
        text = self.block_json(line) if line is not None else None
        if text is None:
            self.set_status("No block here to copy")
            return "break"
        self._to_clipboard(text, "block")
        return "break"

    def copy_path(self) -> str:
        """Copy the jq path of the current line, ready to paste into a jq filter."""
        line = self.current_line
        path = self.paths.get(line) if line is not None else None
        if path is None:
            self.set_status("No path for this line")
            return "break"
        self._to_clipboard(path.text, "path")
        return "break"

    def select_all(self) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _true_position(self, widget: tk.Text, event: tk.Event) -> tuple[int, int] | None:
        """Pointer coordinates as a (line, column) in current_json, or None when they fall outside the buffer.

        Widget line numbers are current_json line numbers: rendering inserts exactly the joined lines, and folding
        only elides or appends inside a line, never adding or removing a newline. The one exception is the newline Tk
        keeps past the last line, which answers with a line one beyond the buffer; it is rejected here so no caller
        can quote a number that does not exist in the file.
        """
        line, column = (int(part) for part in widget.index(f"@{event.x},{event.y}").split("."))
        return (line, column) if 1 <= line <= len(self.current_json) else None

    def _on_text_motion(self, event: tk.Event) -> None:
        """Arm the tooltip when the pointer rests on a coloured item, and drop it as soon as it leaves."""
        position = self._true_position(self.text, event)
        ref = self.document.ref_at(*position) if position is not None else None
        if ref is not None:
            key: object = ("ref", ref.line, ref.span)
            text = self._ref_tooltip(ref)
        else:
            spans = self.origins_at(*position) if position is not None else []
            key, text = spans, self._origin_tooltip(spans) if spans else ""

        if not text:
            self._cancel_hover()
            return
        if key == self._hover_key:
            return  # same item: leave the pending or shown tooltip alone

        self._cancel_hover()
        self._hover_key = key
        pointer = (event.x_root, event.y_root)
        self._hover_after = self.after(HOVER_DELAY_MS, lambda: self._show_origin(text, *pointer))

    def _ref_tooltip(self, ref: Ref) -> str:
        if ref.resolved:
            others = len(self.document.used_by.get(ref.target_parts, [])) - 1
            tail = f"\n{others} other reference{'' if others == 1 else 's'} point here" if others > 0 else ""
            return f"$ref \u2192 {'.'.join(ref.target_parts or ())}  (line {ref.target_line}){tail}"
        if not ref.internal:
            return f"external reference, not followed:\n{ref.pointer}"
        return f"unresolved reference:\n{ref.pointer}"

    def _show_origin(self, text: str, x: int, y: int) -> None:
        self._hover_after = None
        self.tooltip.show(text, x, y)

    def _cancel_hover(self) -> None:
        if self._hover_after is not None:
            self.after_cancel(self._hover_after)
            self._hover_after = None
        self._hover_key = None
        self.tooltip.hide()

    def _on_text_click(self, event: tk.Event) -> None:
        """A click (or a drag-selection) picks the quoted word to hunt for."""
        position = self._true_position(self.text, event)
        if position is None:
            return
        line, column = position

        ref = self.document.ref_at(line, column)
        if ref is not None:
            self.follow_ref(ref)
            return

        self._set_current_line(line)
        self._sync_tree_to_line(line)

        self._highlight_at(line, column)

        needle = self._selected_word(line, column)
        if needle is None:
            return  # a click off any quoted word leaves the box and its results alone
        self._search_for(needle, line)

    def _highlight_at(self, line: int, column: int) -> bool:
        """Shade whatever the click identifies: a key's value, or the block the line belongs to.

        Aiming at a single bracket character is fiddly, so any click on a line that opens or closes a block shades
        that block. YAML has no brackets to aim at in the first place, and this works there unchanged.
        """
        self.text.tag_remove("dictvalue", "1.0", "end")
        body = self.current_json[line - 1]

        key = self.document.key_spans.get(line)
        if key is not None and key[0] <= column < key[1]:
            return self._shade_value(line)

        opened = self.blocks.opening_line(line)
        if opened is not None:
            return self._shade_block(opened)
        return self._shade_empty_pair(body, line, column)

    def _shade_value(self, line: int) -> bool:
        """Shade the value belonging to the key on `line`.

        A container value runs to its closing bracket, so the whole block is shaded; a scalar covers just the value
        itself, leaving the key and the punctuation around it alone.
        """
        if self.blocks.close_of.get(line) is not None:
            return self._shade_block(line)
        span = self.document.value_spans.get(line)
        if span is None:
            return False
        self.text.tag_add("dictvalue", f"{line}.{span[0]}", f"{line}.{span[1]}")
        return True

    def _shade_block(self, opened: int) -> bool:
        """Shade a whole block, from where its value starts down to its last line."""
        closed = self.blocks.close_of.get(opened)
        if closed is None:
            return False

        head = self.current_json[opened - 1]
        span = self.document.value_spans.get(opened)
        start = span[0] if span is not None else max(0, len(head.rstrip()) - 1)
        self.text.tag_add("dictvalue", f"{opened}.{start}", f"{closed}.end")
        return True

    def _shade_empty_pair(self, body: str, line: int, column: int) -> bool:
        """An empty container is a pair of brackets and nothing else; shade just the pair."""
        if column >= len(body) or body[column] not in OPENERS + CLOSERS or inside_string(body, column):
            return False
        start = column if body[column] in OPENERS else column - 1
        if start >= 0 and body[start : start + 2] in ("{}", "[]"):
            self.text.tag_add("dictvalue", f"{line}.{start}", f"{line}.{start + 2}")
            return True
        return False

    def _selected_word(self, line: int, column: int) -> str | None:
        """An explicit selection wins - double-clicking a word selects it - otherwise the quoted word under the click."""
        if self.text.tag_ranges("sel"):
            chosen = self.text.get("sel.first", "sel.last").strip().strip('"')
            if chosen:
                return chosen
        body = self.current_json[line - 1]
        token = quoted_token_at(body, column)
        if token is not None:
            return token

        # YAML writes most keys and many values without quotes; treat those as words too.
        key = self.document.key_spans.get(line)
        if key is not None and key[0] <= column < key[1]:
            return self.document.key_of(line)

        value = self.document.value_spans.get(line)
        if value is not None and value[0] <= column < value[1]:
            return _plain_word(body[value[0] : value[1]])
        return None

    # ------------------------------------------------------------------------------------------------- events --

    def _on_context_menu(self, event: tk.Event) -> str:
        """Point the current line at wherever the menu was opened, so Copy Block acts on what was right-clicked."""
        position = self._true_position(self.text, event)
        if position is not None and self.selection_bounds() is None:
            self._set_current_line(position[0])
        self.context_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _on_gutter_click(self, event: tk.Event) -> str:
        position = self._true_position(self.gutter, event)
        if position is not None:
            self.toggle_fold(position[0])
        return "break"

    def _on_text_double_click(self, event: tk.Event) -> str | None:
        position = self._true_position(self.text, event)
        if position is None or not self.blocks.is_foldable(position[0]):
            return None  # let the default word selection happen
        self.toggle_fold(position[0])
        return "break"

    def _on_gutter_wheel(self, event: tk.Event) -> str:
        if event.num == 4:
            delta = -3
        elif event.num == 5:
            delta = 3
        else:
            delta = -event.delta // 120 * 3
        self.text.yview_scroll(delta, "units")
        self.gutter.yview_moveto(self.text.yview()[0])
        return "break"

    def _on_text_scroll(self, first: str, last: str) -> None:
        self.vbar.set(first, last)
        self.gutter.yview_moveto(float(first))

    def _on_vbar(self, *args: str) -> None:
        self.text.yview(*args)
        self.gutter.yview_moveto(self.text.yview()[0])

    def show_about(self) -> None:
        messagebox.showinfo(
            APP_NAME,
            f"{APP_NAME} \u2014 a viewer for JSON and YAML\n"
            f"JSON is formatted like `jq --indent {JSON_INDENT} -r .`, without needing jq;\n"
            "YAML keeps the file's own text.\n"
            "Pick a path on the left to jump to it; click the gutter or double-click a bracket to fold.\n"
            "Type in the search box to narrow the tree; click a quoted word to find it elsewhere.",
        )


def main() -> None:
    args = parse_args(sys.argv[1:])
    app = App()
    if args.file:
        # after() so the window is mapped first: a warning or a progress box needs something to sit on.
        app.after(0, app.open_argument, Path(args.file).expanduser())
    app.mainloop()


if __name__ == "__main__":
    main()
