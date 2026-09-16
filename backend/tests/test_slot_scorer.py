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


def _make_candidate_db(day_infos):
    class _ScalarResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return self._items

    class _DB:
        async def execute(self, _query):
            return _ScalarResult([di.practitioner for di in day_infos])

    class _BusinessHours:
        is_open = True

        @staticmethod
        def to_minutes():
            return 600, 1260  # 10:00〜21:00

    async def fake_bh(_db, _d):
        return _BusinessHours()

    async def fake_load(_db, _d, _pracs):
        return day_infos

    return _DB(), fake_bh, fake_load


@pytest.mark.asyncio
async def test_build_same_day_candidates_spreads_preferred_practitioner_slots(monkeypatch):
    """希望担当がいれば、その担当の枠を時間帯を散らして埋める。"""
    target_date = date(2026, 8, 13)
    pref = SimpleNamespace(id=2, name="上田", role="院長", display_order=2)
    other = SimpleNamespace(id=1, name="時田", role="施術者", display_order=1)

    # 上田: 15:00(900)〜21:00(1260)は予約で埋まり、10:00〜15:00が空き。時田は終日空き。
    day_infos = [
        slot_scorer._DayInfo(other, True, [], [], 600, 1260),
        slot_scorer._DayInfo(pref, True, [(900, 1260)], [], 600, 1260),
    ]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    results = await slot_scorer.build_same_day_candidates(
        db, target_date, time(14, 0), 60, preferred_practitioner_id=2, max_results=3,
    )

    assert len(results) == 3
    # 希望担当で埋まりきるので他担当は出さない
    assert {r.practitioner_id for r in results} == {2}
    starts = [r.start_time.hour * 60 + r.start_time.minute for r in results]
    assert starts == sorted(starts)
    # 施術時間ぶん間隔があいている（同じ時間帯に固まらない）
    assert all(b - a >= 60 for a, b in zip(starts, starts[1:]))
    # 希望時刻の枠は必ず含む
    assert 14 * 60 in starts


@pytest.mark.asyncio
async def test_build_same_day_candidates_fills_from_other_practitioners(monkeypatch):
    """希望担当だけで埋まらなければ、他の担当の枠で補う。"""
    target_date = date(2026, 8, 13)
    pref = SimpleNamespace(id=2, name="上田", role="院長", display_order=2)
    other = SimpleNamespace(id=1, name="時田", role="施術者", display_order=1)

    # 上田は14:00〜15:00の1枠だけ空き。時田は終日空き。
    day_infos = [
        slot_scorer._DayInfo(other, True, [], [], 600, 1260),
        slot_scorer._DayInfo(pref, True, [(600, 840), (900, 1260)], [], 600, 1260),
    ]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    results = await slot_scorer.build_same_day_candidates(
        db, target_date, time(14, 0), 60, preferred_practitioner_id=2, max_results=3,
    )

    assert len(results) == 3
    assert 2 in {r.practitioner_id for r in results}
    assert 1 in {r.practitioner_id for r in results}


@pytest.mark.asyncio
async def test_build_same_day_candidates_without_preference_spreads_over_the_day(monkeypatch):
    """担当未指定でも、各担当の最速枠ではなく その日の空きを時間帯で散らす。

    2026-08-26 の実機不具合の再現ケース。以前は「各施術者の最速枠を1つずつ」しか
    返さず、候補数が出勤人数ぶんに固定され、夕方の空きが一切見えなかった。
    """
    target_date = date(2026, 8, 13)
    ueda = SimpleNamespace(id=1, name="上田", role="施術者", display_order=1)
    tokita = SimpleNamespace(id=2, name="時田", role="施術者", display_order=2)

    # 上田: 勤務10:00〜13:00 / 10:00〜11:00 だけ空き
    # 時田: 勤務10:00〜21:00 / 13:30〜14:30 と 16:00以降が空き
    day_infos = [
        slot_scorer._DayInfo(ueda, True, [(660, 780)], [], 600, 780),
        slot_scorer._DayInfo(tokita, True, [(600, 810), (870, 960)], [], 600, 1260),
    ]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    results = await slot_scorer.build_same_day_candidates(
        db, target_date, time(9, 0), 60, preferred_practitioner_id=None, max_results=3,
    )

    starts = [r.start_time.hour * 60 + r.start_time.minute for r in results]
    assert len(results) == 3, f"出勤2人でも3候補出ること: {starts}"
    assert starts == [600, 810, 960]  # 10:00 / 13:30 / 16:00
    # 夕方の空きが候補に出る（以前は各担当の最速枠しか見ずゼロだった）
    assert max(starts) >= 960


@pytest.mark.asyncio
async def test_build_same_day_candidates_skips_short_gaps(monkeypatch):
    """施術時間に満たない隙間は候補にしない（黙って短い施術を提案しない）。"""
    target_date = date(2026, 8, 13)
    pref = SimpleNamespace(id=2, name="上田", role="院長", display_order=2)
    other = SimpleNamespace(id=1, name="時田", role="施術者", display_order=1)

    # 上田: 10:00-14:00 と 14:40-21:00 が予約 → 14:00〜14:40 の40分だけ空き。
    day_infos = [
        slot_scorer._DayInfo(other, True, [], [], 600, 1260),
        slot_scorer._DayInfo(pref, True, [(600, 840), (880, 1260)], [], 600, 1260),
    ]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    results = await slot_scorer.build_same_day_candidates(
        db, target_date, time(14, 0), 60, preferred_practitioner_id=2, max_results=3,
    )

    # 40分の隙間は出さない。短縮の可否は患者に尋ねてから扱う。
    assert all(r.practitioner_id != 2 for r in results)
    assert all("40分" not in r.label for r in results)
    for candidate in results:
        start = candidate.start_time.hour * 60 + candidate.start_time.minute
        end = candidate.end_time.hour * 60 + candidate.end_time.minute
        assert end - start == 60

@pytest.mark.asyncio
async def test_build_day_availability_summary_reports_whole_day(monkeypatch):
    """候補だけでなく『その日どこがどう空いているか』を事実として返す。"""
    ueda = SimpleNamespace(id=1, name="上田", role="施術者", display_order=1)
    tokita = SimpleNamespace(id=2, name="時田", role="施術者", display_order=2)

    # 上田は非番。時田は 13:30〜14:00 の30分と 16:00〜21:00 が空き。
    day_infos = [
        slot_scorer._DayInfo(ueda, False),
        slot_scorer._DayInfo(tokita, True, [(600, 810), (840, 960)], [], 600, 1260),
    ]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    summary = await slot_scorer.build_day_availability_summary(db, date(2026, 8, 31), 60)

    by_name = {entry["practitioner"]: entry for entry in summary["practitioners"]}
    # 出勤していない事実は「空きが無い」と区別して渡す
    assert by_name["上田"]["working"] is False
    # 施術時間が収まる空きだけ open_blocks に入る
    assert by_name["時田"]["open_blocks"] == ["16:00〜21:00"]
    # 施術時間に満たない空きは別枠。こちらから短縮を勧めさせないため。
    assert by_name["時田"]["shorter_than_requested"] == ["13:30〜14:00(30分)"]
    assert summary["requested_duration_minutes"] == 60
    assert summary["business_hours"] == "10:00〜21:00"


@pytest.mark.asyncio
async def test_build_day_availability_summary_marks_closed_day(monkeypatch):
    """休診日は空き無しではなく休診として返す。"""
    tokita = SimpleNamespace(id=2, name="時田", role="施術者", display_order=2)
    day_infos = [slot_scorer._DayInfo(tokita, True, [], [], 600, 1260)]
    db, _fake_bh, fake_load = _make_candidate_db(day_infos)

    class _Closed:
        is_open = False

        @staticmethod
        def to_minutes():
            return 0, 0

    async def closed_bh(_db, _d):
        return _Closed()

    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", closed_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    summary = await slot_scorer.build_day_availability_summary(db, date(2026, 9, 1), 60)
    assert summary["closed"] is True
    assert summary["practitioners"] == []


@pytest.mark.asyncio
async def test_build_same_day_candidates_never_offers_a_time_already_past(monkeypatch):
    """今日の分は、すでに過ぎた時刻を案内しない。

    2026-09-04 実機: 23時台に「本日9月4日の13:30〜14:30でしたらご案内可能です」と、
    10時間前の枠を案内した。
    """
    from datetime import timedelta

    prac = SimpleNamespace(id=1, name="時田", role="施術者", display_order=1)
    day_infos = [slot_scorer._DayInfo(prac, True, [], [], 600, 1260)]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    # いま 18:00 とみなす（診療は 10:00〜21:00）
    fixed_now = datetime(2026, 9, 4, 18, 0, tzinfo=JST)
    monkeypatch.setattr(slot_scorer, "now_jst", lambda: fixed_now)

    results = await slot_scorer.build_same_day_candidates(
        db, fixed_now.date(), time(13, 30), 60, max_results=3,
    )
    starts = [r.start_time.hour * 60 + r.start_time.minute for r in results]
    assert starts, "まだ空いている時間帯があるのに候補が出ていない"
    assert min(starts) >= 18 * 60, starts

    # 別の日なら従来どおり終日から探す
    tomorrow = (fixed_now + timedelta(days=1)).date()
    future = await slot_scorer.build_same_day_candidates(
        db, tomorrow, time(13, 30), 60, max_results=3,
    )
    assert min(r.start_time.hour * 60 + r.start_time.minute for r in future) < 18 * 60

    # 閉店間際で枠が取れないなら、無理に出さない
    late_now = datetime(2026, 9, 4, 20, 45, tzinfo=JST)
    monkeypatch.setattr(slot_scorer, "now_jst", lambda: late_now)
    assert await slot_scorer.build_same_day_candidates(
        db, late_now.date(), time(20, 45), 60, max_results=3,
    ) == []


def test_the_candidate_label_reads_as_a_japanese_date_not_an_iso_string():
    """候補一覧の日付を LINE がリンク化しない形にする。

    2026-09-08 実機: 候補が「1. 2026-09-09 10:00〜11:00（担当: 上田）」と出て、
    LINE が ISO 日付を自動でリンク化するため青い文字だらけで読みにくかった。

    生成箇所が2つあり、担当者の書き方も「（時田）」「（担当:時田）」で
    揃っていなかったので、ScoredSlot の中で1回だけ作る形にした。
    片方だけ直しても落ちるよう、両方の生成経路を通る値を検査する。
    """
    slot = slot_scorer.ScoredSlot(
        date(2026, 9, 9), time(10, 0), time(11, 0), 2, "上田", 0.0,
    )

    assert slot.label == "9/9(水) 10:00〜11:00（担当: 上田）"
    assert "2026-" not in slot.label
    # to_dict の date は ISO のまま（曜日の検算や日付の突き合わせが使う）
    assert slot.to_dict()["date"] == "2026-09-09"
    assert slot.to_dict()["label"] == slot.label


def test_the_candidate_label_cannot_be_set_from_outside():
    """ラベルを引数で渡せる余地を残すと、また2通りの書き方が生まれる。"""
    with pytest.raises(TypeError):
        slot_scorer.ScoredSlot(
            date(2026, 9, 9), time(10, 0), time(11, 0), 2, "上田", 0.0,
            "2026-09-09 10:00〜11:00（上田）",
        )


@pytest.mark.asyncio
async def test_the_usual_practitioners_slot_comes_first_even_if_it_is_later_in_the_day(monkeypatch):
    """いつもの担当（希望担当）の枠を先頭に出す。

    2026-09-16 実機: いつもの担当は時田なのに「1. 10:40 上田 / 2. 11:40 上田 / 3. 13:35 時田」
    と出た。時田の枠を先に集めていたのに、最後に全体を時刻順へ並べ直していた。
    """
    target_date = date(2026, 8, 13)
    tokita = SimpleNamespace(id=1, name="時田", role="院長", display_order=1)
    ueda = SimpleNamespace(id=2, name="上田", role="施術者", display_order=2)

    # 時田: 13:35〜15:00 だけ空き（朝は埋まり、15時で上がり）。上田: 10:40〜13:00 が空き。
    day_infos = [
        slot_scorer._DayInfo(tokita, True, [(600, 815)], [], 600, 900),
        slot_scorer._DayInfo(ueda, True, [(600, 640), (780, 1260)], [], 600, 1260),
    ]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    results = await slot_scorer.build_same_day_candidates(
        db, target_date, time(9, 0), 60, preferred_practitioner_id=1, max_results=3,
        preferred_first=True,
    )

    assert [r.practitioner_id for r in results][0] == 1
    assert results[0].start_time == time(13, 35)
    # 希望担当の後ろは、他の担当を時刻順に
    others = [r for r in results if r.practitioner_id != 1]
    assert [r.start_time for r in others] == sorted(r.start_time for r in others)



@pytest.mark.asyncio
async def test_without_preferred_first_the_candidates_stay_in_time_order(monkeypatch):
    """既定は時刻順のまま。予約変更の「もっと早く」は早い順の先頭1件を提示するので、
    並びを変えると提示する枠そのものが変わる（2026-09-16 レビューで検出）。
    """
    target_date = date(2026, 8, 13)
    tokita = SimpleNamespace(id=1, name="時田", role="院長", display_order=1)
    ueda = SimpleNamespace(id=2, name="上田", role="施術者", display_order=2)
    day_infos = [
        slot_scorer._DayInfo(tokita, True, [(600, 815)], [], 600, 900),
        slot_scorer._DayInfo(ueda, True, [(600, 640), (780, 1260)], [], 600, 1260),
    ]
    db, fake_bh, fake_load = _make_candidate_db(day_infos)
    monkeypatch.setattr(slot_scorer, "get_business_hours_for_date", fake_bh)
    monkeypatch.setattr(slot_scorer, "_load_day_infos", fake_load)

    results = await slot_scorer.build_same_day_candidates(
        db, target_date, time(9, 0), 60, preferred_practitioner_id=1, max_results=3,
    )

    starts = [r.start_time for r in results]
    assert starts == sorted(starts)
