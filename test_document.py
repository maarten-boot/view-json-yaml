"""The document model: rendering, structure, and the two formats. No display needed."""

from __future__ import annotations

import json
import random

import pytest

import view_json_yaml
from conftest import line_of


class TestJsonRendering:
    """view-json-yaml renders JSON itself; the layout has to stay identical to `jq --indent 2 -r .`."""

    @pytest.mark.parametrize(
        "data",
        [
            {"a": 1, "b": "two", "c": None, "d": True, "e": 1.5},
            {"a": {}, "b": [], "c": {"d": {}}, "e": [[]]},
            {"build id": 1, "with.dot": 2, 'quo"te': 3, "üñî": 4, "": 5},
            {"s": 'has "quotes" and {braces} and [brackets]', "t": "tab\tnl\nback\\slash"},
            [{"a": 1}, 2, [3]],
            {"n": 12345678901234567890, "f": 1e300, "z": -0.0},
            42,
        ],
    )
    def test_matches_json_dumps(self, data):
        expected = json.dumps(data, indent=2, ensure_ascii=False).splitlines()
        assert view_json_yaml.build_document(data).lines == expected

    def test_random_documents_match(self):
        rng = random.Random(11)

        def value(depth):
            if depth <= 0 or rng.random() < 0.35:
                return rng.choice([1, "s", None, True, 3.25, "", "br{}ck[]ets", 'q"q'])
            if rng.random() < 0.5:
                return {f"k{rng.randrange(6)}": value(depth - 1) for _ in range(rng.randrange(0, 4))}
            return [value(depth - 1) for _ in range(rng.randrange(0, 4))]

        for _ in range(200):
            data = value(4)
            if isinstance(data, str):
                continue  # a bare string is unwrapped by -r; covered separately
            assert (
                view_json_yaml.build_document(data).lines == json.dumps(data, indent=2, ensure_ascii=False).splitlines()
            )

    def test_top_level_string_is_unwrapped(self):
        assert view_json_yaml.format_json('"just a string"') == ["just a string"]
        assert view_json_yaml.format_json('"two\\nlines"') == ["two", "lines"]

    def test_non_ascii_stays_readable(self):
        assert view_json_yaml.format_json('{"k": "\\u00e9l\\u00e8ve"}') == ["{", '  "k": "élève"', "}"]

    @pytest.mark.parametrize("text", ["{nope", "NaN", "Infinity", "", "{'single': 1}"])
    def test_invalid_json_is_refused(self, text):
        with pytest.raises(view_json_yaml.DocumentError):
            view_json_yaml.parse_json(text)


class TestJsonStructure:
    def test_blocks_pair_up(self, report_document):
        for opened, closed in report_document.blocks.close_of.items():
            assert report_document.blocks.open_of[closed] == opened
            assert closed > opened

    def test_empty_containers_are_not_foldable(self, report_document):
        empty = line_of(report_document, '"tags": []')
        assert not report_document.blocks.is_foldable(empty)

    def test_a_brace_in_a_string_is_not_structure(self, report_document):
        line = line_of(report_document, "a brace {")
        assert not report_document.blocks.is_foldable(line)

    def test_paths_use_jq_spelling(self, report_document):
        line = line_of(report_document, '"aaaa-1111"')
        assert report_document.paths[line].text == '.report.metadata.components["aaaa-1111"]'

    def test_array_index_folds_into_its_key(self, report_document):
        line = line_of(report_document, '"CVE-1"')
        assert report_document.paths[line].parts[-2:] == ("findings[0]", "title")

    def test_scalar_items_exclude_containers(self, report_document):
        opened = line_of(report_document, '"aaaa-1111"')
        members = {report_document.key_of(line) for line in report_document.scalar_items[opened]}
        assert members == {"name", "version", "status"}  # tags and licenses are containers

    def test_line_of_parts_points_back(self, report_document):
        for line, path in report_document.paths.items():
            assert report_document.line_of_parts[path.parts] <= line


class TestYamlStructure:
    def test_the_file_is_shown_verbatim(self, spec_document, spec_yaml):
        assert spec_document.lines == spec_yaml.read_text().splitlines()

    def test_comments_survive(self, spec_document):
        assert any("# trailing comment" in line for line in spec_document.lines)

    def test_keys_become_paths(self, spec_document):
        line = line_of(spec_document, "operationId: listPets")
        assert spec_document.paths[line].text == '.paths["/pets"].get.operationId'

    def test_block_scalar_covers_its_text(self, spec_document):
        start = line_of(spec_document, "description: |-")
        closed = spec_document.blocks.close_of[start]
        assert spec_document.lines[closed - 1].strip() == "Second paragraph."
        assert spec_document.blocks.is_foldable(start)

    def test_a_blank_line_inside_block_text_does_not_end_it(self, spec_document):
        start = line_of(spec_document, "description: |-")
        covered = range(start + 1, spec_document.blocks.close_of[start] + 1)
        assert any(not spec_document.lines[line - 1].strip() for line in covered)

    def test_parent_reaches_past_the_block_text(self, spec_document):
        info = line_of(spec_document, "info:")
        description = line_of(spec_document, "description: |-")
        assert spec_document.blocks.close_of[info] == spec_document.blocks.close_of[description]

    def test_merge_keys_are_skipped(self, spec_document):
        merge = line_of(spec_document, "<<: *defaults")
        assert merge not in spec_document.paths

    def test_an_alias_is_a_leaf(self, spec_document):
        staging = line_of(spec_document, "staging:")
        keys = {spec_document.key_of(child.line) for child in spec_document.nodes[staging].children}
        assert "retries" not in keys  # it lives at the anchor, not here

    def test_the_root_owns_no_line(self, spec_document):
        first = line_of(spec_document, "openapi:")
        assert spec_document.paths[first].text == ".openapi"
        assert not spec_document.blocks.is_foldable(first)

    def test_value_span_stops_before_a_comment(self, spec_document):
        line = line_of(spec_document, "title: Pet store")
        start, end = spec_document.value_spans[line]
        assert spec_document.lines[line - 1][start:end] == "Pet store"

    def test_multiple_documents_warn(self):
        document = view_json_yaml.build_yaml_document("a: 1\n---\nb: 2\n")
        assert document.warning and "only the first" in document.warning

    def test_invalid_yaml_is_refused(self):
        with pytest.raises(view_json_yaml.DocumentError):
            view_json_yaml.build_yaml_document("a: [1,\nb\n")


class TestFolding:
    def test_json_folds_stop_at_the_bracket(self, report_document):
        opened = line_of(report_document, '"licenses"')
        closed = report_document.blocks.close_of[opened]
        assert report_document.fold_end(opened) == f"{closed}.{report_document.closing_column[closed]}"

    def test_yaml_folds_run_to_the_end_of_the_line(self, spec_document):
        opened = line_of(spec_document, "info:")
        assert spec_document.fold_end(opened).endswith(".end")

    def test_innermost_block_wins_a_shared_last_line(self, spec_document):
        description = line_of(spec_document, "description: |-")
        last = spec_document.blocks.close_of[description]
        assert spec_document.blocks.open_of[last] == description  # not the enclosing "info:"


class TestStatus:
    def test_status_is_found_in_both_formats(self, report_document, spec_document):
        assert set(view_json_yaml.collect_status_lines(report_document)) >= {"pass", "fail", "warning"}
        assert set(view_json_yaml.collect_status_lines(spec_document)) == {"fail", "pass"}

    def test_only_the_three_values_get_paths(self, report_document):
        lines = view_json_yaml.collect_status_lines(report_document)
        lines.setdefault("skip", []).append(1)
        entries = view_json_yaml.collect_status_paths(report_document.paths, lines)
        assert {entry.value for entry in entries} <= set(view_json_yaml.STATUS_COLORS)

    def test_every_status_line_is_remembered(self, report_document):
        found = view_json_yaml.collect_status_lines(report_document)
        for value, numbers in found.items():
            for number in numbers:
                assert value in report_document.lines[number - 1] or report_document.key_of(number) == "status"


class TestHelpers:
    @pytest.mark.parametrize(
        ("name", "supported"),
        [("a.json", True), ("b.yaml", True), ("c.yml", True), ("d.YAML", True), ("e.txt", False), ("f", False)],
    )
    def test_extensions(self, tmp_path, name, supported):
        assert view_json_yaml.is_supported_file(tmp_path / name) is supported

    def test_quoted_spans_handle_escapes(self):
        line = '      "note": "he said \\"hi\\" twice",'
        assert len(view_json_yaml.quoted_spans(line)) == 2

    @pytest.mark.parametrize("column", [21, 37])
    def test_a_bracket_inside_a_string_is_text(self, column):
        line = '  "note": "a brace { and a bracket ] inside"'
        assert view_json_yaml.inside_string(line, column)

    def test_missing_file_has_no_size(self, tmp_path):
        assert view_json_yaml.file_size(tmp_path / "nope.json") == 0

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [(["--file=/tmp/a.json"], "/tmp/a.json"), (["--file", "/tmp/b.yaml"], "/tmp/b.yaml"), ([], None)],
    )
    def test_argument_parsing(self, argv, expected):
        assert view_json_yaml.parse_args(argv).file == expected
