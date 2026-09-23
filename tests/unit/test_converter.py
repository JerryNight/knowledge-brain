"""Unit tests for the conversion layer — spec §7.

Real fixtures for the acceptance criteria (three formats plus a scanned document)
belong with the integration suite; what is checked here is the conversion
*policy*: registry order, failure isolation, the size cap, and the cache.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from kb.converter import default_registry
from kb.converter.base import (
    CONVERSION_FAILED,
    CONVERSION_NO_TEXT,
    CONVERSION_OK,
    CONVERSION_UNSUPPORTED,
    ConversionResult,
    ConverterRegistry,
)
from kb.converter.markitdown_conv import MarkItDownConverter
from kb.converter.pdf import PdfConverter, normalise_page
from kb.converter.plaintext import TextConverter
from kb.converter.service import CachedConversion, ConversionService
from kb.converter.xlsx import XlsxConverter
from kb.models.document import CONVERSION_STATUSES


class SpyConverter:
    """Converter that records calls and returns a canned result."""

    name = "spy"

    def __init__(self, result: ConversionResult | None = None) -> None:
        self.calls = 0
        self._result = result or ConversionResult(status=CONVERSION_OK, markdown="ok")

    def supports(self, *, path: str, mime: str | None) -> bool:
        return True

    def convert(self, blob: bytes, *, path: str, mime: str | None) -> ConversionResult:
        self.calls += 1
        return self._result


class ExplodingConverter(SpyConverter):
    def convert(self, blob: bytes, *, path: str, mime: str | None) -> ConversionResult:
        raise RuntimeError("boom")


class FakeCache:
    def __init__(self) -> None:
        self.rows: dict[str, CachedConversion] = {}
        self.puts = 0

    async def get(self, content_sha: str) -> CachedConversion | None:
        return self.rows.get(content_sha)

    async def put(self, content_sha: str, *, status: str, converted: str | None) -> None:
        self.puts += 1
        self.rows[content_sha] = CachedConversion(status=status, converted=converted)


async def sync_thread(func, *args, **kwargs):
    """Run converters inline so tests do not depend on a thread pool."""
    return func(*args, **kwargs)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_default_registry_puts_specific_converters_before_markitdown() -> None:
    assert default_registry().names == ("text", "pdf", "xlsx", "markitdown")


def test_registry_dispatches_by_extension() -> None:
    assert default_registry().find(path="a.md", mime=None).name == "text"  # type: ignore[union-attr]
    assert default_registry().find(path="a.pdf", mime=None).name == "pdf"  # type: ignore[union-attr]
    assert default_registry().find(path="a.xlsx", mime=None).name == "xlsx"  # type: ignore[union-attr]
    assert default_registry().find(path="a.docx", mime=None).name == "markitdown"  # type: ignore[union-attr]


def test_registry_reports_unsupported_without_a_match() -> None:
    result = ConverterRegistry([TextConverter()]).convert(b"x", path="a.bin")
    assert result.status == CONVERSION_UNSUPPORTED
    assert result.error and ".bin" in result.error


def test_registry_turns_converter_crashes_into_a_failed_status() -> None:
    """A converter must never break the batch (spec §7 约束 3)."""
    result = ConverterRegistry([ExplodingConverter()]).convert(b"x", path="a.bin")
    assert result.status == CONVERSION_FAILED
    assert "boom" in (result.error or "")


def test_registry_stamps_the_converter_name() -> None:
    result = ConverterRegistry([TextConverter()]).convert(b"# hi", path="a.md")
    assert result.converter == "text"
    assert result.ok


def test_converter_statuses_match_the_schema_enum() -> None:
    from kb.models.document import CONVERSION_STATUSES

    assert set(CONVERSION_STATUSES) == {
        CONVERSION_OK,
        CONVERSION_FAILED,
        CONVERSION_NO_TEXT,
        CONVERSION_UNSUPPORTED,
    }


# ---------------------------------------------------------------------------
# text converter
# ---------------------------------------------------------------------------


def test_text_converter_passes_markdown_through_untouched() -> None:
    source = "# 标题\n\n正文 [[双链]] \n"
    result = TextConverter().convert(source.encode(), path="note.md", mime=None)
    assert result.markdown == source


def test_text_converter_decodes_gb18030() -> None:
    """A vault synced from a Windows editor is occasionally not UTF-8."""
    result = TextConverter().convert("中文笔记".encode("gb18030"), path="a.md", mime=None)
    assert result.markdown == "中文笔记"


def test_text_converter_marks_empty_files_no_text() -> None:
    assert TextConverter().convert(b"   \n", path="a.md", mime=None).status == CONVERSION_NO_TEXT


# ---------------------------------------------------------------------------
# pdf converter
# ---------------------------------------------------------------------------


def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_pdf_without_extractable_text_is_marked_no_text() -> None:
    """A scanned document: page structure is there, text is not (spec §7)."""
    result = PdfConverter().convert(_blank_pdf(), path="扫描件.pdf", mime=None)
    assert result.status == CONVERSION_NO_TEXT


def test_pdf_converter_reports_corrupt_input_as_failed() -> None:
    result = PdfConverter().convert(b"%PDF-1.4 not really", path="broken.pdf", mime=None)
    assert result.status == CONVERSION_FAILED


def test_pdf_page_numbers_are_dropped_from_text() -> None:
    assert normalise_page("正文内容\n\n12\n") == "正文内容"


def test_pdf_converter_claims_pdf_by_mime_when_the_extension_is_missing() -> None:
    assert PdfConverter().supports(path="noext", mime="application/pdf")


# ---------------------------------------------------------------------------
# xlsx converter
# ---------------------------------------------------------------------------


def _workbook(rows: list[tuple[int, list[object]]], sheet_title: str = "Sheet1") -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = sheet_title
    for row_number, values in rows:
        for column, value in enumerate(values, start=1):
            sheet.cell(row=row_number, column=column, value=value)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_xlsx_converter_emits_sheet_heading_marker_and_table() -> None:
    blob = _workbook([(1, ["客户", "金额"]), (2, ["甲", 100])])
    result = XlsxConverter().convert(blob, path="预算.xlsx", mime=None)

    assert result.ok
    assert "## Sheet1" in result.markdown
    assert '<!-- kb:sheet="Sheet1" row_start=1 -->' in result.markdown
    assert "| 客户 | 金额 |" in result.markdown
    assert "| 甲 | 100 |" in result.markdown


def test_xlsx_locator_uses_real_row_numbers_not_offsets() -> None:
    """Two blank rows above the data must not shift the locator to row 1."""
    blob = _workbook([(3, ["客户", "金额"]), (4, ["甲", 100])])
    result = XlsxConverter().convert(blob, path="预算.xlsx", mime=None)
    assert "row_start=3" in result.markdown


def test_xlsx_converter_skips_empty_sheets() -> None:
    blob = _workbook([], sheet_title="空的")
    result = XlsxConverter().convert(blob, path="空.xlsx", mime=None)
    assert result.status == CONVERSION_NO_TEXT


def test_xlsx_converter_reports_corrupt_input_as_failed() -> None:
    result = XlsxConverter().convert(b"not a workbook", path="broken.xlsx", mime=None)
    assert result.status == CONVERSION_FAILED


# ---------------------------------------------------------------------------
# markitdown converter
# ---------------------------------------------------------------------------


def test_markitdown_converts_csv() -> None:
    result = MarkItDownConverter().convert(b"a,b\n1,2\n", path="t.csv", mime=None)
    assert result.ok
    assert "1" in result.markdown


def test_markitdown_never_raises_on_corrupt_input() -> None:
    """A bad attachment must produce a status, never an exception (spec §7 约束 3).

    With markitdown's format extras installed this comes back ``failed``; without
    them markitdown falls back to the generic text path. Either way the contract
    the pipeline depends on holds: a status, no traceback. Corrupt-input
    *detection* is pinned for real by the PDF and XLSX tests above, whose
    dependencies are always present.
    """
    result = MarkItDownConverter().convert(b"PK\x03\x04 garbage", path="broken.docx", mime=None)
    assert result.status in CONVERSION_STATUSES


# ---------------------------------------------------------------------------
# conversion service: cap, cache, thread boundary
# ---------------------------------------------------------------------------


async def test_service_returns_cache_hits_without_converting() -> None:
    spy = SpyConverter()
    cache = FakeCache()
    cache.rows["abc"] = CachedConversion(status=CONVERSION_OK, converted="# cached")
    service = ConversionService(ConverterRegistry([spy]), cache, to_thread=sync_thread)

    result = await service.convert(b"ignored", path="a.md", content_sha="abc")

    assert result.status == CONVERSION_OK
    assert result.markdown == "# cached"
    assert result.converter == "cache"
    assert spy.calls == 0


async def test_service_writes_results_to_the_cache() -> None:
    cache = FakeCache()
    service = ConversionService(ConverterRegistry([SpyConverter()]), cache, to_thread=sync_thread)

    await service.convert(b"blob", path="a.md", content_sha="sha1")

    assert cache.puts == 1
    assert cache.rows["sha1"].converted == "ok"


async def test_service_marks_oversized_files_without_parsing_them() -> None:
    """spec §6: a 50MB attachment is skipped, never handed to a converter."""
    spy = SpyConverter()
    service = ConversionService(ConverterRegistry([spy]), max_bytes=10, to_thread=sync_thread)

    result = await service.convert(b"x" * 11, path="big.pdf", content_sha="sha")

    assert result.status == CONVERSION_UNSUPPORTED
    assert spy.calls == 0


async def test_service_never_raises_on_converter_failure() -> None:
    service = ConversionService(ConverterRegistry([ExplodingConverter()]), to_thread=sync_thread)
    result = await service.convert(b"blob", path="a.pdf", content_sha="sha")
    assert result.status == CONVERSION_FAILED


def test_service_names_the_converter_it_would_use() -> None:
    service = ConversionService()
    assert service.describe_target(path="a.pdf", mime=None) == "pdf"
    assert "none" in service.describe_target(path="a.bin", mime=None)


@pytest.mark.parametrize("extension,expected", [(".MD", "text"), (".Pdf", "pdf")])
def test_extension_matching_is_case_insensitive(extension: str, expected: str) -> None:
    assert default_registry().find(path=f"a{extension}", mime=None).name == expected  # type: ignore[union-attr]
