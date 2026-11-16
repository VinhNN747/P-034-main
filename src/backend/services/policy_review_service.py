from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.policy_reviews import (
    PolicyDecisionCreate,
    PolicyDecisionRead,
    PolicyReviewPage,
    PolicyReviewRead,
)
from src.shared.models.integration import PolicyDecisionRecord, PolicyReviewRecord
from src.shared.repositories.audit import AuditRepository

DECISION_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "changes_requested",
}

DECISION_AUDIT = {"approve": "approved", "reject": "rejected", "request_changes": "changes_requested"}


class PolicyReviewNotFoundError(Exception):
    pass


class PolicyReviewConflictError(Exception):
    pass


class PolicyReviewService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit = AuditRepository(session)

    async def list(
        self,
        *,
        application_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PolicyReviewPage:
        conditions = []
        if application_id:
            conditions.append(PolicyReviewRecord.application_id == application_id)
        if status:
            conditions.append(PolicyReviewRecord.status == status)
        total = int(
            await self.session.scalar(select(func.count()).select_from(PolicyReviewRecord).where(*conditions)) or 0
        )
        rows = list(
            await self.session.scalars(
                select(PolicyReviewRecord)
                .where(*conditions)
                .order_by(PolicyReviewRecord.submitted_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return PolicyReviewPage(
            items=[await self._read(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def decide(self, actor_user_id: str, review_id: str, payload: PolicyDecisionCreate) -> PolicyReviewRead:
        review = await self.session.get(PolicyReviewRecord, review_id)
        if review is None:
            raise PolicyReviewNotFoundError("Policy review not found")
        if review.status != "pending":
            raise PolicyReviewConflictError("Policy review is no longer pending")
        review.status = DECISION_STATUS[payload.decision]
        review.decided_at = datetime.now(UTC)
        decision = PolicyDecisionRecord(
            review_id=review.id,
            actor_user_id=actor_user_id,
            decision=payload.decision,
            comment=payload.comment,
        )
        self.session.add(decision)
        await self.session.flush()
        await self.audit.add_event(
            actor_user_id=actor_user_id,
            action=f"policy_review.{DECISION_AUDIT[payload.decision]}",
            resource_type="policy_review",
            resource_id=review.id,
            metadata={"comment": payload.comment},
        )
        await self.session.commit()
        await self.session.refresh(review)
        return await self._read(review)

    async def _read(self, row: PolicyReviewRecord) -> PolicyReviewRead:
        decisions = list(
            await self.session.scalars(
                select(PolicyDecisionRecord)
                .where(PolicyDecisionRecord.review_id == row.id)
                .order_by(PolicyDecisionRecord.created_at, PolicyDecisionRecord.id)
            )
        )
        metadata = row.metadata_json or {}
        return PolicyReviewRead(
            id=row.id,
            name=row.name,
            scope=row.scope,
            type=row.review_type,
            submitted_by=row.submitted_by,
            submitted_at=row.submitted_at,
            status=row.status,
            decision_history=[
                PolicyDecisionRead(
                    id=item.id,
                    actor_user_id=item.actor_user_id,
                    decision=item.decision,
                    comment=item.comment,
                    created_at=item.created_at,
                )
                for item in decisions
            ],
            affected_models=list(row.affected_models_json or []),
            affected_applications=list(metadata.get("affected_applications", [])),
            application_id=row.application_id,
        )
