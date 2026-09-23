"""Markdown and plain-text files — the identity converter.

Most of an Obsidian vault arrives here. Nothing is rewritten: Obsidian syntax
(``[[wikilinks]]``, callouts, dataview blocks) passes through untouched, as
spec §6 requires. Frontmatter stays in the text too — the indexer is what reads
it and strips it for chunking.
"""

from __future__ import annotations

from kb.converter.base import CONVERSION_NO_TEXT, CONVERSION_OK, ConversionResult, suffix

TEXT_EXTENSIONS = (".md", ".markdown", ".mdx", ".txt", ".text")

# Tried in order. A vault synced from a Windows editor is occasionally GB18030
# rather than UTF-8, and mojibake would be indexed as real content.
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


class TextConverter:
    name = "text"

    def __init__(self, extensions: tuple[str, ...] = TEXT_EXTENSIONS) -> None:
        self.extensions = extensions

    def supports(self, *, path: str, mime: str | None) -> bool:
        if suffix(path) in self.extensions:
            return True
        return bool(mime) and mime.startswith("text/") and suffix(path) in {".md", ".markdown", ".txt", ""}

    def convert(self, blob: bytes, *, path: str, mime: str | None) -> ConversionResult:
        text = decode_text(blob)
        if not text.strip():
            return ConversionResult(status=CONVERSION_NO_TEXT, error="file is empty", converter=self.name)
        return ConversionResult(status=CONVERSION_OK, markdown=text, converter=self.name)


def decode_text(blob: bytes) -> str:
    """Decode bytes using the first encoding that works, then normalise newlines."""
    for encoding in ENCODINGS:
        try:
            text = blob.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return blob.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
