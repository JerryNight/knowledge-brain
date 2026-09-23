"""Retrieval quality evaluation — Recall@k and MRR (spec §11.2).

Spec §11 is emphatic that correctness and *quality* are two different things
needing two different instruments. The unit and integration suites prove the
system does not break; this module answers whether it actually finds the right
notes, which is a question no assertion about chunk counts can settle.

Three deliberate design choices, each taken from the reference project's hard-won
experience (spec §11.2):

* **No target thresholds.** The first job is a baseline. Spec §11.2's warning is
  specific: a made-up "R@5 must exceed 0.8" pushes you to tune parameters until
  the number looks good rather than to make the system better.
* **Ranking is measured at the document level.** ``search_notes`` returns chunks;
  a user wants a *file*. Relevance is therefore "did this file appear in the top
  k", with duplicates collapsed, so a long note cannot inflate its own score.
* **Coverage matters more than precision.** The design deliberately over-recalls
  and lets the caller select (spec §8), so Recall@5/@10 and MRR are the honest
  metrics; precision@k would penalise the intended behaviour.

The set itself has to be built from real notes — spec §11.2 asks for 50-100
queries covering three styles (keyword, semantic, entity). ``eval/queries.example.jsonl``
is a template with a handful of seeds, not a substitute: a set written by the
same person who wrote the retrieval code measures nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kb.retrieval.service import RetrievalService
from kb.retrieval.types import SearchQuery

# The styles spec §11.2 asks for. Recorded per case so a report can show *which
# kind* of query regressed — an aggregate that hides a keyword collapse behind a
# semantic improvement is worse than no report.
STYLE_KEYWORD = "keyword"
STYLE_SEMANTIC = "semantic"
STYLE_ENTITY = "entity"
STYLES = (STYLE_KEYWORD, STYLE_SEMANTIC, STYLE_ENTITY)

DEFAULT_KS = (5, 10)


class EvalSetError(ValueError):
    """The evaluation file is malformed or empty."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    query: str
    relevant: tuple[str, ...]
    style: str = STYLE_KEYWORD
    note: str = ""

    @classmethod
    def from_mapping(cls, payload: dict, *, line: int) -> EvalCase:
        query = str(payload.get("query", "")).strip()
        if not query:
            raise EvalSetError(f"line {line}: missing 'query'")
        relevant = payload.get("relevant") or payload.get("paths") or []
        if isinstance(relevant, str):
            relevant = [relevant]
        if not relevant:
            raise EvalSetError(f"line {line}: 'relevant' must name at least one path")
        style = str(payload.get("style", STYLE_KEYWORD))
        if style not in STYLES:
            raise EvalSetError(f"line {line}: unknown style {style!r}; expected one of {STYLES}")
        return cls(
            query=query,
            relevant=tuple(str(path) for path in relevant),
            style=style,
            note=str(payload.get("note", "")),
        )


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: EvalCase
    ranked_paths: tuple[str, ...]
    first_relevant_rank: int | None

    def recall_at(self, k: int) -> float:
        top = set(self.ranked_paths[:k])
        return len(top & set(self.case.relevant)) / len(self.case.relevant)

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_relevant_rank if self.first_relevant_rank else 0.0

    @property
    def found(self) -> bool:
        """Whether anything relevant appeared at all, at any depth."""
        return self.first_relevant_rank is not None


@dataclass(slots=True)
class EvalReport:
    cases: tuple[EvalCase, ...]
    results: tuple[CaseResult, ...]
    ks: tuple[int, ...] = DEFAULT_KS
    branch_stats: dict[str, int] = field(default_factory=dict)

    @property
    def recall(self) -> dict[int, float]:
        if not self.results:
            return {k: 0.0 for k in self.ks}
        return {
            k: sum(result.recall_at(k) for result in self.results) / len(self.results) for k in self.ks
        }

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.reciprocal_rank for result in self.results) / len(self.results)

    @property
    def coverage(self) -> float:
        """Share of queries where *something* relevant was retrieved."""
        if not self.results:
            return 0.0
        return sum(1 for result in self.results if result.found) / len(self.results)

    def by_style(self) -> dict[str, dict[str, float]]:
        """The same metrics split by query style — where the regression hides."""
        grouped: dict[str, list[CaseResult]] = {}
        for result in self.results:
            grouped.setdefault(result.case.style, []).append(result)

        report: dict[str, dict[str, float]] = {}
        for style, results in sorted(grouped.items()):
            report[style] = {
                **{
                    f"recall@{k}": sum(r.recall_at(k) for r in results) / len(results)
                    for k in self.ks
                },
                "mrr": sum(r.reciprocal_rank for r in results) / len(results),
                "cases": float(len(results)),
            }
        return report

    def incomplete(self, k: int | None = None) -> list[CaseResult]:
        """Cases not *fully* retrieved within ``k`` — the ones worth reading.

        Two distinct situations land here and both are useful: nothing relevant
        appeared at all, and a relevant file appeared but ranked below the cut.
        The per-case output shows which, because the fixes differ.
        """
        window = k or max(self.ks)
        return [result for result in self.results if result.recall_at(window) < 1.0]

    def unfound(self) -> list[CaseResult]:
        """Cases where nothing relevant was retrieved at any depth.

        A strict subset of ``incomplete``, and the more alarming one: it means the
        candidate set itself is wrong, not merely misordered.
        """
        return [result for result in self.results if not result.found]

    def as_dict(self) -> dict:
        return {
            "cases": len(self.cases),
            "recall": {f"recall@{k}": round(value, 4) for k, value in self.recall.items()},
            "mrr": round(self.mrr, 4),
            "coverage": round(self.coverage, 4),
            "by_style": {
                style: {key: round(value, 4) for key, value in metrics.items()}
                for style, metrics in self.by_style().items()
            },
            "branch_stats": self.branch_stats,
            "unfound": [
                {"query": result.case.query, "style": result.case.style, "expected": list(result.case.relevant)}
                for result in self.unfound()
            ],
            "incomplete": [
                {
                    "query": result.case.query,
                    "style": result.case.style,
                    "expected": list(result.case.relevant),
                    "got": list(result.ranked_paths[: max(self.ks)]),
                }
                for result in self.incomplete()
            ],
        }

    def as_markdown(self) -> str:
        lines = [
            "# 检索质量基线",
            "",
            f"- 用例数：{len(self.cases)}",
            f"- 覆盖率（至少命中一次）：{self.coverage:.3f}",
            f"- MRR：{self.mrr:.4f}",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
        ]
        for k, value in sorted(self.recall.items()):
            lines.append(f"| Recall@{k} | {value:.4f} |")
        lines.append(f"| MRR | {self.mrr:.4f} |")
        lines += ["", "## 按查询风格", "", "| 风格 | 用例 | Recall@5 | Recall@10 | MRR |", "|---|---|---|---|---|"]
        for style, metrics in self.by_style().items():
            lines.append(
                f"| {style} | {int(metrics['cases'])} | "
                f"{metrics.get('recall@5', 0):.3f} | {metrics.get('recall@10', 0):.3f} | {metrics['mrr']:.3f} |"
            )
        incomplete = self.incomplete()
        if incomplete:
            lines += ["", f"## 未完全命中（{len(incomplete)} 条，前 20）", ""]
            for result in incomplete[:20]:
                lines.append(f"- `{result.case.query}`（{result.case.style}）")
                lines.append(f"  - 期望：{', '.join(result.case.relevant)}")
                expected = max(self.ks)
                lines.append(f"  - 实得：{', '.join(result.ranked_paths[:expected]) or '（无）'}")
        else:
            lines += ["", "全部用例在前 10 条内命中。"]
        return "\n".join(lines) + "\n"


def load_cases(path: str | Path) -> list[EvalCase]:
    """Load a JSONL (or JSON array) evaluation set.

    Both shapes are accepted because the file is meant to be edited by hand and
    grown over time; forcing JSONL on someone who has four queries is friction for
    no benefit. Validation is strict about the two things that make a case
    meaningless — no query, or no expected path.
    """
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise EvalSetError(f"{path} is empty")

    if text.startswith("["):
        payload = json.loads(text)
        cases = [EvalCase.from_mapping(item, line=index + 1) for index, item in enumerate(payload)]
    else:
        cases = []
        for index, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise EvalSetError(f"line {index}: not JSON ({exc})") from exc
            cases.append(EvalCase.from_mapping(payload, line=index))

    if not cases:
        raise EvalSetError(f"{path} contains no cases")
    return cases


async def evaluate(
    retrieval: RetrievalService,
    cases: Sequence[EvalCase],
    *,
    user_id,
    ks: Sequence[int] = DEFAULT_KS,
    limit: int | None = None,
) -> EvalReport:
    """Run every case and compute document-level Recall@k and MRR.

    Ranking is deduplicated by path while preserving order, because
    ``search_notes`` is chunk-level and the question being asked is file-level.
    Clips at ``max(ks)``: computing recall@10 does not need the full 25-slot wide
    recall, and the extra calls would only add failure modes to the measurement.
    """
    window = max(*ks, 1) if ks else max(DEFAULT_KS)
    results: list[CaseResult] = []
    branch_totals: dict[str, int] = {}

    for case in cases:
        result = await retrieval.search(
            SearchQuery(query=case.query, limit=limit or window), user_id=user_id
        )
        for branch, count in result.branches.items():
            branch_totals[branch] = branch_totals.get(branch, 0) + count

        ranked: list[str] = []
        for hit in result.hits:
            if hit.path not in ranked:
                ranked.append(hit.path)

        first_rank = next(
            (index for index, path in enumerate(ranked, start=1) if path in case.relevant), None
        )
        results.append(
            CaseResult(case=case, ranked_paths=tuple(ranked), first_relevant_rank=first_rank)
        )

    return EvalReport(
        cases=tuple(cases),
        results=tuple(results),
        ks=tuple(sorted(ks)),
        branch_stats=branch_totals,
    )


def summarise_styles(cases: Iterable[EvalCase]) -> dict[str, int]:
    """Case counts per style — used to warn when a style is uncovered."""
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.style] = counts.get(case.style, 0) + 1
    return counts


__all__ = [
    "DEFAULT_KS",
    "STYLES",
    "STYLE_ENTITY",
    "STYLE_KEYWORD",
    "STYLE_SEMANTIC",
    "CaseResult",
    "EvalCase",
    "EvalReport",
    "EvalSetError",
    "evaluate",
    "load_cases",
    "summarise_styles",
]
