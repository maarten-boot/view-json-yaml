"""References, history and the recent-file store. No display needed."""

from __future__ import annotations

from pathlib import Path

import pytest

import view_json_yaml
from conftest import line_of


class TestPointerResolution:
    def test_a_plain_pointer_resolves(self, spec_document):
        node = spec_document.resolve_pointer("#/components/schemas/Pet")
        assert node is not None
        assert spec_document.lines[node.line - 1].strip() == "Pet:"

    def test_escapes_are_decoded(self, spec_document):
        node = spec_document.resolve_pointer("#/paths/~1pets/get")
        assert node is not None and node.key == "get"

    def test_array_indices_are_followed(self):
        document = view_json_yaml.build_document({"servers": [{"url": "a"}, {"url": "b"}]})
        node = document.resolve_pointer("#/servers/1/url")
        assert node is not None and node.value == "b"

    def test_the_empty_pointer_is_the_root(self, spec_document):
        assert spec_document.resolve_pointer("#") is spec_document.root

    @pytest.mark.parametrize("pointer", ["#/nope", "#/components/schemas/Nope", "#/components/0", "elsewhere.yaml#/X"])
    def test_pointers_that_go_nowhere(self, spec_document, pointer):
        assert spec_document.resolve_pointer(pointer) is None


class TestRefIndex:
    def test_internal_refs_resolve(self, spec_document):
        internal = [ref for ref in spec_document.refs if ref.internal]
        assert internal, "the sample spec has $refs"
        assert any(ref.resolved for ref in internal)

    def test_an_external_ref_is_not_followed(self, spec_document):
        external = [ref for ref in spec_document.refs if not ref.internal]
        assert len(external) == 1
        assert not external[0].resolved

    def test_an_internal_ref_to_nothing_is_unresolved(self, spec_document):
        missing = [ref for ref in spec_document.refs if ref.pointer.endswith("Missing")]
        assert missing and missing[0].internal and not missing[0].resolved

    def test_used_by_counts_every_pointer(self, spec_document):
        pet = spec_document.resolve_pointer("#/components/schemas/Pet")
        assert len(spec_document.used_by[pet.parts]) == 2  # the 200 response and Category.pet

    def test_a_ref_span_covers_the_pointer(self, spec_document):
        ref = spec_document.refs[0]
        text = spec_document.lines[ref.line - 1][ref.span[0] : ref.span[1]]
        assert ref.pointer in text

    def test_ref_at_finds_by_column(self, spec_document):
        ref = spec_document.refs[0]
        assert spec_document.ref_at(ref.line, ref.span[0]) is ref
        assert spec_document.ref_at(ref.line, ref.span[0] - 1) is None

    def test_json_refs_work_too(self):
        document = view_json_yaml.build_document({"a": {"$ref": "#/b"}, "b": {"x": 1}})
        assert document.refs[0].resolved


class TestHistory:
    def test_visits_accumulate(self):
        history = view_json_yaml.History()
        for parts in (("a",), ("b",), ("c",)):
            history.visit(parts, str(parts))
        assert history.index == 2
        assert len(history.entries) == 3

    def test_revisiting_the_same_place_adds_nothing(self):
        history = view_json_yaml.History()
        history.visit(("a",), "a")
        history.visit(("a",), "a")
        assert len(history.entries) == 1

    def test_back_and_forward_walk(self):
        history = view_json_yaml.History()
        for parts in (("a",), ("b",), ("c",)):
            history.visit(parts, str(parts))
        assert history.back() == ("b",)
        assert history.back() == ("a",)
        assert history.back() is None  # already at the start
        assert history.forward() == ("b",)

    def test_a_new_visit_truncates_what_was_ahead(self):
        history = view_json_yaml.History()
        for parts in (("a",), ("b",), ("c",)):
            history.visit(parts, str(parts))
        history.back()
        history.visit(("d",), "d")
        assert [parts for parts, _label in history.entries] == [("a",), ("b",), ("d",)]
        assert history.forward() is None

    def test_the_limit_drops_the_oldest(self):
        history = view_json_yaml.History(limit=3)
        for index in range(6):
            history.visit((str(index),), str(index))
        assert [parts[0] for parts, _label in history.entries] == ["3", "4", "5"]

    def test_go_by_index(self):
        history = view_json_yaml.History()
        for parts in (("a",), ("b",)):
            history.visit(parts, str(parts))
        assert history.go(0) == ("a",)
        assert history.go(9) is None

    def test_clearing(self):
        history = view_json_yaml.History()
        history.visit(("a",), "a")
        history.clear()
        assert history.entries == [] and history.index == -1


class TestRecentFiles:
    def test_a_missing_store_is_empty(self, tmp_path):
        recent = view_json_yaml.RecentFiles(tmp_path / "nowhere")
        assert recent.paths == [] and recent.error is None

    def test_newest_first_and_capped(self, tmp_path):
        recent = view_json_yaml.RecentFiles(tmp_path / "store", limit=3)
        for index in range(5):
            recent.add(tmp_path / f"f{index}.json")
        assert [path.name for path in recent.paths] == ["f4.json", "f3.json", "f2.json"]

    def test_re_adding_moves_to_the_front(self, tmp_path):
        recent = view_json_yaml.RecentFiles(tmp_path / "store")
        first, second = tmp_path / "a.json", tmp_path / "b.json"
        recent.add(first)
        recent.add(second)
        recent.add(first)
        assert recent.paths == [first, second]

    def test_it_survives_a_restart(self, tmp_path):
        store = tmp_path / "store"
        view_json_yaml.RecentFiles(store).add(tmp_path / "a.json")
        assert view_json_yaml.RecentFiles(store).paths == [tmp_path / "a.json"]

    def test_the_same_file_spelled_differently_counts_once(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        target = tmp_path / "docs" / "a.json"
        target.write_text("{}")
        recent = view_json_yaml.RecentFiles(tmp_path / "store")
        recent.add(target)
        monkeypatch.chdir(tmp_path / "docs")
        recent.add(Path("a.json"))
        assert len(recent.paths) == 1

    def test_an_unwritable_store_reports_but_does_not_raise(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file, not a directory")
        recent = view_json_yaml.RecentFiles(blocker)
        recent.add(tmp_path / "a.json")
        assert recent.error is not None
        assert recent.paths  # still usable in this session

    def test_an_unreadable_store_reports(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        (store / view_json_yaml.RECENT_FILE).write_bytes(b"\xff\xfe not utf-8")
        assert view_json_yaml.RecentFiles(store).error is not None

    def test_removal(self, tmp_path):
        recent = view_json_yaml.RecentFiles(tmp_path / "store")
        recent.add(tmp_path / "a.json")
        recent.remove(tmp_path / "a.json")
        assert recent.paths == []

    @pytest.mark.parametrize(
        "argv0", ["/usr/local/bin/view-json-yaml", "./view_json_yaml.py", "/opt/tools/view_json_yaml.py"]
    )
    def test_both_ways_of_starting_share_one_directory(self, monkeypatch, tmp_path, argv0):
        """The console script and `python3 view_json_yaml.py` must not keep two separate recent lists."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr("sys.argv", [argv0])
        assert view_json_yaml.app_directory() == tmp_path / ".view-json-yaml"

    def test_without_a_script_name_it_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr("sys.argv", [""])
        assert view_json_yaml.app_directory() == tmp_path / f".{view_json_yaml.FALLBACK_SLUG}"

    def test_home_is_shortened_for_display(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert view_json_yaml.shorten_home(tmp_path / "docs" / "a.json") == "~/docs/a.json"
        assert view_json_yaml.shorten_home(Path("/etc/hosts")) == "/etc/hosts"


class TestTreeStyleFilter:
    """The filter that keeps a selected tree row showing its own colour."""

    @pytest.mark.parametrize(
        "entries",
        [
            [("selected", "#4a6984"), ("!disabled", "!selected", "#ffffff")],
            [("disabled", "#d9d9d9"), ("selected", "#4a6984")],
            [(("selected", "focus"), "#0078d7"), ("disabled", "#a3a3a3")],
            [],
        ],
    )
    def test_no_selected_entry_survives(self, entries):
        kept = view_json_yaml.without_selected(entries)
        states = [str(part) for entry in kept for part in entry[:-1]]
        assert not any("selected" in state for state in states)

    def test_other_states_are_left_alone(self):
        kept = view_json_yaml.without_selected([("disabled", "#aaa"), ("selected", "#bbb")])
        assert kept == [("disabled", "#aaa")]


def test_line_numbers_are_real(report_document):
    """Whatever a path claims, that line must exist and hold what the path names."""
    for line in report_document.paths:
        assert 1 <= line <= len(report_document.lines)
        key = report_document.key_of(line)
        if key is not None:
            assert key in report_document.lines[line - 1]


def test_status_paths_point_at_status_lines(report_document):
    lines = view_json_yaml.collect_status_lines(report_document)
    for entry in view_json_yaml.collect_status_paths(report_document.paths, lines):
        assert entry.value in report_document.lines[entry.line - 1]
        assert entry.parts[-1] == "status"


def test_auto_collapse_targets_lists_only(report_document):
    component = line_of(report_document, '"component"')
    assert report_document.key_of(component) in view_json_yaml.AUTO_COLLAPSE_KEYS
    assert report_document.lines[component - 1].rstrip().endswith("[")
