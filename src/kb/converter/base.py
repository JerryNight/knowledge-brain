"""Converter boundary: binary blob + format in, markdown out (spec §7).

A converter knows file formats and nothing else. It does not know whether the
bytes came from git or from an upload, and it does not know that embeddings
exist. That boundary is what lets a single format be swapped — register another
implementation and only this package changes.

Conversion happens at index time, never at query time (spec §7 约束 1): a
retrieval path that converted a PDF on demand would be unusable, and the
converted text could not have made it into the full-text index.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kb.models.document import CONVERSION_STATUSES

# Status values. Kept in sync with the schema-facing tuple by a unit test.
CONVERSION_OK = "ok"
CONVERSION_FAILED = "failed"
CONVERSION_NO_TEXT = "no_text"
CONVERSION_UNSUPPORTED = "unsupported"

# The two states that leave a document *in* the knowledge base but out of the
# index (spec §7). Defined once because the CLI report and the status endpoint
# have to describe the same set: two literals would drift apart silently, and
# this list is the only thing that tells "converted badly" apart from "never
# uploaded". `unsupported` is deliberately absent -- it is a dropped format, not
# a failed conversion.
UNSEARCHABLE_STATUSES = (CONVERSION_FAILED, CONVERSION_NO_TEXT)


@dataclass(slots=True)
class ConversionResult:
    """Outcome of one conversion attempt.

    Failure is a value, not an exception (spec §7 约束 3). A single unreadable
    attachment must mark itself and let the rest of the batch through.
    """

    status: str
    markdown: str = ""
    error: str | None = None
    converter: str = ""

    @property
    def ok(self) -> bool:
        return self.status == CONVERSION_OK

    @property
    def indexable(self) -> bool:
        return self.status == CONVERSION_OK and bool(self.markdown.strip())


@runtime_checkable
class Converter(Protocol):
    """One file format (or family) to markdown."""

    name: str

    def supports(self, *, path: str, mime: str | None) -> bool:
        """Whether this converter handles ``path``.

        ``path`` is authoritative: uploads frequently arrive with a generic MIME
        type, and a vault is mostly extension-tagged files.
        """
        ...

    def convert(self, blob: bytes, *, path: str, mime: str | None) -> ConversionResult:
        """Convert ``blob``. Implementations must not raise for bad input."""
        ...


def suffix(path: str) -> str:
    """Lowercase file extension including the dot, or ``""``."""
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot else ""


class ConverterRegistry:
    """First-match-wins registry.

    Order is explicit and stable: specific formats are registered before the
    general-purpose fallback, so adding markitdown support for a new extension
    cannot silently steal a file from a purpose-built converter.
    """

    def __init__(self, converters: Sequence[Converter] = ()) -> None:
        self._converters: list[Converter] = list(converters)

    def register(self, converter: Converter) -> None:
        self._converters.append(converter)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(converter.name for converter in self._converters)

    def find(self, *, path: str, mime: str | None) -> Converter | None:
        for converter in self._converters:
            if converter.supports(path=path, mime=mime):
                return converter
        return None

    def convert(self, blob: bytes, *, path: str, mime: str | None = None) -> ConversionResult:
        """Convert with the first matching converter, or report ``unsupported``."""
        converter = self.find(path=path, mime=mime)
        if converter is None:
            return ConversionResult(
                status=CONVERSION_UNSUPPORTED,
                error=f"no converter registered for {suffix(path) or path!r}",
                converter="",
            )
        try:
            result = converter.convert(blob, path=path, mime=mime)
        except Exception as exc:  # noqa: BLE001 — a converter must never break the batch
            return ConversionResult(
                status=CONVERSION_FAILED,
                error=f"{type(exc).__name__}: {exc}",
                converter=converter.name,
            )
        if result.converter == "":
            result.converter = converter.name
        return result


assert set(CONVERSION_STATUSES) == {  # pragma: no cover - import-time invariant
    CONVERSION_OK,
    CONVERSION_FAILED,
    CONVERSION_NO_TEXT,
    CONVERSION_UNSUPPORTED,
}, "converter statuses drifted from kb.models.document.CONVERSION_STATUSES"
