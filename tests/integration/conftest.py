import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from condora.db.base import Base
from condora.db.engine import create_session_factory
from condora.db.repository import Repository
from condora.db.tables import (  # noqa: F401
    DAGHistoryRow,
    DAGRow,
    ProcessingBlockRow,
    RequestRow,
    SiteRow,
    WorkflowRow,
)

TEST_DB_URL = "postgresql+asyncpg://condoratest:condoratest@localhost:5432/condoratest"


@pytest.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
async def session(engine):
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def repo(session):
    return Repository(session)
