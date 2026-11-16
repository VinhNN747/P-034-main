"""The public profile refuses media it cannot actually analyze."""

from src.backend_ai.gateway.multimodal.attachments import extract_attachments
from src.shared.dtos.gateway import AttachmentKind
from src.shared.dtos.runtime_api import RuntimeAPIError


def preflight_media(request, analyzer):
    for item in extract_attachments(request):
        if item.data is not None and len(item.data) > analyzer.limits.max_bytes:
            raise RuntimeAPIError(413, "ATTACHMENT_TOO_LARGE", "Attachment exceeds the analysis size limit.",
                                  details={"param": "messages"})
        if request.constraints.allow_partial_response:
            continue
        if item.source == "url":
            raise RuntimeAPIError(400, "UNSUPPORTED_MEDIA", "Remote image URLs are not supported; send inline data.",
                                  details={"param": "messages"})
        if item.error:
            raise RuntimeAPIError(400, "UNSUPPORTED_MEDIA", "The attachment data is invalid.",
                                  details={"param": "messages"})
        if item.data is not None and len(item.data) > analyzer.limits.max_bytes:
            raise RuntimeAPIError(413, "ATTACHMENT_TOO_LARGE", "Attachment exceeds the analysis size limit.",
                                  details={"param": "messages"})
        if item.kind == AttachmentKind.IMAGE and analyzer._ocr_block_reason(request.constraints):
            raise RuntimeAPIError(400, "ATTACHMENT_ANALYSIS_FAILED", "Image analysis is unavailable for this request.",
                                  details={"param": "messages"})
