"""Converter package — the public types plus registry construction.

``default_registry()`` lives in ``kb.converter.registry`` (and is re-exported
here) because ``ConversionService`` needs it, and having the package export it
too would close an import cycle.
"""

from __future__ import annotations

from kb.converter.base import (
    CONVERSION_FAILED,
    CONVERSION_NO_TEXT,
    CONVERSION_OK,
    CONVERSION_UNSUPPORTED,
    ConversionResult,
    Converter,
    ConverterRegistry,
    suffix,
)
from kb.converter.markitdown_conv import MarkItDownConverter
from kb.converter.pdf import PdfConverter
from kb.converter.plaintext import TextConverter
from kb.converter.registry import default_registry
from kb.converter.service import CachedConversion, ConversionCachePort, ConversionService, NullConversionCache
from kb.converter.xlsx import XlsxConverter

__all__ = [
    "CONVERSION_FAILED",
    "CONVERSION_NO_TEXT",
    "CONVERSION_OK",
    "CONVERSION_UNSUPPORTED",
    "CachedConversion",
    "ConversionCachePort",
    "ConversionResult",
    "ConversionService",
    "Converter",
    "ConverterRegistry",
    "MarkItDownConverter",
    "NullConversionCache",
    "PdfConverter",
    "TextConverter",
    "XlsxConverter",
    "default_registry",
    "suffix",
]
