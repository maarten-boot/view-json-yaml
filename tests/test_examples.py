"""The shipped examples must stay loadable, and must keep exercising the rules they were written for.

An example that quietly stops demonstrating a feature is worse than no example, so these assert what each file is
supposed to show rather than merely that it parses.
"""

from __future__ import annotations

import pytest
from conftest import EXAMPLES

import jy


@pytest.fixture(scope="module")
def report() -> jy.Document:
    return jy.load_document(EXAMPLES / "report.rl.json")


@pytest.fixture(scope="module")
def spec() -> jy.Document:
    return jy.load_document(EXAMPLES / "openapi.yaml")


def test_the_examples_are_where_the_readme_says():
    assert (EXAMPLES / "report.rl.json").is_file()
    assert (EXAMPLES / "openapi.yaml").is_file()


class TestReportExample:
    def test_it_carries_all_three_statuses(self, report):
        assert set(jy.collect_status_lines(report)) >= {"pass", "warning", "fail"}

    def test_it_has_components_to_cross_reference(self, report):
        components = [
            path.parts for path in report.paths.values() if len(path.parts) == 4 and path.parts[-2] == jy.COMPONENTS_KEY
        ]
        assert len(components) == 4

    def test_one_component_has_no_status(self, report):
        """So the example shows a uuid that is deliberately left uncoloured."""
        statuses = jy.collect_status_lines(report)
        coloured = sum(len(lines) for value, lines in statuses.items() if value in jy.STATUS_COLORS)
        assert coloured < len(report.lines)

    def test_it_has_violations_with_rule_ids(self, report):
        rules = [line for line, node in report.nodes.items() if node.key == jy.RULE_ID_KEY]
        assert len(rules) == 3

    def test_a_brace_in_a_string_is_not_structure(self, report):
        line = next(number for number, text in enumerate(report.lines, 1) if "A brace {" in text)
        assert not report.blocks.is_foldable(line)

    def test_it_has_an_empty_container(self, report):
        line = next(
            number for number, text in enumerate(report.lines, 1) if text.rstrip().rstrip(",").endswith(("{}", "[]"))
        )
        assert not report.blocks.is_foldable(line)


class TestSpecExample:
    def test_it_is_shown_exactly_as_written(self, spec):
        assert spec.lines == (EXAMPLES / "openapi.yaml").read_text().splitlines()

    def test_it_keeps_its_comments(self, spec):
        assert any("# a trailing comment" in line or "# external" in line for line in spec.lines)

    def test_it_has_refs_of_every_kind(self, spec):
        resolved = [ref for ref in spec.refs if ref.resolved]
        external = [ref for ref in spec.refs if not ref.internal]
        dangling = [ref for ref in spec.refs if ref.internal and not ref.resolved]
        assert resolved and len(external) == 1 and len(dangling) == 1

    def test_it_contains_a_cycle(self, spec):
        pet = spec.resolve_pointer("#/components/schemas/Pet")
        category = spec.resolve_pointer("#/components/schemas/Category")
        assert pet is not None and category is not None
        assert spec.used_by[pet.parts] and spec.used_by[category.parts]

    def test_a_schema_is_pointed_at_more_than_once(self, spec):
        pet = spec.resolve_pointer("#/components/schemas/Pet")
        assert len(spec.used_by[pet.parts]) >= 3

    def test_it_has_a_block_scalar_that_folds(self, spec):
        line = next(number for number, text in enumerate(spec.lines, 1) if "description: |-" in text)
        assert spec.blocks.is_foldable(line)
        assert spec.blocks.close_of[line] > line + 2

    def test_it_has_a_merge_key_that_is_skipped(self, spec):
        line = next(number for number, text in enumerate(spec.lines, 1) if "<<: *defaults" in text)
        assert line not in spec.paths

    def test_it_has_a_status_free_document(self, spec):
        """Which is what makes the left pane minimise - worth having an example of."""
        assert jy.collect_status_lines(spec) == {}
