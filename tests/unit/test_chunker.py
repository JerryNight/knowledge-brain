"""Unit tests for the markdown chunker — spec §8's rules, one test each."""

from __future__ import annotations

from kb.indexer.chunker import ChunkDraft, ChunkOptions, chunk_markdown, render_heading_context
from kb.indexer.locator import marker
from kb.indexer.tables import render_markdown_table

OPTIONS = ChunkOptions(min_tokens=120, max_tokens=800, overlap_ratio=0.15)


def body(tokens: int, prefix: str = "") -> str:
    """A paragraph of exactly ``tokens`` estimated tokens (one per CJK char)."""
    return prefix + "字" * tokens


# ---------------------------------------------------------------------------
# heading boundaries and heading_path
# ---------------------------------------------------------------------------


def test_sections_split_at_heading_boundaries() -> None:
    markdown = f"# 第一章\n\n{body(300, '甲')}\n\n## 1.1 小节\n\n{body(300, '乙')}\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert len(chunks) == 2
    assert chunks[0].heading_path == ["第一章"]
    assert chunks[1].heading_path == ["第一章", "1.1 小节"]
    assert chunks[0].text == body(300, "甲")
    assert chunks[1].text == body(300, "乙")


def test_heading_lines_are_not_repeated_in_chunk_text() -> None:
    """The heading lives in heading_path; the text the user is shown is content."""
    chunks = chunk_markdown(f"# 认证\n\n{body(200)}\n", OPTIONS)
    assert "# 认证" not in chunks[0].text


def test_deep_headings_stay_in_the_text_without_splitting() -> None:
    markdown = f"# 一\n\n{body(200, '甲')}\n\n#### 细节\n\n{body(200, '乙')}\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert len(chunks) == 1
    assert chunks[0].heading_path == ["一"]
    assert "#### 细节" in chunks[0].text


def test_ordinals_are_contiguous_from_zero() -> None:
    markdown = "".join(f"# 第{i}章\n\n{body(200)}\n\n" for i in range(4))
    chunks = chunk_markdown(markdown, OPTIONS)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# short sections merge upward
# ---------------------------------------------------------------------------


def test_short_section_merges_into_the_previous_one() -> None:
    markdown = f"# 大章\n\n{body(300)}\n\n## 小注\n\n短\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert len(chunks) == 1
    assert chunks[0].heading_path == ["大章"]
    # The merged section keeps its heading as content: its label would otherwise
    # be lost, since heading_path belongs to the group it joined.
    assert "## 小注" in chunks[0].text
    assert "短" in chunks[0].text


def test_short_leading_section_merges_forward() -> None:
    markdown = f"# 开场\n\n短\n\n# 正文\n\n{body(300)}\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert len(chunks) == 1
    assert chunks[0].heading_path == ["正文"]
    assert "短" in chunks[0].text


def test_a_lone_short_section_is_kept_as_its_own_chunk() -> None:
    chunks = chunk_markdown("# 只有一行\n\n短\n", OPTIONS)
    assert len(chunks) == 1
    assert chunks[0].text == "短"


def test_merge_is_refused_when_it_would_exceed_the_ceiling() -> None:
    """Merging two sections into an oversized chunk would only be re-split."""
    markdown = f"# 大\n\n{body(780)}\n\n## 小\n\n{body(50)}\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert len(chunks) == 2
    assert chunks[1].heading_path == ["大", "小"]


# ---------------------------------------------------------------------------
# sliding window
# ---------------------------------------------------------------------------


def test_long_section_is_split_into_a_sliding_window_with_overlap() -> None:
    atoms = [body(100, f"第{i}段") for i in range(12)]
    chunks = chunk_markdown("# 大\n\n" + "\n\n".join(atoms) + "\n", OPTIONS)

    assert len(chunks) >= 2
    assert all(chunk.token_count <= OPTIONS.max_tokens for chunk in chunks)
    # The next window starts back inside the previous one: that is the 15%
    # overlap, and it is what keeps a paragraph broken across the boundary
    # readable somewhere.
    last_atom_of_first = chunks[0].text.rsplit("\n\n", 1)[-1]
    first_atom_of_second = chunks[1].text.split("\n\n", 1)[0]
    assert last_atom_of_first == first_atom_of_second


def test_sliding_window_never_loses_content() -> None:
    atoms = [body(100, f"第{i}段") for i in range(12)]
    chunks = chunk_markdown("# 大\n\n" + "\n\n".join(atoms) + "\n", OPTIONS)
    joined = "\n\n".join(chunk.text for chunk in chunks)
    for atom in atoms:
        assert atom in joined


# ---------------------------------------------------------------------------
# code blocks
# ---------------------------------------------------------------------------


def test_code_block_is_never_cut_even_when_oversized() -> None:
    code = "\n".join(f"print('第{i}行')" for i in range(300))
    markdown = f"# 代码\n\n```python\n{code}\n```\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert len(chunks) == 1
    assert chunks[0].text.count("```") == 2
    assert code in chunks[0].text
    assert chunks[0].token_count > OPTIONS.max_tokens  # deliberately long


def test_oversized_code_block_does_not_swallow_neighbouring_text() -> None:
    code = "\n".join(f"print('第{i}行')" for i in range(300))
    markdown = f"# 混合\n\n{body(200, '前')}\n\n```python\n{code}\n```\n\n{body(200, '后')}\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert any(chunk.text.startswith("前") for chunk in chunks)
    assert any(chunk.text.startswith("后") for chunk in chunks)
    assert sum(chunk.text.count("```") for chunk in chunks) == 2
    assert any(code in chunk.text for chunk in chunks)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def test_small_table_stays_whole() -> None:
    table = render_markdown_table(["a", "b"], [["1", "2"], ["3", "4"]])
    chunks = chunk_markdown(f"## 表\n\n{table}\n", OPTIONS)

    assert len(chunks) == 1
    assert table in chunks[0].text


def test_wide_table_is_grouped_by_rows_with_a_repeated_header() -> None:
    header = [f"列{i}" for i in range(12)]
    rows = [[f"r{row}c{col}数据" for col in range(12)] for row in range(40)]
    table = render_markdown_table(header, rows)
    markdown = f"## Sheet1\n\n{marker(sheet='Sheet1', row_start=1)}\n\n{table}\n"

    chunks = chunk_markdown(markdown, OPTIONS)

    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.text.splitlines()[0].startswith("| 列0")
    assert "r0c0数据" not in chunks[1].text


def test_table_group_locators_track_real_spreadsheet_rows() -> None:
    """Every group must point at its own rows, not at the top of the sheet."""
    header = [f"列{i}" for i in range(12)]
    rows = [[f"r{row}c{col}数据" for col in range(12)] for row in range(40)]
    table = render_markdown_table(header, rows)
    markdown = f"## Sheet1\n\n{marker(sheet='Sheet1', row_start=1)}\n\n{table}\n"

    chunks = chunk_markdown(markdown, OPTIONS)

    assert chunks[0].locator == {"sheet": "Sheet1", "row_start": 2, "row_end": 22}
    assert chunks[1].locator == {"sheet": "Sheet1", "row_start": 23, "row_end": 41}
    assert len({chunk.locator["row_start"] for chunk in chunks}) == len(chunks)


def test_tall_narrow_table_is_also_grouped() -> None:
    table = render_markdown_table(
        ["编号", "说明"],
        [[f"N{i}号", "说明" * 20] for i in range(200)],
    )
    chunks = chunk_markdown(f"## 台账\n\n{table}\n", OPTIONS)
    assert len(chunks) >= 2
    assert all(chunk.text.splitlines()[0] == "| 编号 | 说明 |" for chunk in chunks)


# ---------------------------------------------------------------------------
# locators, embedding window, embed text
# ---------------------------------------------------------------------------


def test_page_markers_become_chunk_locators_and_leave_the_text() -> None:
    markdown = f"# 一\n\n<!-- kb:page=1 -->\n\n{body(300, '甲')}\n\n# 二\n\n<!-- kb:page=2 -->\n\n{body(300, '乙')}\n"
    chunks = chunk_markdown(markdown, OPTIONS)

    assert [chunk.locator for chunk in chunks] == [{"page": 1}, {"page": 2}]
    assert all("kb:page" not in chunk.text for chunk in chunks)


def test_markdown_chunks_have_no_locator() -> None:
    """Locator is null for markdown — heading_path is the locator there (spec §8)."""
    chunks = chunk_markdown(f"# 标题\n\n{body(200)}\n", OPTIONS)
    assert chunks[0].locator is None
    assert chunks[0].heading_path == ["标题"]


def test_embedding_window_excludes_tiny_and_huge_chunks() -> None:
    options = ChunkOptions(embed_skip_min_tokens=10, embed_skip_max_tokens=8000)

    assert not ChunkDraft(0, "短", token_count=9).needs_embedding(options)
    assert ChunkDraft(0, "适中", token_count=10).needs_embedding(options)
    assert ChunkDraft(0, "适中", token_count=8000).needs_embedding(options)
    assert not ChunkDraft(0, "很长", token_count=8001).needs_embedding(options)


def test_embed_text_prefixes_heading_context_but_text_stays_clean() -> None:
    chunk = ChunkDraft(ordinal=0, text="jwt 校验只接受 RS256", heading_path=["认证", "3.2 认证流程"])
    assert chunk.embed_text == "# 认证\n## 3.2 认证流程\n\njwt 校验只接受 RS256"
    assert chunk.embed_text != chunk.text


def test_render_heading_context_caps_at_six_levels() -> None:
    assert (
        render_heading_context(["a", "b", "c", "d", "e", "f", "g"])
        == "# a\n## b\n### c\n#### d\n##### e\n###### f\n###### g"
    )


# ---------------------------------------------------------------------------
# degenerate input
# ---------------------------------------------------------------------------


def test_empty_document_produces_no_chunks() -> None:
    assert chunk_markdown("", OPTIONS) == []
    assert chunk_markdown("\n\n   \n", OPTIONS) == []


def test_blocks_are_separated_by_a_blank_line_in_the_stored_text() -> None:
    markdown = f"# 标题\n\n{body(200, '甲')}\n\n{body(200, '乙')}\n"
    chunks = chunk_markdown(markdown, OPTIONS)
    assert chunks[0].text == f"{body(200, '甲')}\n\n{body(200, '乙')}"
