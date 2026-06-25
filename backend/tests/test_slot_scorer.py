from datetime import date, time, datetime
from types import SimpleNamespace

import pytest

from app.services import slot_scorer
from app.utils.datetime_jst import JST


@pytest.mark.asyncio
async def test_find_best_practitioner_falls_back_when_top_candidate_conflicts(monkeypatch):
    target_date = date(2026, 4, 20)
    start_time = time(10, 0)

    practitioner_conflicted = SimpleNamespace(id=1, name="時田")
    practitioner_available = SimpleNamespace(id=2, name="上田")

    class _ScalarResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return self._items

    class _DB:
        async def execute(self, _query):
            return _ScalarResult([practitioner_conflicted, practitioner_available])

    class _BusinessHours:
        is_open = True

        @staticmethod
        def to_minutes():
            return 600, 1260

    async def fake_get_business_hours_for_date(_db, _target_date):
        return _BusinessHours()

    async def fake_load_day_infos(_db, _target_date, _practitioners):
        return [
            slot_scorer._DayInfo(practitioner_conflicted, True, [], [], 600, 1260),
            slot_scorer._DayInfo(practitioner_available, True, [], [], 600, 1260),
        ]

    async def fake_check_conflict(_db, practitioner_id, start_dt, end_dt):
        assert start_dt == datetime(2026, 4, 20, 10, 0, tzinfo=JST)
        assert end_dt == datetime(2026, 4, 20, 11, 0, tzinfo=JST)
        return [object()] if practitioner_id == 1 else []

    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_get_business_hours_for_date)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load_day_infos)
    monkeypatch.setattr(slot_scorer, "check_conflict", fake_check_conflict)

    practitioner, start_dt, end_dt, _, _ = await slot_scorer.find_best_practitioner(
        _DB(),
        target_date,
        start_time,
        60,
    )

    assert practitioner is practitioner_available
    assert start_dt == datetime(2026, 4, 20, 10, 0, tzinfo=JST)
    assert end_dt == datetime(2026, 4, 20, 11, 0, tzinfo=JST)


@pytest.mark.asyncio
async def test_find_best_practitioner_prefers_director(monkeypatch):
    target_date = date(2026, 4, 20)
    start_time = time(10, 0)

    # id=1は施術者、id=2は院長
    practitioner_staff = SimpleNamespace(id=1, name="施術者A", role="施術者", display_order=1)
    practitioner_director = SimpleNamespace(id=2, name="院長B", role="院長", display_order=2)

    class _ScalarResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return self._items

    class _DB:
        async def execute(self, _query):
            return _ScalarResult([practitioner_staff, practitioner_director])

    class _BusinessHours:
        is_open = True

        @staticmethod
        def to_minutes():
            return 600, 1260

    async def fake_get_business_hours_for_date(_db, _target_date):
        return _BusinessHours()

    async def fake_load_day_infos(_db, _target_date, _practitioners):
        return [
            slot_scorer._DayInfo(practitioner_staff, True, [], [], 600, 1260),
            slot_scorer._DayInfo(practitioner_director, True, [], [], 600, 1260),
        ]

    async def fake_check_conflict(_db, practitioner_id, start_dt, end_dt):
        return []

    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_get_business_hours_for_date)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load_day_infos)
    monkeypatch.setattr(slot_scorer, "check_conflict", fake_check_conflict)

    # 1) prefer_director=True の場合：院長 (id=2) が選ばれる
    practitioner, _, _, _, _ = await slot_scorer.find_best_practitioner(
        _DB(),
        target_date,
        start_time,
        60,
        prefer_director=True,
    )
    assert practitioner is practitioner_director

    # 2) prefer_director=False の場合：display_orderが若い施術者Aが選ばれる（通常挙動）
    practitioner, _, _, _, _ = await slot_scorer.find_best_practitioner(
        _DB(),
        target_date,
        start_time,
        60,
        prefer_director=False,
    )
    assert practitioner is practitioner_staff
