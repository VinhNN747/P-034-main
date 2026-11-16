"""Turns image/file content parts into ``AttachmentAnalysis`` text surrogates.

Order of preference per attachment (cheapest first):
1. text files (txt/md/csv/json): decode in-process.
2. PDFs with a usable text layer: pypdf extraction, no model call.
3. images and scanned PDFs: OCR model (Gemini 3.8 Flash by default),
   only when the request's constraints allow sending data to that provider.

Every failure degrades to a ``skipped``/``failed`` analysis with an error
string; the request itself is never failed by this stage.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from src.backend_ai.gateway.core.circuit_breaker import CircuitBreaker, is_transient_error
from src.backend_ai.gateway.multimodal.attachments import (
    OCR_IMAGE_MIME_TYPES,
    TEXT_MIME_TYPES,
    RawAttachment,
    extract_attachments,
)
from src.backend_ai.gateway.multimodal.ocr import OcrClient
from src.backend_ai.gateway.multimodal.pdf import inspect_pdf
from src.shared.dtos.gateway import (
    AttachmentAnalysis,
    AttachmentKind,
    GatewayRequest,
    ModelUsage,
    RequestConstraints,
)


@dataclass
class AttachmentLimits:
    max_bytes: int = 20 * 1024 * 1024
    max_extracted_chars: int = 200_000
    pdf_max_ocr_pages: int = 50
    pdf_text_layer_min_chars_per_page: int = 200
    concurrency: int = 4
    cache_max_entries: int = 256


class AttachmentAnalyzer:
    def __init__(
        self,
        ocr_client: OcrClient | None = None,
        limits: AttachmentLimits | None = None,
        *,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.ocr_client = ocr_client
        self.limits = limits or AttachmentLimits()
        self.circuit_breaker = circuit_breaker
        self._cache: OrderedDict[str, AttachmentAnalysis] = OrderedDict()

    async def analyze(
        self, request: GatewayRequest, constraints: RequestConstraints | None = None
    ) -> list[AttachmentAnalysis]:
        raw = extract_attachments(request)
        if not raw:
            return []
        ocr_block_reason = self._ocr_block_reason(constraints or request.constraints)
        semaphore = asyncio.Semaphore(self.limits.concurrency)

        async def run(item: RawAttachment) -> AttachmentAnalysis:
            async with semaphore:
                return await self._analyze_one(item, ocr_block_reason)

        return list(await asyncio.gather(*(run(item) for item in raw)))

    def _ocr_block_reason(self, constraints: RequestConstraints) -> str | None:
        if self.ocr_client is None:
            return "OCR is not configured (GATEWAY_OCR_ENABLED / ocr_google connection)"
        provider = self.ocr_client.provider
        if not constraints.external_provider_allowed:
            return "external providers are disallowed by request constraints"
        if provider in constraints.blocked_providers:
            return f"OCR provider '{provider}' is blocked by request constraints"
        if constraints.allowed_providers is not None and provider not in constraints.allowed_providers:
            return f"OCR provider '{provider}' is not in allowed_providers"
        return None

    def _base(self, item: RawAttachment) -> AttachmentAnalysis:
        return AttachmentAnalysis(
            attachment_id=item.attachment_id,
            message_index=item.message_index,
            part_index=item.part_index,
            kind=item.kind,
            source=item.source,  # type: ignore[arg-type]
            mime_type=item.mime_type,
            filename=item.filename,
            size_bytes=len(item.data) if item.data is not None else None,
            sha256=item.sha256,
            status="skipped",
        )

    async def _analyze_one(self, item: RawAttachment, ocr_block_reason: str | None) -> AttachmentAnalysis:
        base = self._base(item)
        if item.error:
            return base.model_copy(update={"status": "failed", "error": item.error})
        if item.data is None:
            reason = (
                "remote image URLs are not fetched by the gateway; send a base64 data URL"
                if item.source == "url"
                else "file_id was not resolved for this request (upload it via /v1/files first)"
            )
            return base.model_copy(update={"error": reason})
        if len(item.data) > self.limits.max_bytes:
            return base.model_copy(
                update={"status": "failed", "error": f"attachment exceeds {self.limits.max_bytes} bytes"}
            )

        cache_key = f"{item.sha256}:{self.ocr_client.model if self.ocr_client else '-'}:{ocr_block_reason is None}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached.model_copy(update={**self._identity(base), "cache_hit": True, "usage": ModelUsage(), "usage_complete":True})

        try:
            if item.mime_type in TEXT_MIME_TYPES:
                result = self._decode_text(base, item.data)
            elif item.kind == AttachmentKind.PDF:
                result = await self._analyze_pdf(base, item.data, ocr_block_reason)
            elif item.kind == AttachmentKind.IMAGE:
                result = await self._analyze_image(base, item, ocr_block_reason)
            else:
                result = base.model_copy(update={"error": f"unsupported attachment type '{item.mime_type}'"})
        except Exception as exc:  # noqa: BLE001 - an attachment must never fail the request
            result = base.model_copy(update={"status": "failed", "error": f"{type(exc).__name__}: {exc}"})

        if result.status == "analyzed":
            self._cache_put(cache_key, result)
        return result

    @staticmethod
    def _identity(base: AttachmentAnalysis) -> dict:
        return {
            "attachment_id": base.attachment_id,
            "message_index": base.message_index,
            "part_index": base.part_index,
            "filename": base.filename,
            "source": base.source,
        }

    def _truncate(self, text: str) -> str:
        limit = self.limits.max_extracted_chars
        return text if len(text) <= limit else text[:limit] + "\n[...truncated]"

    def _decode_text(self, base: AttachmentAnalysis, data: bytes) -> AttachmentAnalysis:
        return base.model_copy(
            update={
                "status": "analyzed",
                "method": "text_decode",
                "content_category": "document",
                "extracted_text": self._truncate(data.decode("utf-8", errors="replace")),
            }
        )

    async def _analyze_pdf(
        self, base: AttachmentAnalysis, data: bytes, ocr_block_reason: str | None
    ) -> AttachmentAnalysis:
        inspection = inspect_pdf(data)
        base = base.model_copy(update={"page_count": inspection.page_count})
        if inspection.chars_per_page() >= self.limits.pdf_text_layer_min_chars_per_page:
            return base.model_copy(
                update={
                    "status": "analyzed",
                    "method": "pdf_text_layer",
                    "content_category": "document",
                    "extracted_text": self._truncate(inspection.text),
                }
            )
        # Scanned (or mostly-image) PDF: needs OCR.
        if ocr_block_reason is not None:
            return self._partial_text(base, inspection.text, f"scanned PDF needs OCR but {ocr_block_reason}")
        if inspection.page_count > self.limits.pdf_max_ocr_pages:
            return self._partial_text(
                base,
                inspection.text,
                f"scanned PDF has {inspection.page_count} pages; OCR limit is {self.limits.pdf_max_ocr_pages}",
            )
        return await self._ocr(base, data, "application/pdf", AttachmentKind.PDF)

    def _partial_text(self, base: AttachmentAnalysis, text: str, error: str) -> AttachmentAnalysis:
        if text.strip():
            return base.model_copy(
                update={
                    "status": "analyzed",
                    "method": "pdf_text_layer",
                    "content_category": "document",
                    "extracted_text": self._truncate(text),
                    "error": f"partial text layer only: {error}",
                }
            )
        return base.model_copy(update={"error": error})

    async def _analyze_image(
        self, base: AttachmentAnalysis, item: RawAttachment, ocr_block_reason: str | None
    ) -> AttachmentAnalysis:
        if item.mime_type not in OCR_IMAGE_MIME_TYPES:
            return base.model_copy(update={"error": f"unsupported image type '{item.mime_type}'"})
        if ocr_block_reason is not None:
            return base.model_copy(update={"error": ocr_block_reason})
        return await self._ocr(base, item.data, item.mime_type, AttachmentKind.IMAGE)  # type: ignore[arg-type]

    async def _ocr(self, base: AttachmentAnalysis, data: bytes, mime_type: str, kind: AttachmentKind) -> AttachmentAnalysis:
        assert self.ocr_client is not None
        key = CircuitBreaker.key(self.ocr_client.model, self.ocr_client.provider)
        if self.circuit_breaker is not None and not self.circuit_breaker.allow(key):
            return base.model_copy(update={"error": "OCR model temporarily unavailable (circuit open)"})
        try:
            result = await self.ocr_client.ocr(data, mime_type, kind=kind)
        except Exception as exc:
            if self.circuit_breaker is not None:
                if is_transient_error(exc):
                    self.circuit_breaker.record_failure(key, error=f"{type(exc).__name__}: {exc}"[:300])
                else:
                    self.circuit_breaker.release_probe(key)
            raise
        if self.circuit_breaker is not None:
            self.circuit_breaker.record_success(key)
        return base.model_copy(
            update={
                "status": "analyzed",
                "method": "ocr",
                "content_category": result.output.content_category,
                "description": result.output.description,
                "extracted_text": self._truncate(result.output.text),
                "language": result.output.language,
                "model": result.model,
                "usage": result.usage,
                "usage_complete":result.usage_complete,
            }
        )

    def _cache_put(self, key: str, result: AttachmentAnalysis) -> None:
        if self.limits.cache_max_entries <= 0:
            return
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self.limits.cache_max_entries:
            self._cache.popitem(last=False)
