"""Unit tests for the pure helpers: tokens, tables, locator markers, frontmatter."""

from __future__ import annotations

from kb.indexer.frontmatter import derive_title, parse_frontmatter
from kb.indexer.locator import iter_marked_lines, marker, parse_marker, shift_rows, strip_markers
from kb.indexer.tables import is_table_line, parse_markdown_table, render_markdown_table, split_row
from kb.indexer.tokens import estimate_tokens

# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_counts_cjk_per_character() -> None:
    assert estimate_tokens("防止任务重复执行") == 8


def test_estimate_tokens_counts_identifiers_as_single_units() -> None:
    # `SKIP LOCKED` is queried as an identifier pair, so two tokens is the
    # useful reading, not nine characters.
    assert estimate_tokens("SKIP LOCKED") == 2


def test_estimate_tokens_ignores_empty_input() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("   \n ") == 0


def test_estimate_tokens_is_monotonic() -> None:
    short = "这是一段短文本"
    assert estimate_tokens(short) < estimate_tokens(short * 3)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def test_parse_markdown_table_reads_header_and_rows() -> None:
    table = parse_markdown_table(["| a | b |", "| --- | --- |", "| 1 | 2 |", "| 3 | 4 |"])
    assert table is not None
    assert table.header == ["a", "b"]
    assert table.rows == [["1", "2"], ["3", "4"]]


def test_parse_markdown_table_requires_a_separator_row() -> None:
    """A ``|``-delimited line in a note is not a table without its separator."""
    assert parse_markdown_table(["| not | a table |", "| still | prose |"]) is None


def test_parse_markdown_table_rejects_single_line() -> None:
    assert parse_markdown_table(["| a | b |"]) is None


def test_split_row_honours_escaped_pipes() -> None:
    assert split_row("| a \\| b | c |") == ["a | b", "c"]


def test_render_round_trips_through_parse() -> None:
    rendered = render_markdown_table(["列一", "列二"], [["x", "y"]])
    table = parse_markdown_table(rendered.splitlines())
    assert table is not None
    assert table.header == ["列一", "列二"]
    assert table.rows == [["x", "y"]]


def test_render_escapes_pipes_in_cells() -> None:
    rendered = render_markdown_table(["a"], [["x | y"]])
    table = parse_markdown_table(rendered.splitlines())
    assert table is not None
    assert table.rows == [["x | y"]]


def test_is_table_line_is_strict_about_both_edges() -> None:
    assert is_table_line("| a | b |")
    assert not is_table_line("a | b")
    assert not is_table_line("| only left")


# ---------------------------------------------------------------------------
# locator markers
# ---------------------------------------------------------------------------


def test_marker_quotes_values_containing_spaces() -> None:
    assert marker(sheet="Sheet 1", row_start=2) == '<!-- kb:sheet="Sheet 1" row_start=2 -->'


def test_parse_marker_recovers_typed_fields() -> None:
    parsed = parse_marker('<!-- kb:sheet="Sheet 1" row_start=2 -->')
    assert parsed == {"sheet": "Sheet 1", "row_start": 2}


def test_parse_marker_ignores_ordinary_html_comments() -> None:
    assert parse_marker("<!-- just a note -->") is None


def test_marked_lines_carry_the_locator_and_drop_marker_lines() -> None:
    markdown = "<!-- kb:page=1 -->\nfirst\n<!-- kb:page=2 -->\nsecond"
    lines = list(iter_marked_lines(markdown))
    assert [line.text for line in lines] == ["first", "second"]
    assert lines[0].locator == {"page": 1}
    assert lines[1].locator == {"page": 2}


def test_strip_markers_returns_clean_text() -> None:
    markdown = "<!-- kb:page=1 -->\nhello\n<!-- kb:page=2 -->\nworld"
    assert strip_markers(markdown) == "hello\nworld"


def test_shift_rows_moves_a_row_locator() -> None:
    assert shift_rows({"sheet": "S", "row_start": 5, "row_end": 9}, 4) == {
        "sheet": "S",
        "row_start": 9,
        "row_end": 13,
    }


def test_shift_rows_leaves_page_locators_alone() -> None:
    assert shift_rows({"page": 3}, 10) == {"page": 3}


# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_extracts_title_and_tags() -> None:
    parsed = parse_frontmatter("---\ntitle: Q3 复盘\ntags: [work, q3]\n---\n# body\ntext\n")
    assert parsed.title == "Q3 复盘"
    assert parsed.tags == ["work", "q3"]
    assert parsed.body.startswith("# body")


def test_parse_frontmatter_accepts_a_scalar_tags_line() -> None:
    parsed = parse_frontmatter("---\ntags: work, q3\n---\nbody\n")
    assert parsed.tags == ["work", "q3"]


def test_parse_frontmatter_handles_documents_without_frontmatter() -> None:
    parsed = parse_frontmatter("# just a note\n")
    assert parsed.title is None
    assert parsed.tags == []
    assert parsed.body == "# just a note\n"


def test_parse_frontmatter_survives_malformed_yaml() -> None:
    """A broken frontmatter must not fail a sync — the note is still indexable."""
    text = "---\ntitle: [unclosed\n---\nbody\n"
    parsed = parse_frontmatter(text)
    assert parsed.title is None
    assert parsed.body == text


def test_parse_frontmatter_requires_a_closing_delimiter() -> None:
    text = "---\ntitle: x\nno closing delimiter\n"
    parsed = parse_frontmatter(text)
    assert parsed.title is None
    assert parsed.body == text


def test_derive_title_falls_back_to_first_heading() -> None:
    assert derive_title("## 认证流程\n\n内容\n") == "认证流程"


def test_derive_title_falls_back_to_first_paragraph() -> None:
    assert derive_title("一句直接开头的话\n\n更多\n") == "一句直接开头的话"


def test_derive_title_returns_none_for_empty_body() -> None:
    assert derive_title("\n\n") is None
