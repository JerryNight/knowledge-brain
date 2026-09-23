"""Spreadsheets → markdown tables, sheet by sheet.

Read with openpyxl rather than markitdown because the locator needs real cell
coordinates: "``预算.xlsx`` Sheet1 第 40 行" is only possible if the converter
knows which spreadsheet row each markdown row came from.

The marker is emitted immediately before the table, so the chunker can (a) tag
the whole table with its sheet, and (b) keep row numbers honest when it splits a
wide table into groups with a repeated header (spec §8).
"""

from __future__ import annotations

from io import BytesIO

from kb.converter.base import CONVERSION_FAILED, CONVERSION_NO_TEXT, CONVERSION_OK, ConversionResult, suffix
from kb.indexer.locator import marker
from kb.indexer.tables import render_markdown_table

SPREADSHEET_EXTENSIONS = (".xlsx", ".xlsm")

# How many columns of an empty tail to tolerate before trimming.
MAX_COLUMNS = 256


class XlsxConverter:
    name = "xlsx"

    def supports(self, *, path: str, mime: str | None) -> bool:
        if suffix(path) in SPREADSHEET_EXTENSIONS:
            return True
        return mime in {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel.sheet.macroenabled.12",
        }

    def convert(self, blob: bytes, *, path: str, mime: str | None) -> ConversionResult:
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(BytesIO(blob), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 — corrupt files are expected input
            return ConversionResult(status=CONVERSION_FAILED, error=f"{type(exc).__name__}: {exc}", converter=self.name)

        blocks: list[str] = []
        try:
            blocks = [block for sheet in workbook.worksheets if (block := render_sheet(sheet))]
        finally:
            workbook.close()

        if not blocks:
            return ConversionResult(status=CONVERSION_NO_TEXT, error="no non-empty cells", converter=self.name)
        return ConversionResult(status=CONVERSION_OK, markdown="\n\n".join(blocks), converter=self.name)


def render_sheet(sheet) -> str:
    """Render one worksheet as a heading, a locator marker and a table."""
    rows = _non_empty_rows(sheet)
    if not rows:
        return ""

    header = rows[0][1]
    body = [values for _, values in rows[1:]]
    table = render_markdown_table(header, body)
    return f"## {sheet.title}\n\n{marker(sheet=sheet.title, row_start=rows[0][0])}\n\n{table}"


def _non_empty_rows(sheet) -> list[tuple[int, list[object]]]:
    """Rows that carry data, as ``(spreadsheet_row_number, values)``.

    Row numbers are the real ones, not offsets: skipping a leading blank band
    must not shift every locator in the sheet by a few lines.
    """
    collected: list[tuple[int, list[object]]] = []
    for index, values in enumerate(sheet.iter_rows(values_only=True), start=1):
        cells = list(values)[:MAX_COLUMNS]
        if all(_is_empty(cell) for cell in cells):
            continue
        collected.append((index, cells))
    if not collected:
        return []

    # Trim the ragged empty tail of every row to a common width.
    width = 0
    for _, cells in collected:
        last = _last_filled(cells)
        width = max(width, last + 1)
    return [(number, cells[:width]) for number, cells in collected]


def _last_filled(cells: list[object]) -> int:
    for index in range(len(cells) - 1, -1, -1):
        if not _is_empty(cells[index]):
            return index
    return -1


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()
