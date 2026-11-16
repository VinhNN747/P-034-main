from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.repositories.runtime_operations import RuntimeOperationRepository
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.openai_operation_fingerprint import OperationFingerprint
from src.shared.dtos.runtime_api import RuntimeRequestInput
from src.shared.models.core import Application
from src.shared.models.runtime import RuntimeOperationRecord


class InvalidIdempotencyKeyError(ValueError):
    pass


class RuntimeStorageError(Exception):
    pass


@dataclass(frozen=True)
class ClaimResult:
    kind: str
    operation: RuntimeOperationRecord | None = None
    retry_after: int | None = None


class RuntimeOperationService:
    NORMALIZATION_VERSION = 1
    DEFAULT_REQUESTS_PER_MINUTE = 60
    DEFAULT_MAX_CONCURRENT_REQUESTS = 4
    DEFAULT_DEADLINE_SECONDS = 120
    EXPIRY_GRACE_SECONDS = 30

    def __init__(
        self,
        session: AsyncSession,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    ):
        self.session = session
        self.repository = RuntimeOperationRepository(session)
        self.requests_per_minute = requests_per_minute
        self.max_concurrent_requests = max_concurrent_requests
        self.deadline_seconds = deadline_seconds

    @staticmethod
    def validate_idempotency_key(value: str) -> str:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
        ):
            raise InvalidIdempotencyKeyError("Idempotency-Key must be 1..128 visible ASCII characters")
        return value

    @classmethod
    def normalize_payload(cls, payload: RuntimeRequestInput | dict[str, Any]) -> dict[str, Any]:
        request = RuntimeRequestInput.model_validate(payload)
        constraints = request.constraints.model_dump(mode="json", exclude_unset=True, exclude_none=True)
        for name in ("allowed_models", "blocked_models", "allowed_providers", "blocked_providers"):
            if name in constraints:
                constraints[name] = sorted(set(constraints[name]))
        if constraints.get("allow_partial_response") is False:
            constraints.pop("allow_partial_response")
        if constraints.get("external_provider_allowed") is True:
            constraints.pop("external_provider_allowed")
        if constraints.get("output_format") == "text":
            constraints.pop("output_format")
        normalized: dict[str, Any] = {
            "prompt": request.prompt,
            "context": request.context or {},
            "constraints": constraints,
            "metadata": request.metadata or {},
        }
        # Only added when present so prompt-only fingerprints stay unchanged.
        if request.messages is not None:
            normalized["messages"] = [message.model_dump(mode="json") for message in request.messages]
        return normalized

    @classmethod
    def fingerprint(cls, payload: RuntimeRequestInput | dict[str, Any]) -> str:
        normalized = cls.normalize_payload(payload)
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def key_hash(cls, idempotency_key: str) -> str:
        key = cls.validate_idempotency_key(idempotency_key)
        return hashlib.sha256(key.encode("ascii")).hexdigest()

    async def lookup(
        self,
        principal: ApplicationPrincipal,
        idempotency_key: str,
        payload: RuntimeRequestInput | dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ClaimResult | None:
        """Resolve an existing receipt before current config preflight runs."""
        fingerprint = OperationFingerprint(self.fingerprint(payload), self.NORMALIZATION_VERSION)
        return await self.lookup_normalized(
            principal,
            idempotency_key,
            fingerprint,
            now=now,
        )

    async def lookup_normalized(
        self,
        principal: ApplicationPrincipal,
        idempotency_key: str,
        fingerprint: OperationFingerprint,
        *,
        now: datetime | None = None,
    ) -> ClaimResult | None:
        """Resolve a receipt by its already validated, versioned fingerprint."""
        current_time = now or datetime.now(UTC)
        key_hash = self.key_hash(idempotency_key)
        try:
            await self._begin_serialized_transaction(principal.application_id)
            existing = await self.repository.by_idempotency_key(principal.application_id, key_hash)
            if existing is None:
                await self.session.rollback()
                return None
            if (
                existing.payload_hash != fingerprint.payload_hash
                or existing.normalization_version != fingerprint.normalization_version
            ):
                await self.session.commit()
                return ClaimResult("conflict", existing)
            if existing.status == "running" and self._aware(existing.expires_at) <= current_time:
                await self.repository.reconcile_expired(principal.application_id, current_time)
                existing.status = "indeterminate"
                existing.finished_at = current_time
                await self.session.commit()
                return ClaimResult("unknown", existing)
            await self.session.commit()
            if existing.status in ("completed", "partial"):
                return ClaimResult("replay", existing)
            if existing.status == "failed":
                return ClaimResult("terminal_replay", existing)
            if existing.status == "indeterminate":
                return ClaimResult("unknown", existing)
            return ClaimResult("running", existing, retry_after=1)
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise RuntimeStorageError("Runtime operation storage is unavailable") from exc

    async def claim(
        self,
        principal: ApplicationPrincipal,
        idempotency_key: str,
        payload: RuntimeRequestInput | dict[str, Any],
        *,
        config_version: int,
        config_snapshot: dict[str, Any],
        execution_mode: str = "offline",
        now: datetime | None = None,
    ) -> ClaimResult:
        fingerprint = OperationFingerprint(self.fingerprint(payload), self.NORMALIZATION_VERSION)
        return await self.claim_normalized(
            principal,
            idempotency_key,
            fingerprint,
            config_version=config_version,
            config_snapshot=config_snapshot,
            execution_mode=execution_mode,
            now=now,
        )

    async def claim_normalized(
        self,
        principal: ApplicationPrincipal,
        idempotency_key: str,
        fingerprint: OperationFingerprint,
        *,
        config_version: int,
        config_snapshot: dict[str, Any],
        execution_mode: str,
        now: datetime | None = None,
    ) -> ClaimResult:
        """Claim one application-scoped operation using a versioned hash."""
        current_time = now or datetime.now(UTC)
        key_hash = self.key_hash(idempotency_key)
        try:
            await self._begin_serialized_transaction(principal.application_id)
            existing = await self.repository.by_idempotency_key(principal.application_id, key_hash)
            if existing is not None:
                if (
                    existing.payload_hash != fingerprint.payload_hash
                    or existing.normalization_version != fingerprint.normalization_version
                ):
                    await self.session.commit()
                    return ClaimResult("conflict", existing)
                if existing.status == "running" and self._aware(existing.expires_at) <= current_time:
                    await self.repository.reconcile_expired(principal.application_id, current_time)
                    existing.status = "indeterminate"
                    existing.finished_at = current_time
                    await self.session.commit()
                    return ClaimResult("unknown", existing)
                await self.session.commit()
                if existing.status in ("completed", "partial"):
                    return ClaimResult("replay", existing)
                if existing.status == "failed":
                    return ClaimResult("terminal_replay", existing)
                if existing.status == "indeterminate":
                    return ClaimResult("unknown", existing)
                return ClaimResult("running", existing, retry_after=1)

            await self.repository.reconcile_expired(principal.application_id, current_time)
            if await self.repository.count_since(principal.application_id, current_time - timedelta(minutes=1)) >= max(
                0, self.requests_per_minute
            ):
                await self.session.rollback()
                return ClaimResult("rate_limited", retry_after=1)
            if await self.repository.count_running(principal.application_id, current_time) >= max(
                0, self.max_concurrent_requests
            ):
                await self.session.rollback()
                return ClaimResult("concurrency_limited", retry_after=1)

            operation = RuntimeOperationRecord(
                request_id=str(uuid4()),
                application_id=principal.application_id,
                application_api_key_id=principal.key_id,
                idempotency_key_hash=key_hash,
                payload_hash=fingerprint.payload_hash,
                normalization_version=fingerprint.normalization_version,
                status="running",
                execution_mode=execution_mode,
                config_version=config_version,
                config_snapshot_json=config_snapshot,
                started_at=current_time,
                expires_at=current_time
                + timedelta(seconds=self.deadline_seconds + self.EXPIRY_GRACE_SECONDS),
            )
            await self.repository.add(operation)
            await self.session.commit()
            return ClaimResult("owner", operation)
        except IntegrityError:
            await self.session.rollback()
            # Application-level serialization should make this rare; the unique
            # index remains the final guard against duplicate execution owners.
            try:
                existing = await self.repository.by_idempotency_key(principal.application_id, key_hash)
                if existing is None:
                    raise RuntimeStorageError("Operation claim could not be resolved")
                if (
                    existing.payload_hash != fingerprint.payload_hash
                    or existing.normalization_version != fingerprint.normalization_version
                ):
                    await self.session.commit()
                    return ClaimResult("conflict", existing)
                if existing.status in ("completed", "partial"):
                    await self.session.commit()
                    return ClaimResult("replay", existing)
                if existing.status == "failed":
                    await self.session.commit()
                    return ClaimResult("terminal_replay", existing)
                if existing.status == "indeterminate":
                    await self.session.commit()
                    return ClaimResult("unknown", existing)
                await self.session.commit()
                return ClaimResult("running", existing, retry_after=1)
            except SQLAlchemyError as exc:
                await self.session.rollback()
                raise RuntimeStorageError("Unable to resolve operation claim") from exc
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise RuntimeStorageError("Runtime operation storage is unavailable") from exc

    async def finalize(
        self,
        request_id: str,
        *,
        status: str,
        public_result: dict[str, Any],
        http_status: int,
        http_body: dict[str, Any],
        now: datetime | None = None,
    ) -> bool:
        try:
            updated = await self.finalize_in_transaction(
                request_id,
                status=status,
                public_result=public_result,
                http_status=http_status,
                http_body=http_body,
                now=now,
            )
            if updated:
                await self.session.commit()
            else:
                await self.session.rollback()
            return updated
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise RuntimeStorageError("Runtime operation finalization failed") from exc

    async def finalize_in_transaction(
        self,
        request_id: str,
        *,
        status: str,
        public_result: dict[str, Any],
        http_status: int,
        http_body: dict[str, Any],
        now: datetime | None = None,
    ) -> bool:
        if status not in {"completed", "partial", "failed", "indeterminate"}:
            raise ValueError("Runtime operation final status is invalid")
        updated = await self.repository.finalize(
            request_id,
            status=status,
            public_result=public_result,
            http_status=http_status,
            http_body=http_body,
            finished_at=now or datetime.now(UTC),
        )
        return updated

    async def read(
        self, principal: ApplicationPrincipal, request_id: str, *, now: datetime | None = None
    ) -> RuntimeOperationRecord | None:
        current_time = now or datetime.now(UTC)
        try:
            operation = await self.repository.scoped(principal.application_id, request_id)
            if operation is None:
                return None
            if operation.status == "running" and self._aware(operation.expires_at) <= current_time:
                updated = await self.repository.reconcile_expired(principal.application_id, current_time)
                if updated:
                    operation.status = "indeterminate"
                    operation.finished_at = current_time
                    await self.session.commit()
                else:
                    await self.session.rollback()
                    operation = await self.repository.scoped(principal.application_id, request_id)
            return operation
        except SQLAlchemyError as exc:
            await self.session.rollback()
            raise RuntimeStorageError("Runtime operation storage is unavailable") from exc

    async def _begin_serialized_transaction(self, application_id: str) -> None:
        dialect = self.session.bind.dialect.name if self.session.bind is not None else ""
        if self.session.in_transaction():
            await self.session.rollback()
        if dialect == "sqlite":
            await self.session.execute(text("BEGIN IMMEDIATE"))
            return
        await self.session.begin()
        if dialect == "postgresql":
            await self.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        application = await self.session.scalar(
            select(Application).where(Application.id == application_id).with_for_update()
        )
        if application is None:
            raise RuntimeStorageError("Application disappeared before operation claim")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
