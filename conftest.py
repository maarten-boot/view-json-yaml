"""Shared fixtures.

Tests split in two. Most need no display and exercise the document model directly. The ones marked `gui` build a
real Tk application: they cover everything that can only be judged by asking the widgets themselves - elided folds,
tag ranges, style maps, clipboard round trips, geometry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# The tests run whether they sit in tests/ or flat beside jy.py, so find the project rather than assume it.
PROJECT = next((place for place in (HERE, HERE.parent) if (place / "jy.py").is_file()), HERE)
EXAMPLES = PROJECT / "examples"
sys.path.insert(0, str(PROJECT))

import jy


def pytest_configure(config):
    """Register the marker here rather than only in pytest.ini, which applies only when it is found."""
    config.addinivalue_line("markers", "gui: needs a display; builds a real Tk application")


REPORT = {
    "report": {
        "version": "3.1",
        "metadata": {
            "components": {
                "aaaa-1111": {
                    "name": "libfoo",
                    "version": "1.2.3",
                    "status": "fail",
                    "tags": [],
                    "licenses": [{"id": "MIT"}, {"id": "Apache-2.0"}],
                },
                "bbbb-2222": {"name": "libbar", "status": "pass"},
                "cccc-3333": {"name": "libqux", "status": "warning"},
                "dddd-4444": {"name": "libnil"},
            },
            "violations": {
                "v-0001": {"rule_id": "RULE-A", "status": "fail"},
                "v-0002": {"rule_id": "RULE-A2", "status": "pass"},
                "v-0003": {"rule_id": "RULE-C"},
            },
            "component": ["aaaa-1111", "bbbb-2222"],
            "findings": [
                {"title": "CVE-1", "about": "aaaa-1111", "rule": "RULE-A", "status": "pass"},
                {"title": "CVE-2", "references": {"component": ["cccc-3333"]}},
            ],
            "note": "a brace { and RULE-A inside a string",
        },
    }
}

SPEC = """\
# a small OpenAPI spec
openapi: 3.0.3
info:
  title: Pet store        # trailing comment
  version: "1.0"
  description: |-
    Long text.

    Second paragraph.
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          $ref: '#/components/schemas/Pet'
        default:
          $ref: 'errors.yaml#/NotFound'
        '404':
          $ref: '#/components/schemas/Missing'
    post:
      operationId: createPet
      status: fail
components:
  schemas:
    Pet:
      type: object
      status: pass
      category:
        $ref: '#/components/schemas/Category'
    Category:
      type: object
      pet:
        $ref: '#/components/schemas/Pet'
    Empty: {}
defaults: &defaults
  retries: 3
staging:
  <<: *defaults
  extra: []
"""


@pytest.fixture
def report_json(tmp_path: Path) -> Path:
    path = tmp_path / "report.rl.json"
    path.write_text(json.dumps(REPORT), encoding="utf-8")
    return path


@pytest.fixture
def spec_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "openapi.yaml"
    path.write_text(SPEC, encoding="utf-8")
    return path


@pytest.fixture
def report_document() -> jy.Document:
    return jy.build_document(REPORT)


@pytest.fixture
def spec_document() -> jy.Document:
    return jy.build_yaml_document(SPEC)


@pytest.fixture
def big_json(tmp_path: Path) -> Path:
    """Over the 1 MB mark, so the progress window is used."""
    bulk = {
        f"component-{index:05d}": {"name": f"pkg-{index}", "sha256": f"{index:064x}", "status": "pass"}
        for index in range(8000)
    }
    path = tmp_path / "big.json"
    path.write_text(json.dumps({"report": {"metadata": {"components": bulk}}}), encoding="utf-8")
    assert path.stat().st_size > jy.LARGE_FILE_BYTES
    return path


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A real application, with HOME redirected so the developer's own recent-file list is left alone."""
    tkinter = pytest.importorskip("tkinter")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(sys, "argv", ["jy.py"])

    try:
        application = jy.App()
    except tkinter.TclError as exc:  # no display
        pytest.skip(f"Tk cannot open a display: {exc}")

    application.geometry("1200x800+0+0")
    application.update()
    yield application
    application.destroy()


@pytest.fixture
def loaded(app, report_json):
    app.open_path(report_json)
    app.update()
    return app


@pytest.fixture
def loaded_yaml(app, spec_yaml):
    app.open_path(spec_yaml)
    app.update()
    return app


def line_of(document: jy.Document, needle: str) -> int:
    """The first line containing `needle`, so tests read by content rather than by counting."""
    for number, line in enumerate(document.lines, start=1):
        if needle in line:
            return number
    raise AssertionError(f"no line contains {needle!r}")
