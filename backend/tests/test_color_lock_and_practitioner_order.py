"""ホットペッパー予約の色の固定と、施術者の並び替え（2026-09-19）。

ホットペッパーのメール取り込みは、色コード #f2740d で「ホットペッパー予約」の色を探す。
画面から色を変えたり消したり、別の色を同じコードにすると、自動の色付けが外れたり
（2件あると）取り込みが落ちたりするので、画面からは変えられないようにする。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.practitioners import PractitionerOrderItem, reorder_practitioners
from app.api.reservation_colors import create_color, delete_color, update_color
from app.models.practitioner import Practitioner
from app.models.reservation_color import HOTPEPPER_COLOR_CODE, ReservationColor
from app.schemas.reservation_color import ReservationColorCreate, ReservationColorUpdate
from tests.test_hotpepper_rpa_queues_real_db import isolated_sessions


async def _seed_colors(sessions) -> tuple[int, int]:
    async with sessions() as db:
        hotpepper = ReservationColor(name="ホットペッパー予約", color_code=HOTPEPPER_COLOR_CODE)
        other = ReservationColor(name="保険診療", color_code="#2563eb")
        db.add_all([hotpepper, other])
        await db.commit()
        return hotpepper.id, other.id


@pytest.mark.asyncio
async def test_the_hotpepper_color_cannot_be_changed_or_deleted():
    async with isolated_sessions() as sessions:
        hotpepper_id, _ = await _seed_colors(sessions)
        async with sessions() as db:
            with pytest.raises(HTTPException) as changed:
                await update_color(hotpepper_id, ReservationColorUpdate(color_code="#000000"), db, {})
        async with sessions() as db:
            with pytest.raises(HTTPException) as made_default:
                await update_color(hotpepper_id, ReservationColorUpdate(is_default=True), db, {})
        async with sessions() as db:
            with pytest.raises(HTTPException) as deleted:
                await delete_color(hotpepper_id, db, {})
        async with sessions() as db:
            kept = await db.get(ReservationColor, hotpepper_id)

    assert changed.value.status_code == made_default.value.status_code == deleted.value.status_code == 400
    assert kept.color_code == HOTPEPPER_COLOR_CODE


@pytest.mark.asyncio
async def test_no_other_color_can_take_the_hotpepper_code():
    async with isolated_sessions() as sessions:
        _, other_id = await _seed_colors(sessions)
        async with sessions() as db:
            with pytest.raises(HTTPException) as created:
                await create_color(ReservationColorCreate(name="まぎらわしい色", color_code="#F2740D"), db, {})
        async with sessions() as db:
            with pytest.raises(HTTPException) as updated:
                await update_color(other_id, ReservationColorUpdate(color_code="#f2740d"), db, {})
        async with sessions() as db:
            ok = await update_color(other_id, ReservationColorUpdate(name="保険"), db, {})

    assert created.value.status_code == updated.value.status_code == 400
    assert ok.name == "保険"


@pytest.mark.asyncio
async def test_practitioners_can_be_reordered():
    async with isolated_sessions() as sessions:
        async with sessions() as db:
            first, second, third = Practitioner(name="A", display_order=0), Practitioner(name="B", display_order=1), Practitioner(name="C", display_order=2)
            db.add_all([first, second, third])
            await db.commit()
            ids = [first.id, second.id, third.id]
        async with sessions() as db:
            ordered = await reorder_practitioners(
                [PractitionerOrderItem(id=ids[2], display_order=0),
                 PractitionerOrderItem(id=ids[0], display_order=1),
                 PractitionerOrderItem(id=ids[1], display_order=2)],
                db,
                {},
            )

    assert [p.name for p in ordered] == ["C", "A", "B"]
