"""Unit tests for hybrid retrieval — fusion, capping, degradation (spec §11.1 #6).

``reciprocal_rank_fusion`` is a pure function, and spec §11.1 #6 asks for exactly
that: a direct test of the ranking it produces. The service tests around it pin
down the four behaviours that are policy rather than arithmetic:

* one branch failing is not a failed query (§8);
* an empty result carries a message, never a bare empty list (§9);
* one document cannot flood the result set (§8's per-document cap);
* the wide-recall window is 25 by default and 50 at most (§8).
"""

from __future__ import annotations

import uuid

import pytest

from kb.retrieval.rrf import cap_per_document, reciprocal_rank_fusion
from kb.retrieval.service import RetrievalService, RetrievalSettings
from kb.retrieval.types import (
    BRANCH_KEYWORD,
    BRANCH_VECTOR,
    EMPTY_RESULT_MESSAGE,
    SearchQuery,
    make_snippet,
)
from tests.unit.fakes import FakeQueryEmbedder, FakeSearchBackend, candidate

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")

SETTINGS = RetrievalSettings(
    default_limit=25, max_limit=50, per_document_limit=3, branch_limit=40, rrf_k=60
)


# ---------------------------------------------------------------------------
# RRF — the pure function
# ---------------------------------------------------------------------------


def test_rrf_scores_are_one_over_k_plus_rank() -> None:
    """``score = Σ 1 / (k + rank)``, with rank starting at 1 (spec §8)."""
    fused = dict(reciprocal_rank_fusion([[7, 9]], k=60))
    assert fused[7] == pytest.approx(1 / 61)
    assert fused[9] == pytest.approx(1 / 62)


def test_appearing_in_both_branches_beats_topping_one() -> None:
    """The property that makes a second branch worth paying for."""
    vector = [1, 2, 3]
    keyword = [3, 4, 5]
    ranked = [chunk_id for chunk_id, _ in reciprocal_rank_fusion([vector, keyword], k=60)]
    # 3 is 3rd in vector and 1st in keyword: 1/63 + 1/61 > 1/61.
    assert ranked[0] == 3


def test_rrf_ties_break_on_id_so_results_are_stable() -> None:
    fused = reciprocal_rank_fusion([[5, 2], [2, 5]], k=60)
    assert [chunk_id for chunk_id, _ in fused] == [2, 5]


def test_rrf_rejects_a_non_positive_k() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([[1]], k=0)


# ---------------------------------------------------------------------------
# per-document cap
# ---------------------------------------------------------------------------


def test_cap_keeps_at_most_n_per_document() -> None:
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    items = [(doc_a, "a1"), (doc_a, "a2"), (doc_a, "a3"), (doc_a, "a4"), (doc_b, "b1")]
    kept = cap_per_document(items, per_document_limit=3, limit=10, document_of=lambda x: x[0])
    assert [item[1] for item in kept] == ["a1", "a2", "a3", "b1"]


def test_cap_runs_before_the_final_cut_so_later_slots_go_to_other_documents() -> None:
    """Otherwise one long note's chunks would consume the whole result set."""
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    items = [(doc_a, "a1"), (doc_a, "a2"), (doc_a, "a3"), (doc_a, "a4"), (doc_b, "b1")]
    kept = cap_per_document(items, per_document_limit=3, limit=4, document_of=lambda x: x[0])
    assert (doc_b, "b1") in kept


def test_cap_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        cap_per_document([(1, 1)], per_document_limit=0, limit=5, document_of=lambda x: x[0])


# ---------------------------------------------------------------------------
# service behaviour
# ---------------------------------------------------------------------------


def service(backend: FakeSearchBackend, embedder=None) -> RetrievalService:
    return RetrievalService(backend=backend, query_embedder=embedder, settings=SETTINGS)


async def test_both_branches_are_queried_and_fused() -> None:
    doc = uuid.uuid4()
    backend = FakeSearchBackend(
        vector=[candidate(1, document_id=doc)], keyword=[candidate(2, document_id=doc)]
    )
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="认证"), user_id=USER)
    assert {hit.chunk_id for hit in result.hits} == {1, 2}
    assert result.branches == {BRANCH_VECTOR: 1, BRANCH_KEYWORD: 1}
    assert result.degraded == ()


async def test_a_failed_vector_branch_still_answers_from_keywords() -> None:
    """A 429 from the embedding provider must not take the tool offline (spec §8)."""
    backend = FakeSearchBackend(keyword=[candidate(2)], fail_vector=True)
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="认证"), user_id=USER)
    assert [hit.chunk_id for hit in result.hits] == [2]
    assert result.degraded == (BRANCH_VECTOR,)


async def test_a_failed_keyword_branch_still_answers_from_vectors() -> None:
    backend = FakeSearchBackend(vector=[candidate(1)], fail_keyword=True)
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="认证"), user_id=USER)
    assert [hit.chunk_id for hit in result.hits] == [1]
    assert result.degraded == (BRANCH_KEYWORD,)


async def test_a_failed_embedder_degrades_the_vector_branch() -> None:
    backend = FakeSearchBackend(keyword=[candidate(2)])
    result = await service(backend, FakeQueryEmbedder(fail=True)).search(
        SearchQuery(query="认证"), user_id=USER
    )
    assert [hit.chunk_id for hit in result.hits] == [2]
    assert result.degraded == (BRANCH_VECTOR,)


async def test_both_branches_down_says_so_instead_of_returning_nothing() -> None:
    """Every branch failing is "unavailable", not "no matches" (spec §9)."""
    backend = FakeSearchBackend(fail_vector=True, fail_keyword=True)
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="认证"), user_id=USER)
    assert result.is_empty
    assert "暂时不可用" in (result.message or "")
    assert "不要据此判断知识库里没有相关内容" in (result.message or "")


async def test_a_partly_degraded_empty_result_is_reported_as_incomplete() -> None:
    """One branch down and the other empty means *unknown*, so it must not claim emptiness."""
    backend = FakeSearchBackend(keyword=[], fail_vector=True)
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="认证"), user_id=USER)
    assert result.is_empty
    assert "可能不完整" in (result.message or "")
    assert result.message != EMPTY_RESULT_MESSAGE


async def test_empty_result_speaks_rather_than_returning_an_empty_list() -> None:
    """spec §9: an empty array reads to a model as "the tool is broken"."""
    backend = FakeSearchBackend()
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="不存在"), user_id=USER)
    assert result.message == EMPTY_RESULT_MESSAGE
    assert result.as_dict()["message"] == EMPTY_RESULT_MESSAGE


async def test_a_blank_query_short_circuits_without_calling_any_branch() -> None:
    backend = FakeSearchBackend(keyword=[candidate(1)])
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="   "), user_id=USER)
    assert result.is_empty
    assert backend.calls == []


async def test_limit_is_clamped_to_the_wide_recall_window() -> None:
    backend = FakeSearchBackend()
    await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="认证", limit=999), user_id=USER)
    # The branch limit is what is passed down; clamping applies to the final cut.
    assert all(limit == SETTINGS.branch_limit for _, limit, _ in backend.calls)


async def test_filters_reach_both_branches() -> None:
    backend = FakeSearchBackend()
    await service(backend, FakeQueryEmbedder()).search(
        SearchQuery(query="认证", tags=("架构",), path_prefix="notes/"), user_id=USER
    )
    assert [payload for _, _, payload in backend.calls] == [
        (("架构",), "notes/"),
        (("架构",), "notes/"),
    ]


async def test_final_cut_respects_the_clamped_limit() -> None:
    doc = uuid.uuid4()
    backend = FakeSearchBackend(vector=[candidate(index, document_id=doc) for index in range(1, 11)])
    result = await service(backend, FakeQueryEmbedder()).search(
        SearchQuery(query="认证", limit=2), user_id=USER
    )
    assert len(result.hits) == 2


async def test_hits_point_at_the_original_file_not_the_chunk() -> None:
    """spec §8's result object: path/title/source/locator, plus a snippet."""
    backend = FakeSearchBackend(keyword=[candidate(1, path="报告.pdf")])
    result = await service(backend, FakeQueryEmbedder()).search(SearchQuery(query="报告"), user_id=USER)
    payload = result.as_dict()["results"][0]
    assert set(payload) == {"path", "title", "source", "heading_path", "locator", "score", "snippet"}
    assert payload["path"] == "报告.pdf"


# ---------------------------------------------------------------------------
# snippet shaping
# ---------------------------------------------------------------------------


def test_short_text_is_returned_unchanged() -> None:
    assert make_snippet("很短的一段") == "很短的一段"


def test_long_text_is_cut_on_a_sentence_boundary() -> None:
    text = "第一句。" + "填充" * 400 + "。结尾"
    snippet = make_snippet(text, max_chars=50)
    assert len(snippet) <= 51
    assert snippet.endswith("…")
    assert "第一句。" in snippet
