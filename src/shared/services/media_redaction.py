"""Strip inline base64 images/files from chat messages before they are
persisted or echoed back. Shared because AI execution writes request history
and management reads it; it depends only on the shared chat contract."""

from __future__ import annotations

import hashlib

from src.shared.dtos.gateway import ChatMessage, FileContentPart, ImageContentPart


def redact_inline_media(message: ChatMessage) -> dict:
    """JSON dump of a message with inline base64 payloads replaced by a
    sha256/size placeholder, so persisted request logs stay small and do not
    retain user files. Remote URLs and file_ids are kept as-is."""
    dumped = message.model_dump(mode="json")
    if message.content is None or isinstance(message.content, str):
        return dumped
    for part, raw in zip(dumped["content"], message.content, strict=True):
        payload: str | None = None
        if isinstance(raw, ImageContentPart) and raw.image_url.url.startswith("data:"):
            payload = raw.image_url.url
        elif isinstance(raw, FileContentPart) and raw.file.file_data:
            payload = raw.file.file_data
        if payload is None:
            continue
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        placeholder = f"[inline data omitted: {len(payload)} chars, sha256={digest}]"
        if isinstance(raw, ImageContentPart):
            part["image_url"]["url"] = placeholder
        else:
            part["file"]["file_data"] = placeholder
    return dumped
