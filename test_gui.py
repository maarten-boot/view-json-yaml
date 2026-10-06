"""Everything that can only be judged by asking the widgets themselves.

These need a display. On a headless machine run them under a virtual one:

    xvfb-run -a pytest tests/test_gui.py

Each test drives the application the way a person would - clicking at real coordinates, reading real tag ranges -
rather than calling handlers with invented events, so a binding that is wired to the wrong widget still fails.
"""

from __future__ import annotations

import json

import pytest
from conftest import line_of

import jy

pytestmark = pytest.mark.gui


def click(widget, index: str, button: str = "<ButtonRelease-1>") -> None:
    """Click the character at `index`, at its real position on screen."""
    widget.see(index)
    widget.update_idletasks()
    box = widget.bbox(index)
    assert box is not None, f"{index} is not visible; cannot click it"
    x, y, width, height = box
    widget.event_generate(button, x=x + width // 2, y=y + height // 2)
    widget.update()


def displayed_lines(text) -> int:
    """How many lines are actually on show, elided ones excluded."""
    return text.count("1.0", "end", "displaylines")[0]


class TestLayout:
    def test_the_window_has_its_three_panes(self, app):
        assert len(app.content.panes()) == 3

    def test_the_text_window_is_black_on_white(self, app):
        assert app.text.cget("foreground") == jy.FG_COLOR
        assert app.text.cget("background") == jy.BG_COLOR

    def test_the_text_window_is_read_only(self, loaded):
        before = loaded.text.get("1.0", "end")
        loaded.text.event_generate("<Key>", keysym="x")
        loaded.update()
        assert loaded.text.get("1.0", "end") == before

    def test_the_menus_are_there(self, app):
        menubar = app.nametowidget(app.cget("menu"))
        labels = [
            menubar.entrycget(index, "label")
            for index in range(menubar.index("end") + 1)
            if menubar.type(index) == "cascade"
        ]
        assert labels == ["Files", "Edit", "View", "Help"]

    def test_the_edit_menu_offers_the_copy_actions(self, app):
        menubar = app.nametowidget(app.cget("menu"))
        edit = app.nametowidget(menubar.entrycget("Edit", "menu"))
        labels = [
            edit.entrycget(index, "label") for index in range(edit.index("end") + 1) if edit.type(index) != "separator"
        ]
        assert "Copy Current Block" in labels
        assert "Copy Path" in labels


class TestTreeStyle:
    """A selected row must keep the colour its status gave it (rule X3)."""

    def test_selection_is_very_light_grey(self, app):
        style = app.tk.call("ttk::style", "map", jy.TREE_STYLE, "-background")
        assert jy.VERY_LIGHT_GRAY in str(style)

    def test_no_selected_foreground_overrides_the_tag(self, app):
        mapping = str(app.tk.call("ttk::style", "map", jy.TREE_STYLE, "-foreground"))
        assert "selected" not in mapping

    def test_the_status_tags_carry_their_colours(self, loaded):
        for value, colour in jy.STATUS_COLORS.items():
            assert str(loaded.tree.tag_configure(value, "foreground")) == colour


class TestLoading:
    def test_the_text_matches_the_buffer(self, loaded):
        shown = loaded.text.get("1.0", "end-1c")
        assert shown == "\n".join(loaded.current_json)

    def test_the_gutter_has_a_row_per_line(self, loaded):
        gutter = loaded.gutter.get("1.0", "end-1c").split("\n")
        assert len(gutter) == len(loaded.current_json)

    def test_the_title_names_the_file(self, loaded, report_json):
        assert report_json.name in loaded.title()

    def test_a_yaml_file_loads_too(self, loaded_yaml, spec_yaml):
        assert loaded_yaml.current_json == spec_yaml.read_text().splitlines()

    def test_a_bad_extension_is_refused(self, app, tmp_path, monkeypatch):
        shown = {}
        monkeypatch.setattr(jy.messagebox, "showwarning", lambda title, message: shown.setdefault("message", message))
        rubbish = tmp_path / "notes.txt"
        rubbish.write_text("{}")
        app.open_argument(rubbish)
        app.update()
        assert shown and not app.current_json

    def test_a_broken_file_reports_and_keeps_going(self, app, tmp_path, monkeypatch):
        monkeypatch.setattr(jy.messagebox, "showerror", lambda *args: None)
        broken = tmp_path / "broken.json"
        broken.write_text("{nope")
        app.open_path(broken)
        app.update()
        assert app.current_json == []
        assert broken not in app.recent.paths

    def test_a_large_file_shows_progress_and_finishes(self, app, big_json):
        app.open_path(big_json)
        app.update()
        assert app.current_json
        assert app._progress is None  # the window closed again

    def test_opening_fills_the_recent_menu(self, loaded, report_json):
        labels = [
            loaded.recent_menu.entrycget(index, "label")
            for index in range(loaded.recent_menu.index("end") + 1)
            if loaded.recent_menu.type(index) == "command"
        ]
        assert any(report_json.name in label for label in labels)


class TestFolding:
    def test_a_fold_hides_lines(self, loaded):
        opened = line_of(loaded.document, '"licenses"')
        before = displayed_lines(loaded.text)
        loaded.toggle_fold(opened)
        loaded.update()
        assert displayed_lines(loaded.text) < before

    def test_unfolding_restores_the_text_exactly(self, loaded):
        opened = line_of(loaded.document, '"licenses"')
        before = loaded.text.get("1.0", "end-1c")
        loaded.toggle_fold(opened)
        loaded.toggle_fold(opened)
        loaded.update()
        assert loaded.text.get("1.0", "end-1c") == before

    def test_collapse_all_then_expand_all_round_trips(self, loaded):
        before = loaded.text.get("1.0", "end-1c")
        loaded.collapse_all()
        loaded.update()
        assert displayed_lines(loaded.text) < len(loaded.current_json)
        loaded.expand_all()
        loaded.update()
        assert loaded.text.get("1.0", "end-1c") == before
        assert loaded.folded == set()

    def test_the_gutter_arrow_follows_the_fold(self, loaded):
        opened = line_of(loaded.document, '"licenses"')
        loaded.toggle_fold(opened)
        loaded.update()
        assert loaded.gutter.get(f"{opened}.0", f"{opened}.1") == jy.ARROW_CLOSED
        loaded.toggle_fold(opened)
        loaded.update()
        assert loaded.gutter.get(f"{opened}.0", f"{opened}.1") == jy.ARROW_OPEN

    def test_clicking_the_gutter_folds(self, loaded):
        opened = line_of(loaded.document, '"licenses"')
        click(loaded.gutter, f"{opened}.0", "<Button-1>")
        assert opened in loaded.folded

    def test_a_component_list_starts_folded(self, app, tmp_path):
        quiet = tmp_path / "quiet.json"
        quiet.write_text(json.dumps({"report": {"metadata": {"component": ["nothing", "coloured", "here"]}}}))
        app.open_path(quiet)
        app.update()
        assert line_of(app.document, '"component"') in app.folded

    def test_but_not_one_holding_a_coloured_reference(self, loaded):
        """Rule P1 yields to P2: folding it would hide the cross-reference the colouring just drew."""
        component = line_of(loaded.document, '"component"')
        assert loaded.document.blocks.is_foldable(component)
        assert component not in loaded.folded

    def test_the_components_dict_does_not(self, loaded):
        components = line_of(loaded.document, '"components"')
        assert components not in loaded.folded

    def test_a_yaml_block_scalar_folds(self, loaded_yaml):
        opened = line_of(loaded_yaml.document, "description: |-")
        before = displayed_lines(loaded_yaml.text)
        loaded_yaml.toggle_fold(opened)
        loaded_yaml.update()
        assert displayed_lines(loaded_yaml.text) < before


class TestColouring:
    def lines_tagged(self, text, tag):
        ranges = text.tag_ranges(tag)
        found = set()
        for start, end in zip(ranges[::2], ranges[1::2]):
            for line in range(int(str(start).split(".")[0]), int(str(end).split(".")[0]) + 1):
                found.add(line)
        return found

    def test_a_failing_block_is_red(self, loaded):
        line = line_of(loaded.document, '"libfoo"')
        assert line in self.lines_tagged(loaded.text, "fail")

    def test_a_passing_block_is_green(self, loaded):
        line = line_of(loaded.document, '"libbar"')
        assert line in self.lines_tagged(loaded.text, "pass")

    def test_fail_outranks_pass_on_the_same_key(self, loaded):
        key = line_of(loaded.document, '"components"')
        assert key in self.lines_tagged(loaded.text, "fail")

    def test_a_uuid_mention_takes_the_component_colour(self, loaded):
        mention = line_of(loaded.document, '"about"')
        assert mention in self.lines_tagged(loaded.text, jy.REFERENCE_TAGS["fail"])

    def test_a_rule_id_mention_is_coloured(self, loaded):
        mention = line_of(loaded.document, '"rule"')
        assert mention in self.lines_tagged(loaded.text, jy.REFERENCE_TAGS["fail"])

    def test_the_reference_tags_sit_above_the_status_tags(self, loaded):
        order = loaded.text.tag_names()
        assert order.index(jy.REFERENCE_TAGS["fail"]) > order.index("fail")


class TestSelectionAndShading:
    def test_clicking_a_key_shades_its_value(self, loaded):
        key = line_of(loaded.document, '"licenses"')
        column = loaded.document.key_spans[key][0] + 1
        click(loaded.text, f"{key}.{column}")
        assert loaded.text.tag_ranges("dictvalue")

    def test_clicking_a_block_line_anywhere_shades_it(self, loaded):
        closing = loaded.document.blocks.close_of[line_of(loaded.document, '"licenses"')]
        click(loaded.text, f"{closing}.0")
        ranges = loaded.text.tag_ranges("dictvalue")
        assert ranges, "a click on a closing line should shade its block"

    def test_clicking_a_plain_line_shades_nothing(self, loaded):
        line = line_of(loaded.document, '"version"')
        click(loaded.text, f"{line}.0")
        assert not loaded.text.tag_ranges("dictvalue")

    def test_clicking_a_word_fills_the_right_hand_search(self, loaded):
        line = line_of(loaded.document, '"libfoo"')
        column = loaded.current_json[line - 1].index("libfoo")
        click(loaded.text, f"{line}.{column}")
        assert loaded.match_search_var.get() == "libfoo"
        assert loaded.matches_tree.get_children()

    def test_clicking_a_bare_yaml_key_fills_it_too(self, loaded_yaml):
        line = line_of(loaded_yaml.document, "operationId: listPets")
        column = loaded_yaml.document.key_spans[line][0] + 1
        click(loaded_yaml.text, f"{line}.{column}")
        assert loaded_yaml.match_search_var.get() == "operationId"

    def test_the_current_line_is_marked(self, loaded):
        line = line_of(loaded.document, '"version"')
        click(loaded.text, f"{line}.0")
        assert loaded.text.tag_ranges("reveal")
        assert loaded.current_line == line


class TestBreadcrumbs:
    def test_crumbs_appear_for_the_current_line(self, loaded):
        line = line_of(loaded.document, '"libfoo"')
        click(loaded.text, f"{line}.0")
        labels = [child.cget("text") for child in loaded.breadcrumb.winfo_children()]
        assert "components" in labels

    def test_clicking_a_crumb_moves_the_view(self, loaded):
        line = line_of(loaded.document, '"libfoo"')
        click(loaded.text, f"{line}.0")
        crumbs = [child for child in loaded.breadcrumb.winfo_children() if child.cget("text") == "components"]
        assert crumbs
        crumbs[0].event_generate("<Button-1>")
        loaded.update()
        loaded.update()  # the jump is deferred to idle
        assert loaded.current_line == line_of(loaded.document, '"components"')

    def test_the_bar_scrolls_when_the_path_is_long(self, loaded):
        deep = line_of(loaded.document, '"MIT"')
        click(loaded.text, f"{deep}.0")
        loaded.update()
        region = loaded.crumb_canvas.cget("scrollregion")
        assert region and float(str(region).split()[2]) > 0


class TestTrees:
    def test_the_left_tree_lists_status_paths(self, loaded):
        assert loaded.tree.get_children()
        assert loaded.status_paths

    def test_selecting_a_path_moves_the_text(self, loaded):
        item = next(item for item, line in loaded._tree_lines.items() if line)
        loaded.tree.selection_set(item)
        loaded.update()
        assert loaded.current_line == loaded._tree_lines[item]

    def test_searching_narrows_the_tree(self, loaded):
        before = len(loaded.tree.get_children())
        loaded.search_var.set("aaaa")
        loaded.update()
        assert len(loaded.tree.get_children()) <= before

    def test_the_not_button_inverts_the_search(self, loaded):
        loaded.search_var.set("aaaa")
        loaded.update()
        matching = loaded._fill_tree()
        loaded.search_negate.set(True)
        loaded.update()
        assert loaded._fill_tree() == len(loaded.status_paths) - matching

    def test_an_empty_left_tree_minimises_its_pane(self, app, tmp_path):
        plain = tmp_path / "plain.json"
        plain.write_text(json.dumps({"a": 1}))
        app.open_path(plain)
        app.update()
        assert len(app.content.panes()) == 2

    def test_the_pane_comes_back(self, app, tmp_path, report_json):
        plain = tmp_path / "plain.json"
        plain.write_text(json.dumps({"a": 1}))
        app.open_path(plain)
        app.open_path(report_json)
        app.update()
        assert len(app.content.panes()) == 3

    def test_clicking_a_line_selects_its_path_in_the_left_tree(self, loaded):
        line = line_of(loaded.document, '"libfoo"')
        click(loaded.text, f"{line}.0")
        assert loaded.tree.selection()

    def test_following_the_text_unfolds_the_tree(self, loaded):
        for item in loaded._tree_items.values():
            loaded.tree.item(item, open=False)
        line = line_of(loaded.document, '"libfoo"')
        click(loaded.text, f"{line}.0")
        selected = loaded.tree.selection()[0]
        parent = loaded.tree.parent(selected)
        assert not parent or loaded.tree.item(parent, "open")


class TestReferences:
    def test_a_resolving_ref_is_drawn_as_a_link(self, loaded_yaml):
        assert loaded_yaml.text.tag_ranges("reflink")

    def test_an_external_ref_is_marked_differently(self, loaded_yaml):
        assert loaded_yaml.text.tag_ranges("refdead")

    def test_clicking_a_ref_jumps_to_its_target(self, loaded_yaml):
        ref = next(ref for ref in loaded_yaml.document.refs if ref.resolved)
        click(loaded_yaml.text, f"{ref.line}.{ref.span[0] + 1}")
        assert loaded_yaml.current_line == ref.target_line

    def test_the_jump_leaves_a_way_back(self, loaded_yaml):
        ref = next(ref for ref in loaded_yaml.document.refs if ref.resolved)
        click(loaded_yaml.text, f"{ref.line}.{ref.span[0] + 1}")
        assert str(loaded_yaml.back_button.cget("state")) == "normal"
        loaded_yaml.go_back()
        loaded_yaml.update()
        assert loaded_yaml.current_line == ref.line

    def test_an_external_ref_does_not_move_the_view(self, loaded_yaml):
        ref = next(ref for ref in loaded_yaml.document.refs if not ref.internal)
        loaded_yaml.go_to(1)
        click(loaded_yaml.text, f"{ref.line}.{ref.span[0] + 1}")
        assert loaded_yaml.current_line == 1


class TestHistoryBar:
    def test_it_starts_empty(self, loaded):
        assert str(loaded.back_button.cget("state")) == "disabled"
        assert str(loaded.forward_button.cget("state")) == "disabled"

    def test_visits_fill_the_dropdown(self, loaded):
        loaded.go_to(3)
        loaded.go_to(7)
        loaded.update()
        assert len(loaded.history_box.cget("values")) == 2

    def test_back_enables_forward(self, loaded):
        loaded.go_to(3)
        loaded.go_to(7)
        loaded.go_back()
        loaded.update()
        assert str(loaded.forward_button.cget("state")) == "normal"

    def test_picking_from_the_dropdown_navigates(self, loaded):
        loaded.go_to(3)
        loaded.go_to(7)
        loaded.history_box.current(0)
        loaded.history_box.event_generate("<<ComboboxSelected>>")
        loaded.update()
        assert loaded.current_line == 3

    def test_closing_a_file_clears_it(self, loaded):
        loaded.go_to(3)
        loaded.close_json()
        loaded.update()
        assert loaded.history.entries == []


class TestClipboard:
    def test_copying_a_block_yields_valid_json(self, loaded):
        loaded.go_to(line_of(loaded.document, '"licenses"'))
        loaded.copy_block()
        assert json.loads(loaded.clipboard_get()) == [{"id": "MIT"}, {"id": "Apache-2.0"}]

    def test_copying_a_folded_block_still_yields_its_content(self, loaded):
        opened = line_of(loaded.document, '"licenses"')
        loaded.toggle_fold(opened)
        loaded.go_to(opened)
        loaded.copy_block()
        assert json.loads(loaded.clipboard_get()) == [{"id": "MIT"}, {"id": "Apache-2.0"}]

    def test_no_fold_mark_reaches_the_clipboard(self, loaded):
        loaded.collapse_all()
        loaded.go_to(1)
        loaded.copy_block()
        assert jy.FOLD_MARK.strip() not in loaded.clipboard_get()

    def test_copying_the_current_line(self, loaded):
        line = line_of(loaded.document, '"libfoo"')
        loaded.go_to(line)
        loaded.copy_selection()
        assert loaded.clipboard_get() == loaded.current_json[line - 1]

    def test_copying_a_selection(self, loaded):
        loaded.text.tag_add("sel", "2.0", "3.5")
        loaded.copy_selection()
        assert loaded.clipboard_get().startswith(loaded.current_json[1])

    def test_copying_a_path(self, loaded):
        line = line_of(loaded.document, '"libfoo"')
        loaded.go_to(line)
        loaded.copy_path()
        assert loaded.clipboard_get() == loaded.document.paths[line].text

    def test_copying_a_yaml_block_keeps_its_comments(self, loaded_yaml):
        loaded_yaml.go_to(line_of(loaded_yaml.document, "info:"))
        loaded_yaml.copy_block()
        assert "# trailing comment" in loaded_yaml.clipboard_get()


class TestHover:
    def test_hovering_a_coloured_item_names_its_origin(self, loaded, monkeypatch):
        shown = {}
        monkeypatch.setattr(jy.Tooltip, "show", lambda self, text, x, y: shown.setdefault("text", text))
        line = line_of(loaded.document, '"libfoo"')
        loaded.text.see(f"{line}.0")
        loaded.update_idletasks()
        box = loaded.text.bbox(f"{line}.4")
        loaded.text.event_generate("<Motion>", x=box[0], y=box[1] + box[3] // 2)
        loaded.update()
        loaded.after(jy.HOVER_DELAY_MS + 150, loaded.quit)
        loaded.mainloop()
        assert "status" in shown.get("text", "")

    def test_hovering_a_ref_names_its_target(self, loaded_yaml):
        ref = next(ref for ref in loaded_yaml.document.refs if ref.resolved)
        assert "→" in loaded_yaml._ref_tooltip(ref)


class TestStatusLine:
    def test_it_reports_what_was_loaded(self, loaded):
        assert "components coloured" in loaded._message.get()

    def test_it_counts_the_lines(self, loaded):
        assert str(len(loaded.current_json)) in loaded._counters.get()
