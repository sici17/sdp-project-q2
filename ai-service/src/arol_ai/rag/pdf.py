from pathlib import Path

import fitz

from arol_ai.rag.models import ExtractedPage


def extract_pdf_pages(pdf_path: Path) -> list[ExtractedPage]:
    pages: list[ExtractedPage] = []

    with fitz.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            tables = _extract_tables(page)
            pages.append(ExtractedPage(page=index, text=text, tables=tables))

    return pages


def _extract_tables(page) -> list[str]:
    find_tables = getattr(page, "find_tables", None)
    if find_tables is None:
        return []

    try:
        table_result = find_tables()
    except Exception:
        return []

    table_text: list[str] = []
    for table in table_result.tables:
        rows = table.extract()
        rendered_rows = [" | ".join(str(cell or "").strip() for cell in row) for row in rows]
        rendered = "\n".join(row for row in rendered_rows if row.strip())
        if rendered:
            table_text.append(rendered)

    return table_text
