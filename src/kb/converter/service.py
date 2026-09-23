"""Conversion service — the layer that knows about size limits and caching.

Three concerns live here, all of them policy rather than format knowledge:

* **Size cap** (spec §6): a 50MB attachment is marked ``unsupported`` and skipped
  instead of occupying a worker. The cap is checked before any parsing, so a
  huge file costs nothing.
* **Content-hash cache** (spec §7 约束 2): identical bytes are converted once.
  PDF conversion is seconds-to-tens-of-seconds, and a full rebuild must not pay
  for it again.
* **Thread boundary**: converters are synchronous and CPU-bound. They run via
  ``asyncio.to_thread`` so one slow spreadsheet cannot stall the event loop that
  is also talking to Postgres.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from kb.converter.base import (
    CONVERSION_UNSUPPORTED,
    ConversionResult,
    ConverterRegistry,
)
from kb.converter.base import suffix as file_suffix
from kb.converter.registry import default_registry

# spec §6: files over 50MB are skipped, not retried or partially processed.
MAX_BYTES_DEFAULT = 52_428_800


@dataclass(frozen=True, slots=True)
class CachedConversion:
    """A row of ``conversion_cache``."""

    status: str
    converted: str | None


class ConversionCachePort(Protocol):
    """The cache as the service sees it — no SQL, no session, no ORM."""

    async def get(self, content_sha: str) -> CachedConversion | None: ...

    async def put(self, content_sha: str, *, status: str, converted: str | None) -> None: ...


class NullConversionCache:
    """Cache that never hits. Used when no database is available (CLI, tests)."""

    async def get(self, content_sha: str) -> CachedConversion | None:
        return None

    async def put(self, content_sha: str, *, status: str, converted: str | None) -> None:
        return None


class ConversionService:
    def __init__(
        self,
        registry: ConverterRegistry | None = None,
        cache: ConversionCachePort | None = None,
        *,
        max_bytes: int = MAX_BYTES_DEFAULT,
        to_thread: Callable[..., object] = asyncio.to_thread,
    ) -> None:
        self._registry = registry if registry is not None else default_registry()
        self._cache = cache if cache is not None else NullConversionCache()
        self._max_bytes = max_bytes
        self._to_thread = to_thread

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @classmethod
    def from_settings(cls, registry: ConverterRegistry | None = None, cache: ConversionCachePort | None = None):
        """Build with the configured size cap. Imported lazily so this module
        stays importable without environment variables."""
        from kb.config import get_settings

        return cls(registry, cache, max_bytes=get_settings().max_file_size_bytes)

    async def convert(
        self,
        blob: bytes,
        *,
        path: str,
        content_sha: str,
        mime: str | None = None,
    ) -> ConversionResult:
        if len(blob) > self._max_bytes:
            return ConversionResult(
                status=CONVERSION_UNSUPPORTED,
                error=f"file is {len(blob)} bytes, over the {self._max_bytes} byte limit",
                converter="",
            )

        cached = await self._cache.get(content_sha)
        if cached is not None:
            return ConversionResult(
                status=cached.status,
                markdown=cached.converted or "",
                converter="cache",
            )

        result = await self._to_thread(self._registry.convert, blob, path=path, mime=mime)
        await self._cache.put(
            content_sha,
            status=result.status,
            converted=result.markdown if result.markdown else None,
        )
        return result

    def describe_target(self, *, path: str, mime: str | None) -> str:
        """Name the converter that will handle a path, for logging and status output."""
        converter = self._registry.find(path=path, mime=mime)
        return converter.name if converter else f"none ({file_suffix(path) or path})"
