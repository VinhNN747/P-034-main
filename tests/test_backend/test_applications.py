from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.applications import ApplicationCreate, ApplicationUpdate
from src.backend.services.application_service import (
    ApplicationConflictError,
    ApplicationService,
    ApplicationStateError,
)
from src.shared.database.base import Base


@pytest_asyncio.fixture
async def application_session(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'apps.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_service_validates_environment_and_status(application_session):
    service = ApplicationService(application_session)

    application = await service.create(
        "user-1",
        ApplicationCreate(
            slug="real-estate-assistant",
            name="Real Estate Assistant",
            environment="staging",
            status="paused",
            tags=["real-estate", "vietnamese"],
        ),
    )

    assert application.environment == "staging"
    assert application.status == "paused"


@pytest.mark.asyncio
async def test_application_slug_is_unique(application_session):
    service = ApplicationService(application_session)
    payload = ApplicationCreate(slug="duplicate", name="Duplicate")

    await service.create("user-1", payload)

    with pytest.raises(ApplicationConflictError):
        await service.create("user-2", payload)


@pytest.mark.asyncio
async def test_application_name_is_unique(application_session):
    service = ApplicationService(application_session)

    await service.create("user-1", ApplicationCreate(slug="first-name", name="Shared name"))

    with pytest.raises(ApplicationConflictError, match="name already exists"):
        await service.create("user-2", ApplicationCreate(slug="second-name", name="Shared name"))


@pytest.mark.asyncio
async def test_application_name_cannot_be_reused_on_update(application_session):
    service = ApplicationService(application_session)
    await service.create("user-1", ApplicationCreate(slug="first", name="First"))
    second = await service.create("user-1", ApplicationCreate(slug="second", name="Second"))

    with pytest.raises(ApplicationConflictError, match="name already exists"):
        await service.update("user-1", second.id, ApplicationUpdate(name="First"))


@pytest.mark.asyncio
async def test_application_list_is_scoped_to_owner_and_paginated(application_session):
    service = ApplicationService(application_session)
    for slug in ("one", "two", "three"):
        await service.create("user-1", ApplicationCreate(slug=slug, name=slug))
    await service.create("user-2", ApplicationCreate(slug="other", name="Other"))

    page = await service.list("user-1", page=2, page_size=2)

    assert page.total == 3
    assert page.page == 2
    assert page.page_size == 2
    assert len(page.items) == 1
    assert all(item.owner_user_id == "user-1" for item in page.items)


@pytest.mark.asyncio
async def test_archived_application_cannot_be_updated_as_active(application_session):
    service = ApplicationService(application_session)
    application = await service.create("user-1", ApplicationCreate(slug="archived", name="Archived"))
    await service.archive("user-1", application.id)

    with pytest.raises(ApplicationStateError):
        await service.update("user-1", application.id, ApplicationUpdate(status="active"))
