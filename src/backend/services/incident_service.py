from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.incidents import IncidentCreate, IncidentPage, IncidentRead, IncidentReportCreate, IncidentUpdate
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, IncidentRecord
from src.shared.repositories.audit import AuditRepository

SEVERITY_OUT = {
    "info": "low",
    "warning": "medium",
    "error": "high",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
}
STATUS_OUT = {
    "open": "investigating",
    "acknowledged": "monitoring",
    "investigating": "investigating",
    "monitoring": "monitoring",
    "resolved": "resolved",
}


class IncidentNotFoundError(Exception):
    pass


class IncidentValidationError(Exception):
    pass


class IncidentService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit = AuditRepository(session)

    async def list(
        self,
        user_id: str,
        *,
        application_id: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
        reports_only: bool = False,
        can_read_global: bool = False,
    ) -> IncidentPage:
        owned = or_(
            IncidentRecord.application_id.is_(None),
            IncidentRecord.application_id.in_(select(Application.id).where(Application.owner_user_id == user_id)),
        )
        source = IncidentRecord.metadata_json["source"].as_string()
        reporter = IncidentRecord.metadata_json["reporter_user_id"].as_string()
        # User reports without an application must never become global incidents.
        conditions = [] if can_read_global else [
            or_(reporter == user_id, owned & or_(source.is_(None), source != "user_report"))
        ]
        if reports_only:
            conditions.extend([source == "user_report", reporter == user_id])
        if application_id:
            conditions.append(IncidentRecord.application_id == application_id)
        if severity:
            stored_severities = [key for key, value in SEVERITY_OUT.items() if value == severity]
            conditions.append(IncidentRecord.severity.in_(stored_severities))
        if status:
            stored_statuses = [key for key, value in STATUS_OUT.items() if value == status]
            conditions.append(IncidentRecord.status.in_(stored_statuses))
        if search:
            term = f"%{search}%"
            conditions.append(or_(IncidentRecord.title.ilike(term), IncidentRecord.description.ilike(term)))
        total = int(await self.session.scalar(select(func.count()).select_from(IncidentRecord).where(*conditions)) or 0)
        ordering = (
            (IncidentRecord.updated_at.desc(), IncidentRecord.created_at.desc())
            if reports_only
            else (IncidentRecord.created_at.desc(),)
        )
        rows = list(
            await self.session.scalars(
                select(IncidentRecord)
                .where(*conditions)
                .order_by(*ordering)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return IncidentPage(items=[self._read(row) for row in rows], page=page, page_size=page_size, total=total)

    async def report(self, user_id: str, email: str | None, payload: IncidentReportCreate) -> IncidentRead:
        application_id = payload.application_id
        await self._check_application(user_id, application_id)
        if payload.request_id:
            request = await self.session.scalar(
                select(GatewayRequestRecord)
                .join(Application, Application.id == GatewayRequestRecord.application_id)
                .where(GatewayRequestRecord.request_id == payload.request_id, Application.owner_user_id == user_id)
            )
            if request is None or (application_id and request.application_id != application_id):
                raise IncidentValidationError("Request not found for this application")
            application_id = request.application_id
        metadata = {
            "source": "user_report",
            "reporter_user_id": user_id,
            "reporter_email": email,
            "request_id": payload.request_id,
            "steps_to_reproduce": payload.steps_to_reproduce,
            "expected_behavior": payload.expected_behavior,
            "owner": "Unassigned",
            "type": payload.type,
            "scope": application_id or payload.model or "Application",
            "impact": "User-reported issue",
        }
        return await self.create(
            user_id,
            IncidentCreate(
                application_id=application_id,
                title=payload.title,
                description=payload.description,
                severity="medium",
                status="investigating",
                type=payload.type,
                scope=metadata["scope"],
                impact=metadata["impact"],
                provider=payload.provider,
                model=payload.model,
            ),
            report_metadata=metadata,
        )

    async def create(
        self, user_id: str, payload: IncidentCreate, *,
        can_manage_global: bool = False, report_metadata: dict | None = None,
    ) -> IncidentRead:
        await self._check_application(user_id, payload.application_id, can_manage_global=can_manage_global)
        metadata = dict(payload.metadata)
        for field in ("source", "reporter_user_id", "reporter_email", "request_id", "steps_to_reproduce", "expected_behavior"):
            metadata.pop(field, None)
        metadata.update(
            {
                "owner": payload.owner,
                "type": payload.type,
                "scope": payload.scope,
                "impact": payload.impact,
            }
        )
        metadata.update(report_metadata or {"source": "operational"})
        incident = IncidentRecord(
            application_id=payload.application_id,
            title=payload.title,
            description=payload.description,
            severity=payload.severity,
            status=payload.status,
            provider=payload.provider,
            model=payload.model,
            metadata_json=metadata,
        )
        self.session.add(incident)
        await self.session.flush()
        await self.audit.add_event(
            actor_user_id=user_id,
            action="incident.reported" if report_metadata else "incident.created",
            resource_type="incident",
            resource_id=incident.id,
        )
        await self.session.commit()
        await self.session.refresh(incident)
        return self._read(incident)

    async def update(
        self, user_id: str, incident_id: str, payload: IncidentUpdate, *, can_manage_global: bool = False,
    ) -> IncidentRead:
        incident = await self.session.get(IncidentRecord, incident_id)
        if incident is None:
            raise IncidentNotFoundError("Incident not found")
        if not can_manage_global:
            await self._check_application(user_id, incident.application_id)
        values = payload.model_dump(exclude_unset=True)
        metadata = dict(incident.metadata_json or {})
        for field in ("owner", "type", "scope", "impact", "resolution_note"):
            if field in values:
                metadata[field] = values.pop(field)
        for field, value in values.items():
            setattr(incident, field, value)
        incident.metadata_json = metadata
        await self.audit.add_event(
            actor_user_id=user_id,
            action="incident.updated",
            resource_type="incident",
            resource_id=incident.id,
            metadata={"fields": list(payload.model_dump(exclude_unset=True))},
        )
        await self.session.commit()
        await self.session.refresh(incident)
        return self._read(incident)

    async def _check_application(
        self, user_id: str, application_id: str | None, *, can_manage_global: bool = False,
    ) -> None:
        if application_id is None:
            return
        conditions = [Application.id == application_id]
        if not can_manage_global:
            conditions.append(Application.owner_user_id == user_id)
        exists = await self.session.scalar(select(Application.id).where(*conditions))
        if exists is None:
            raise IncidentValidationError("Application not found")

    @staticmethod
    def _read(row: IncidentRecord) -> IncidentRead:
        metadata = row.metadata_json or {}
        opened = row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at
        updated = row.updated_at.replace(tzinfo=UTC) if row.updated_at.tzinfo is None else row.updated_at
        end = updated if row.status == "resolved" else datetime.now(UTC)
        return IncidentRead(
            id=row.id,
            title=row.title,
            description=row.description,
            severity=SEVERITY_OUT.get(row.severity, "medium"),
            status=STATUS_OUT.get(row.status, "investigating"),
            owner=str(metadata.get("owner", "Unassigned")),
            type=str(metadata.get("type", "operational")),
            scope=str(metadata.get("scope", "Gateway")),
            opened_at=row.created_at,
            duration_seconds=max(0.0, (end - opened).total_seconds()),
            impact=str(metadata.get("impact", "Unknown")),
            application_id=row.application_id,
            provider=row.provider,
            model=row.model,
            updated_at=row.updated_at,
            source="user_report" if metadata.get("source") == "user_report" else "operational",
            reporter_user_id=metadata.get("reporter_user_id"),
            reporter_email=metadata.get("reporter_email"),
            request_id=metadata.get("request_id"),
            steps_to_reproduce=str(metadata.get("steps_to_reproduce", "")),
            expected_behavior=str(metadata.get("expected_behavior", "")),
            resolution_note=str(metadata.get("resolution_note", "")),
        )
