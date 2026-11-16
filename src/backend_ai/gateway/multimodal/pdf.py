"""Deterministic, free PDF inspection: page count and the embedded text
layer. A PDF whose text layer is dense enough never needs OCR."""

from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass
class PdfInspection:
    page_count: int
    text: str
    text_chars: int
    inspected_pages: int

    def chars_per_page(self) -> float:
        return self.text_chars / self.inspected_pages if self.inspected_pages else 0.0


def inspect_pdf(data: bytes, *, max_pages: int | None = None) -> PdfInspection:
    """Raises ValueError for unreadable/encrypted PDFs."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        pages = reader.pages
        page_count = len(pages)
        limit = page_count if max_pages is None else min(page_count, max_pages)
        chunks = []
        text_chars = 0
        for index in range(limit):
            page_text = (pages[index].extract_text() or "").strip()
            text_chars += len(page_text)
            if page_text:
                chunks.append(f"--- page {index + 1} ---\n{page_text}")
    except PdfReadError as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc
    return PdfInspection(
        page_count=page_count,
        text="\n\n".join(chunks),
        text_chars=text_chars,
        inspected_pages=limit,
    )
