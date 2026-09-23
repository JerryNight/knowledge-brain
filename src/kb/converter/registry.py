"""The production converter registry.

Its own module so that both the package's exports and ``ConversionService`` can
import it without a cycle — the service needs the default registry, and the
package needs the service.
"""

from __future__ import annotations

from functools import cache

from kb.converter.base import ConverterRegistry
from kb.converter.markitdown_conv import MarkItDownConverter
from kb.converter.pdf import PdfConverter
from kb.converter.plaintext import TextConverter
from kb.converter.xlsx import XlsxConverter


@cache
def default_registry() -> ConverterRegistry:
    """Build the registry once per process.

    Order is the contract: purpose-built converters first (they know about page
    and row coordinates), markitdown last as the generalist that only answers for
    formats nothing else claims.
    """
    return ConverterRegistry(
        [
            TextConverter(),
            PdfConverter(),
            XlsxConverter(),
            MarkItDownConverter(),
        ]
    )
