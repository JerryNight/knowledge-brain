"""MarkItDown-backed converter — DOCX / PPTX / HTML / CSV and friends (spec §7).

Registered last: it answers for formats that have no purpose-built converter, so
it can never take a file away from one that knows more about it (PDF page
numbers, spreadsheet row numbers).

MarkItDown is MIT-licensed, pure Python and needs no model files, which is why
it was chosen over heavier alternatives. Its PDF path is deliberately not used.
"""

from __future__ import annotations

from functools import cache
from io import BytesIO

from kb.converter.base import CONVERSION_FAILED, CONVERSION_NO_TEXT, CONVERSION_OK, ConversionResult, suffix

# Formats worth routing to markitdown. Deliberately not "everything markitdown
# supports": audio transcription and YouTube need network calls and models, and
# silently sending a user's attachment to a third party is not a conversion
# decision this service should make on its own.
MARKITDOWN_EXTENSIONS = (
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".html",
    ".htm",
    ".csv",
    ".tsv",
    ".epub",
    ".msg",
    ".json",
    ".xml",
    ".rss",
    ".ipynb",
)


@cache
def _markitdown():
    """One instance per process — building it registers every converter."""
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False)


class MarkItDownConverter:
    name = "markitdown"

    def __init__(self, extensions: tuple[str, ...] = MARKITDOWN_EXTENSIONS) -> None:
        self.extensions = extensions

    def supports(self, *, path: str, mime: str | None) -> bool:
        if suffix(path) in self.extensions:
            return True
        if not mime:
            return False
        return mime.startswith(("text/html", "text/csv", "application/vnd.openxml", "message/"))

    def convert(self, blob: bytes, *, path: str, mime: str | None) -> ConversionResult:
        extension = suffix(path)
        try:
            result = _markitdown().convert_stream(BytesIO(blob), file_extension=extension or None)
        except Exception as exc:  # noqa: BLE001 — a bad attachment must not stop the batch
            return ConversionResult(status=CONVERSION_FAILED, error=f"{type(exc).__name__}: {exc}", converter=self.name)

        text = (result.text_content or "").replace("\r\n", "\n").strip()
        if not text:
            return ConversionResult(status=CONVERSION_NO_TEXT, error="converted to empty text", converter=self.name)
        return ConversionResult(status=CONVERSION_OK, markdown=text, converter=self.name)
