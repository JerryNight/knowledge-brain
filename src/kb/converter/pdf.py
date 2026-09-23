"""PDF → markdown, page by page.

pypdf rather than markitdown here, for one reason: page numbers. Spec §7 约束 4
requires a hit to say "``报告.pdf``, page 12", and page granularity is only
available if text is extracted one page at a time. MarkItDown produces a single
undifferentiated blob.

Layout quality on two-column or table-heavy PDFs is mediocre — that is the
accepted cost of the PDF-parsing approach in general (spec §12), not something
this module tries to fix.

Scanned documents come out with no extractable text and are marked ``no_text``
(spec §7). No OCR in phase 1, but the status is explicit, so re-running these
files is a well-defined operation once OCR exists.
"""

from __future__ import annotations

import re
from io import BytesIO

from kb.converter.base import CONVERSION_FAILED, CONVERSION_NO_TEXT, CONVERSION_OK, ConversionResult
from kb.indexer.locator import marker

PDF_EXTENSIONS = (".pdf",)

# Lines that are pure page furniture. Dropping them keeps repeated headers and
# footers from becoming the "content" of a chunk.
_PAGE_FURNITURE = re.compile(r"^\s*(?:page\s+)?\d+\s*(?:/\s*\d+)?\s*$", re.IGNORECASE)


class PdfConverter:
    name = "pdf"

    def supports(self, *, path: str, mime: str | None) -> bool:
        from kb.converter.base import suffix

        return suffix(path) in PDF_EXTENSIONS or mime == "application/pdf"

    def convert(self, blob: bytes, *, path: str, mime: str | None) -> ConversionResult:
        from pypdf import PdfReader

        try:
            reader = PdfReader(BytesIO(blob))
            if reader.is_encrypted:
                # An empty-password PDF is common; anything else is unreadable
                # without a credential we do not have.
                try:
                    reader.decrypt("")
                except Exception:  # noqa: BLE001
                    return ConversionResult(status=CONVERSION_FAILED, error="encrypted PDF", converter=self.name)
            pages = [normalise_page(page.extract_text() or "") for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 — corrupt files are expected input
            return ConversionResult(status=CONVERSION_FAILED, error=f"{type(exc).__name__}: {exc}", converter=self.name)

        non_empty = [(number, text) for number, text in enumerate(pages, start=1) if text]
        if not non_empty:
            return ConversionResult(
                status=CONVERSION_NO_TEXT,
                error="no extractable text (scanned document?)",
                converter=self.name,
            )

        blocks = [f"{marker(page=number)}\n\n{text}" for number, text in non_empty]
        return ConversionResult(status=CONVERSION_OK, markdown="\n\n".join(blocks), converter=self.name)


def normalise_page(text: str) -> str:
    """Clean a page's extracted text without reflowing it."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept = [line.rstrip() for line in text.split("\n") if not _PAGE_FURNITURE.match(line)]
    return "\n".join(kept).strip()
