"""Pulls image/file content parts out of an OpenAI-compatible request and
decodes them into bytes the analyzer can inspect.

Only inline data is decoded. Remote ``https://`` image URLs and ``file_id``
references are reported (``source="url"``/``"file_id"``) but never fetched:
fetching arbitrary user URLs from the gateway is an SSRF vector, and there is
no Files API store yet to resolve ``file_id`` against.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
from dataclasses import dataclass

from src.shared.dtos.gateway import AttachmentKind, FileContentPart, GatewayRequest, ImageContentPart
from src.shared.services.media_redaction import redact_inline_media  # noqa: F401 - re-exported

# Formats Gemini accepts inline for vision/OCR.
OCR_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"})
TEXT_MIME_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})


@dataclass(frozen=True)
class ResolvedFile:
    """Bytes of a Files API upload, loaded for one request by the API layer
    (the pipeline itself has no database access)."""

    file_id: str
    filename: str
    mime_type: str
    data: bytes


@dataclass
class RawAttachment:
    attachment_id: str
    message_index: int
    part_index: int
    source: str  # "data" | "url" | "file_id"
    kind: AttachmentKind
    mime_type: str | None = None
    filename: str | None = None
    data: bytes | None = None
    reference: str | None = None  # remote URL or file_id when not inline
    error: str | None = None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.data).hexdigest() if self.data is not None else None


def sniff_mime_type(data: bytes) -> str | None:
    """Magic-byte detection; the declared data-URL mime type is not trusted."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc"):
        return "image/heic"
    if data[4:12] in (b"ftypmif1", b"ftypmsf1"):
        return "image/heif"
    return None


def kind_for_mime(mime_type: str | None) -> AttachmentKind:
    if mime_type == "application/pdf":
        return AttachmentKind.PDF
    if mime_type and mime_type.startswith("image/"):
        return AttachmentKind.IMAGE
    return AttachmentKind.OTHER


def decode_data_url(value: str) -> tuple[str | None, bytes]:
    """Decodes ``data:<mime>;base64,<payload>`` or bare base64.

    Raises ValueError on anything that is not valid base64.
    """
    declared: str | None = None
    payload = value.strip()
    if payload.startswith("data:"):
        header, sep, payload = payload.partition(",")
        if not sep or ";base64" not in header:
            raise ValueError("data URL must be base64-encoded")
        declared = header[5:].split(";", 1)[0].strip().lower() or None
    try:
        return declared, base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 payload: {exc}") from exc


def _resolve_mime(declared: str | None, data: bytes, filename: str | None) -> str | None:
    sniffed = sniff_mime_type(data)
    if sniffed:
        return sniffed
    if declared:
        return declared
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed
    return None


def _from_image_part(part: ImageContentPart, message_index: int, part_index: int) -> RawAttachment:
    attachment_id = f"m{message_index}p{part_index}"
    url = part.image_url.url
    if not url.startswith("data:"):
        return RawAttachment(
            attachment_id=attachment_id,
            message_index=message_index,
            part_index=part_index,
            source="url",
            kind=AttachmentKind.IMAGE,
            reference=url,
        )
    try:
        declared, data = decode_data_url(url)
    except ValueError as exc:
        return RawAttachment(
            attachment_id=attachment_id,
            message_index=message_index,
            part_index=part_index,
            source="data",
            kind=AttachmentKind.IMAGE,
            error=str(exc),
        )
    mime_type = _resolve_mime(declared, data, None)
    return RawAttachment(
        attachment_id=attachment_id,
        message_index=message_index,
        part_index=part_index,
        source="data",
        kind=kind_for_mime(mime_type),
        mime_type=mime_type,
        data=data,
    )


def _from_file_part(
    part: FileContentPart,
    message_index: int,
    part_index: int,
    resolved_files: dict[str, ResolvedFile] | None = None,
) -> RawAttachment:
    attachment_id = f"m{message_index}p{part_index}"
    file = part.file
    resolved = (resolved_files or {}).get(file.file_id or "")
    if not file.file_data and resolved is not None:
        mime_type = _resolve_mime(resolved.mime_type, resolved.data, resolved.filename)
        return RawAttachment(
            attachment_id=attachment_id,
            message_index=message_index,
            part_index=part_index,
            source="file_id",
            kind=kind_for_mime(mime_type),
            mime_type=mime_type,
            filename=file.filename or resolved.filename,
            data=resolved.data,
            reference=file.file_id,
        )
    if not file.file_data:
        return RawAttachment(
            attachment_id=attachment_id,
            message_index=message_index,
            part_index=part_index,
            source="file_id",
            kind=kind_for_mime(mimetypes.guess_type(file.filename or "")[0]),
            filename=file.filename,
            reference=file.file_id,
        )
    try:
        declared, data = decode_data_url(file.file_data)
    except ValueError as exc:
        return RawAttachment(
            attachment_id=attachment_id,
            message_index=message_index,
            part_index=part_index,
            source="data",
            kind=AttachmentKind.OTHER,
            filename=file.filename,
            error=str(exc),
        )
    mime_type = _resolve_mime(declared, data, file.filename)
    return RawAttachment(
        attachment_id=attachment_id,
        message_index=message_index,
        part_index=part_index,
        source="data",
        kind=kind_for_mime(mime_type),
        mime_type=mime_type,
        filename=file.filename,
        data=data,
    )


def extract_attachments(request: GatewayRequest) -> list[RawAttachment]:
    attachments: list[RawAttachment] = []
    for message_index, message in enumerate(request.messages or []):
        if message.content is None or isinstance(message.content, str):
            continue
        for part_index, part in enumerate(message.content):
            if isinstance(part, ImageContentPart):
                attachments.append(_from_image_part(part, message_index, part_index))
            elif isinstance(part, FileContentPart):
                attachments.append(_from_file_part(part, message_index, part_index, request.resolved_files()))
    return attachments


def referenced_file_ids(request: GatewayRequest) -> set[str]:
    """``file_id``s a request points at (parts that carry no inline data)."""
    return {
        part.file.file_id
        for message in request.messages or []
        if message.content is not None and not isinstance(message.content, str)
        for part in message.content
        if isinstance(part, FileContentPart) and part.file.file_id and not part.file.file_data
    }
