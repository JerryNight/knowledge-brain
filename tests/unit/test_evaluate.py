"""Unit tests for the retrieval-quality harness (spec §11.2).

The metrics are arithmetic, so they are tested against hand-computed values rather
than against whatever the implementation happens to produce. That matters more
here than elsewhere: this is the only instrument that can tell a real retrieval
improvement from a plausible-looking regression, so a quietly wrong Recall@k
would be worse than having no evaluation at all.

The set loader is tested too, because the file is meant to be hand-edited and
grown — a case with no expected path, or a typo'd style, would silently contribute
a meaningless zero to every number in the report.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from kb.evaluate import (
    STYLE_ENTITY,
    STYLE_KEYWORD,
    STYLE_SEMANTIC,
    CaseResult,
    EvalCase,
    EvalSetError,
    evaluate,
    load_cases,
    summarise_styles,
)
from kb.retrieval.types import SearchHit, SearchResult
from tests.unit.fakes import FakeRetrievalService

USER = uuid.UUID("77777777-7777-7777-7777-777777777777")


def case(query: str = "q", relevant=("a.md",), style: str = STYLE_KEYWORD) -> EvalCase:
    return EvalCase(query=query, relevant=tuple(relevant), style=style)


def hit(path: str, chunk_id: int = 1) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        path=path,
        source="git",
        score=0.01,
        snippet="…",
    )


# ---------------------------------------------------------------------------
# metric arithmetic
# ---------------------------------------------------------------------------


def test_recall_at_k_counts_distinct_documents() -> None:
    result = CaseResult(case=case(relevant=("a.md", "b.md")), ranked_paths=("a.md", "c.md"), first_relevant_rank=1)
    assert result.recall_at(1) == 0.5
    assert result.recall_at(2) == 0.5


def test_reciprocal_rank_uses_the_first_relevant_hit() -> None:
    result = CaseResult(case=case(relevant=("b.md",)), ranked_paths=("x.md", "y.md", "b.md"), first_relevant_rank=3)
    assert result.reciprocal_rank == pytest.approx(1 / 3)


def test_reciprocal_rank_is_zero_when_nothing_relevant_was_retrieved() -> None:
    result = CaseResult(case=case(), ranked_paths=("x.md",), first_relevant_rank=None)
    assert result.reciprocal_rank == 0.0
    assert result.found is False


async def test_report_averages_recall_and_mrr_over_cases() -> None:
    # Case 1: relevant a.md at rank 1 → recall@5 = 1, RR = 1
    # Case 2: relevant b.md at rank 2 → recall@5 = 1, RR = 1/2
    # Case 3: nothing relevant     → recall@5 = 0, RR = 0
    results = [
        CaseResult(case=case(relevant=("a.md",)), ranked_paths=("a.md",), first_relevant_rank=1),
        CaseResult(case=case(relevant=("b.md",)), ranked_paths=("x.md", "b.md"), first_relevant_rank=2),
        CaseResult(case=case(relevant=("c.md",)), ranked_paths=("x.md",), first_relevant_rank=None),
    ]
    from kb.evaluate import EvalReport

    report = EvalReport(cases=tuple(r.case for r in results), results=tuple(results), ks=(5,))
    assert report.recall[5] == pytest.approx(2 / 3)
    assert report.mrr == pytest.approx((1 + 0.5 + 0) / 3)
    assert report.coverage == pytest.approx(2 / 3)
    # Nothing failed to be retrieved outright, but the third case retrieved
    # nothing relevant at all — that is an incomplete case, not an unfound one.
    assert len(report.unfound()) == 1
    assert len(report.incomplete()) == 1


async def test_evaluate_deduplicates_paths_so_a_long_note_cannot_inflate_its_score() -> None:
    """Ranking is document-level: `search_notes` is chunk-level, the user wants a file."""
    hits = [hit("a.md", 1), hit("a.md", 2), hit("a.md", 3), hit("b.md", 4)]
    retrieval = FakeRetrievalService(SearchResult(query="", hits=tuple(hits)))
    report = await evaluate(
        retrieval, [case(query="x", relevant=("b.md",))], user_id=USER, ks=(2,)
    )
    result = report.results[0]
    assert result.ranked_paths == ("a.md", "b.md")
    assert result.first_relevant_rank == 2


async def test_evaluate_records_branch_counts_so_a_degraded_run_is_visible() -> None:
    hits = (hit("a.md"),)
    retrieval = FakeRetrievalService(
        SearchResult(query="", hits=hits, branches={"vector": 3, "keyword": 1})
    )
    report = await evaluate(retrieval, [case()], user_id=USER, ks=(5,))
    assert report.branch_stats == {"vector": 3, "keyword": 1}


async def test_evaluate_passes_the_query_through_unchanged() -> None:
    retrieval = FakeRetrievalService(SearchResult(query="", hits=()))
    await evaluate(retrieval, [case(query="SKIP LOCKED 怎么用")], user_id=USER, ks=(5,))
    assert retrieval.queries[0].query == "SKIP LOCKED 怎么用"
    assert retrieval.user_ids == [USER]


def test_by_style_reveals_a_regression_hidden_by_the_aggregate() -> None:
    """spec §11.2's example: an aggregate can mask one style collapsing."""
    results = [
        CaseResult(case=case(style=STYLE_KEYWORD), ranked_paths=(), first_relevant_rank=None),
        CaseResult(case=case(style=STYLE_SEMANTIC), ranked_paths=("a.md",), first_relevant_rank=1),
        CaseResult(case=case(style=STYLE_SEMANTIC), ranked_paths=("a.md",), first_relevant_rank=1),
    ]
    from kb.evaluate import EvalReport

    report = EvalReport(cases=tuple(r.case for r in results), results=tuple(results), ks=(5,))
    assert report.recall[5] == pytest.approx(2 / 3)
    by_style = report.by_style()
    assert by_style[STYLE_KEYWORD]["recall@5"] == 0.0
    assert by_style[STYLE_SEMANTIC]["recall@5"] == 1.0


def test_markdown_report_lists_both_the_numbers_and_the_misses() -> None:
    results = [CaseResult(case=case(query="张三那个方案"), ranked_paths=("x.md",), first_relevant_rank=None)]
    from kb.evaluate import EvalReport

    report = EvalReport(cases=tuple(r.case for r in results), results=tuple(results), ks=(5, 10))
    markdown = report.as_markdown()
    assert "Recall@5" in markdown
    assert "MRR" in markdown
    assert "张三那个方案" in markdown
    assert "x.md" in markdown


def test_json_report_is_serialisable() -> None:
    results = [CaseResult(case=case(), ranked_paths=("a.md",), first_relevant_rank=1)]
    from kb.evaluate import EvalReport

    report = EvalReport(cases=tuple(r.case for r in results), results=tuple(results), ks=(5,))
    payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
    assert payload["cases"] == 1
    assert payload["mrr"] == 1.0


def test_an_empty_report_does_not_divide_by_zero() -> None:
    from kb.evaluate import EvalReport

    report = EvalReport(cases=(), results=(), ks=(5, 10))
    assert report.recall == {5: 0.0, 10: 0.0}
    assert report.mrr == 0.0
    assert report.coverage == 0.0


# ---------------------------------------------------------------------------
# loading the set
# ---------------------------------------------------------------------------


def test_loads_jsonl_and_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(
            [
                "# 这是注释",
                json.dumps({"query": "a", "relevant": ["x.md"], "style": "keyword"}, ensure_ascii=False),
                "",
                json.dumps({"query": "b", "relevant": ["y.md"], "style": "semantic"}, ensure_ascii=False),
            ]
        ),
        encoding="utf-8",
    )
    cases = load_cases(path)
    assert [c.query for c in cases] == ["a", "b"]


def test_loads_a_json_array_too(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"query": "a", "relevant": ["x.md"]}]), encoding="utf-8")
    assert len(load_cases(path)) == 1


def test_a_single_relevant_path_may_be_a_bare_string(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text('{"query": "a", "relevant": "x.md"}\n', encoding="utf-8")
    assert load_cases(path)[0].relevant == ("x.md",)


def test_a_case_without_a_query_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text('{"relevant": ["x.md"]}\n', encoding="utf-8")
    with pytest.raises(EvalSetError, match="missing 'query'"):
        load_cases(path)


def test_a_case_without_an_expected_path_is_rejected(tmp_path: Path) -> None:
    """Without an expected path the case contributes a meaningless zero forever."""
    path = tmp_path / "cases.jsonl"
    path.write_text('{"query": "a"}\n', encoding="utf-8")
    with pytest.raises(EvalSetError, match="relevant"):
        load_cases(path)


def test_an_unknown_style_is_rejected_so_the_report_stays_groupable(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text('{"query": "a", "relevant": ["x.md"], "style": "vibes"}\n', encoding="utf-8")
    with pytest.raises(EvalSetError, match="unknown style"):
        load_cases(path)


def test_malformed_json_names_the_offending_line(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text('{"query": "a", "relevant": ["x.md"]}\nnot json\n', encoding="utf-8")
    with pytest.raises(EvalSetError, match="line 2"):
        load_cases(path)


def test_an_empty_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(EvalSetError, match="empty"):
        load_cases(path)


def test_style_counts_surface_an_uncovered_style() -> None:
    cases = [case(style=STYLE_KEYWORD), case(style=STYLE_KEYWORD), case(style=STYLE_ENTITY)]
    counts = summarise_styles(cases)
    assert counts == {STYLE_KEYWORD: 2, STYLE_ENTITY: 1}
    assert STYLE_SEMANTIC not in counts


def test_the_shipped_example_set_loads_and_covers_all_three_styles() -> None:
    """The template has to be valid — it is the starting point users copy."""
    path = Path(__file__).resolve().parents[2] / "eval" / "queries.example.jsonl"
    cases = load_cases(path)
    assert len(cases) >= 10
    assert set(summarise_styles(cases)) == {STYLE_KEYWORD, STYLE_SEMANTIC, STYLE_ENTITY}
