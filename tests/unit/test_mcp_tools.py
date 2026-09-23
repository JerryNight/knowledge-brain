"""Unit tests for the MCP tools (spec §9).

These drive the tools directly rather than over the streamable-HTTP transport: the
behaviour under test is what the tool *returns*, and spec §9 is unusually specific
about that. In particular, an empty result must come back as a sentence — an empty
JSON array reads to a model as "the tool is broken" and it will stop trying.

The principal is set through ``kb.context``, which is exactly what the
authentication middleware does before calling into MCP, so these tests exercise
the same path a live request does.
"""

from __future__ import annotations

import json
import uuid

import pytest

from kb.context import NotAuthenticated, Principal, reset_principal, set_principal
from kb.db.adapters.documents import DocumentSummary
from kb.mcp.server import build_mcp_server
from kb.retrieval.types import SearchHit, SearchResult
from tests.unit.fakes import FakeDocumentReader, FakeRetrievalService, make_services

USER = uuid.UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture
def principal():
    token = set_principal(Principal(user_id=USER))
    yield USER
    reset_principal(token)


def hit(chunk_id: int, path: str = "notes/a.md") -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        path=path,
        source="git",
        score=0.03,
        snippet="…原文片段…",
        title="标题",
        heading_path=("第一章",),
    )


def summary(path: str, *, status: str = "ok", source: str = "git") -> DocumentSummary:
    return DocumentSummary(
        id=uuid.uuid4(),
        source=source,
        source_path=path,
        title=path,
        conversion_status=status,
        conversion_error=None,
        size_bytes=128,
    )


def tool_functions(services):
    """Pull the registered tool callables off a freshly built server.

    Reaching into ``_tool_manager`` is a little intimate, but the alternative —
    standing up the whole transport in a unit test — trades a lot of setup for
    very little extra coverage of the code under test.
    """
    server = build_mcp_server(services)
    tools = server._tool_manager._tools  # noqa: SLF001 - see docstring
    return {name: entry.fn for name, entry in tools.items()}


async def test_all_three_tools_are_registered() -> None:
    assert set(tool_functions(make_services())) == {"search_notes", "read_note", "list_notes"}


async def test_search_notes_passes_filters_and_returns_provenance(principal) -> None:
    retrieval = FakeRetrievalService(SearchResult(query="", hits=(hit(1),)))
    services = make_services(retrieval=retrieval)
    tools = tool_functions(services)

    payload = json.loads(
        await tools["search_notes"]("认证流程", limit=10, tags=["架构"], path_prefix="notes/")
    )

    query = retrieval.queries[0]
    assert query.query == "认证流程"
    assert query.limit == 10
    assert query.tags == ("架构",)
    assert query.path_prefix == "notes/"
    assert retrieval.user_ids == [USER]
    assert payload["count"] == 1
    assert payload["results"][0]["path"] == "notes/a.md"
    assert payload["results"][0]["heading_path"] == ["第一章"]


async def test_search_notes_with_no_hits_returns_a_message_not_an_empty_array(principal) -> None:
    """spec §9: 空结果明确说「没有匹配的笔记」，不返回空数组。"""
    services = make_services(retrieval=FakeRetrievalService(SearchResult(query="", hits=())))
    payload = json.loads(await tool_functions(services)["search_notes"]("找不到的东西"))

    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["message"]
    assert "没有匹配" in payload["message"]


async def test_read_note_returns_full_text(principal) -> None:
    reader = FakeDocumentReader(summaries=[summary("notes/a.md")], contents={"notes/a.md": "全文内容"})
    services = make_services(documents=reader)
    payload = json.loads(await tool_functions(services)["read_note"]("notes/a.md"))

    assert payload["found"] is True
    assert payload["text"] == "全文内容"
    assert payload["path"] == "notes/a.md"
    # A markdown note is the note itself, so no conversion caveat is attached.
    assert "note" not in payload


async def test_read_note_flags_converted_output_so_a_model_knows_what_it_holds(principal) -> None:
    """spec §9: PDF 返回转换产物并显式标注，不能让 Claude 以为是原始排版。"""
    reader = FakeDocumentReader(
        summaries=[summary("报告.pdf")], contents={"报告.pdf": "| 表格 | 可能错乱 |"}
    )
    payload = json.loads(await tool_functions(make_services(documents=reader))["read_note"]("报告.pdf"))

    assert payload["note"]
    assert "转换" in payload["note"]
    assert "报告.pdf" in payload["note"]


async def test_read_note_reports_a_missing_file_clearly(principal) -> None:
    services = make_services(documents=FakeDocumentReader())
    payload = json.loads(await tool_functions(services)["read_note"]("nope.md"))

    assert payload["found"] is False
    assert "nope.md" in payload["message"]
    assert "list_notes" in payload["message"]


async def test_list_notes_returns_summaries_and_honours_the_prefix(principal) -> None:
    reader = FakeDocumentReader(summaries=[summary("notes/a.md"), summary("other/b.md")])
    payload = json.loads(await tool_functions(make_services(documents=reader))["list_notes"]("notes/"))

    assert payload["count"] == 1
    assert payload["notes"][0]["path"] == "notes/a.md"
    assert reader.browsed == [("notes/", 100)]


async def test_list_notes_on_an_empty_vault_explains_how_to_populate_it(principal) -> None:
    payload = json.loads(await tool_functions(make_services(documents=FakeDocumentReader()))["list_notes"]())

    assert payload["count"] == 0
    assert payload["notes"] == []
    assert "还没有" in payload["message"]


async def test_list_notes_clamps_its_limit(principal) -> None:
    reader = FakeDocumentReader(summaries=[summary("notes/a.md")])
    await tool_functions(make_services(documents=reader))["list_notes"](None, 10_000)
    assert reader.browsed == [(None, 500)]


async def test_tools_refuse_to_run_without_a_principal() -> None:
    """Defence in depth: the middleware is the real gate, but a missing principal
    must fail closed rather than falling back to "some tenant"."""
    tools = tool_functions(make_services(documents=FakeDocumentReader()))
    with pytest.raises(NotAuthenticated):
        await tools["read_note"]("notes/a.md")
