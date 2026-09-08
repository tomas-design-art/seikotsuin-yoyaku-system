from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import engine as app_engine
from app.models.line_webhook_event import LineWebhookEvent
from app.services.line_inbox import claim_pending_events


@asynccontextmanager
async def isolated_queue_sessions():
    schema_name = f"test_line_inbox_{uuid4().hex}"
    admin_engine = create_async_engine(app_engine.url, poolclass=NullPool)
    queue_engine = None
    schema_created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
        schema_created = True
        queue_engine = create_async_engine(
            app_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        async with queue_engine.begin() as connection:
            await connection.run_sync(LineWebhookEvent.__table__.create)
        yield async_sessionmaker(queue_engine, expire_on_commit=False)
    finally:
        if queue_engine is not None:
            await queue_engine.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_pending_events_are_claimed_with_a_skip_locked_row_lock():
    db = AsyncMock()
    result = Mock()
    result.scalars.return_value.all.return_value = []
    db.execute.return_value = result

    assert await claim_pending_events(db) == []

    statement = db.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "NOT (EXISTS" in sql


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_the_same_event():
    event_id = f"test-{uuid4().hex}"
    async with isolated_queue_sessions() as sessions:
        async with sessions() as setup_db:
            setup_db.add(
                LineWebhookEvent(
                    event_id=event_id,
                    line_user_id=f"user-{event_id}",
                    event_timestamp=int(datetime.now().timestamp() * 1000),
                    payload={"webhookEventId": event_id},
                    status="pending",
                )
            )
            await setup_db.commit()

        async with sessions() as first_db, sessions() as second_db:
            first_claim = await claim_pending_events(first_db, limit=1)
            second_claim = await claim_pending_events(second_db, limit=1)
            assert [record["event_id"] for record in first_claim] == [event_id]
            assert second_claim == []
            await first_db.rollback()
            await second_db.rollback()


@pytest.mark.asyncio
async def test_a_late_older_event_cannot_run_beside_a_processing_event():
    user_id = f"user-{uuid4().hex}"
    newer_event_id = f"test-{uuid4().hex}"
    older_event_id = f"test-{uuid4().hex}"
    async with isolated_queue_sessions() as sessions:
        async with sessions() as setup_db:
            setup_db.add(
                LineWebhookEvent(
                    event_id=newer_event_id,
                    line_user_id=user_id,
                    event_timestamp=2,
                    payload={"webhookEventId": newer_event_id},
                    status="pending",
                )
            )
            await setup_db.commit()

        async with sessions() as first_db:
            first_claim = await claim_pending_events(first_db)
            assert [record["event_id"] for record in first_claim] == [newer_event_id]

            async with sessions() as insert_db:
                insert_db.add(
                    LineWebhookEvent(
                        event_id=older_event_id,
                        line_user_id=user_id,
                        event_timestamp=1,
                        payload={"webhookEventId": older_event_id},
                        status="pending",
                    )
                )
                await insert_db.commit()

            async with sessions() as competing_db:
                assert await claim_pending_events(competing_db) == []
                await competing_db.rollback()
            await first_db.commit()

        async with sessions() as blocked_db:
            assert await claim_pending_events(blocked_db) == []
            await blocked_db.rollback()


@pytest.mark.asyncio
async def test_later_event_waits_for_the_same_users_first_event():
    user_id = f"user-{uuid4().hex}"
    first_event_id = f"test-{uuid4().hex}"
    second_event_id = f"test-{uuid4().hex}"
    async with isolated_queue_sessions() as sessions:
        async with sessions() as setup_db:
            setup_db.add_all(
                [
                    LineWebhookEvent(
                        event_id=first_event_id,
                        line_user_id=user_id,
                        event_timestamp=1,
                        payload={"webhookEventId": first_event_id},
                        status="pending",
                    ),
                    LineWebhookEvent(
                        event_id=second_event_id,
                        line_user_id=user_id,
                        event_timestamp=2,
                        payload={"webhookEventId": second_event_id},
                        status="pending",
                    ),
                ]
            )
            await setup_db.commit()

        async with sessions() as first_db:
            claimed = await claim_pending_events(first_db)
            await first_db.commit()
            assert [record["event_id"] for record in claimed] == [first_event_id]

        async with sessions() as blocked_db:
            assert await claim_pending_events(blocked_db) == []
            await blocked_db.rollback()

        async with sessions() as finish_db:
            await finish_db.execute(
                LineWebhookEvent.__table__.update()
                .where(LineWebhookEvent.event_id == first_event_id)
                .values(status="done")
            )
            await finish_db.commit()

        async with sessions() as second_db:
            claimed = await claim_pending_events(second_db)
            await second_db.rollback()
            assert [record["event_id"] for record in claimed] == [second_event_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_timestamp", [7, None])
async def test_equal_or_missing_timestamps_are_ordered_by_event_id(event_timestamp):
    user_id = f"user-{uuid4().hex}"
    first_event_id = f"test-{uuid4().hex}"
    second_event_id = f"test-{uuid4().hex}"
    async with isolated_queue_sessions() as sessions:
        async with sessions() as setup_db:
            setup_db.add_all(
                [
                    LineWebhookEvent(
                        event_id=first_event_id,
                        line_user_id=user_id,
                        event_timestamp=event_timestamp,
                        payload={"webhookEventId": first_event_id},
                        status="pending",
                    ),
                    LineWebhookEvent(
                        event_id=second_event_id,
                        line_user_id=user_id,
                        event_timestamp=event_timestamp,
                        payload={"webhookEventId": second_event_id},
                        status="pending",
                    ),
                ]
            )
            await setup_db.commit()

        async with sessions() as first_db:
            claimed = await claim_pending_events(first_db)
            await first_db.commit()
            assert [record["event_id"] for record in claimed] == [first_event_id]

        async with sessions() as finish_db:
            first = await finish_db.scalar(
                select(LineWebhookEvent).where(
                    LineWebhookEvent.event_id == first_event_id
                )
            )
            first.status = "done"
            await finish_db.commit()

        async with sessions() as second_db:
            claimed = await claim_pending_events(second_db)
            await second_db.rollback()
            assert [record["event_id"] for record in claimed] == [second_event_id]


@pytest.mark.asyncio
async def test_an_exhausted_processing_event_blocks_until_stale_then_fails():
    user_id = f"user-{uuid4().hex}"
    exhausted_event_id = f"test-{uuid4().hex}"
    next_event_id = f"test-{uuid4().hex}"
    async with isolated_queue_sessions() as sessions:
        async with sessions() as setup_db:
            setup_db.add_all(
                [
                    LineWebhookEvent(
                        event_id=exhausted_event_id,
                        line_user_id=user_id,
                        event_timestamp=1,
                        payload={"webhookEventId": exhausted_event_id},
                        status="processing",
                        attempts=5,
                        updated_at=datetime.now().astimezone(),
                    ),
                    LineWebhookEvent(
                        event_id=next_event_id,
                        line_user_id=user_id,
                        event_timestamp=2,
                        payload={"webhookEventId": next_event_id},
                        status="pending",
                    ),
                ]
            )
            await setup_db.commit()

        async with sessions() as fresh_db:
            assert await claim_pending_events(fresh_db) == []
            await fresh_db.rollback()

        async with sessions() as age_db:
            exhausted = await age_db.scalar(
                select(LineWebhookEvent).where(
                    LineWebhookEvent.event_id == exhausted_event_id
                )
            )
            exhausted.updated_at = datetime.now().astimezone() - timedelta(minutes=6)
            await age_db.commit()

        async with sessions() as recovery_db:
            claimed = await claim_pending_events(recovery_db)
            await recovery_db.commit()
            assert [record["event_id"] for record in claimed] == [next_event_id]

        async with sessions() as verify_db:
            exhausted = await verify_db.scalar(
                select(LineWebhookEvent).where(
                    LineWebhookEvent.event_id == exhausted_event_id
                )
            )
            assert exhausted.status == "failed"