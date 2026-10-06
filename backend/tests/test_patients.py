"""患者登録機能テスト"""
import unittest
from datetime import date


class TestPatientNameNormalization(unittest.TestCase):
    """名前正規化テスト"""

    def test_normalize_name_fullwidth_space(self):
        from app.schemas.patient import _normalize_name
        assert _normalize_name("田中\u3000太郎") == "田中 太郎"

    def test_normalize_name_multiple_spaces(self):
        from app.schemas.patient import _normalize_name
        assert _normalize_name("田中   太郎") == "田中 太郎"

    def test_normalize_name_leading_trailing(self):
        from app.schemas.patient import _normalize_name
        assert _normalize_name("  田中 太郎  ") == "田中 太郎"

    def test_normalize_phone_hyphen(self):
        from app.schemas.patient import _normalize_phone
        assert _normalize_phone("090-1234-5678") == "09012345678"

    def test_normalize_phone_fullwidth(self):
        from app.schemas.patient import _normalize_phone
        assert _normalize_phone("０９０１２３４５６７８") == "09012345678"

    def test_normalize_phone_none(self):
        from app.schemas.patient import _normalize_phone
        assert _normalize_phone(None) is None

    def test_normalize_phone_empty(self):
        from app.schemas.patient import _normalize_phone
        assert _normalize_phone("") is None


class TestPatientCreateSplitMode(unittest.TestCase):
    """PatientCreate split モード テスト"""

    def test_create_split_basic(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(registration_mode="split", last_name="田中", first_name="太郎")
        assert p.last_name == "田中"
        assert p.first_name == "太郎"
        assert p.registration_mode == "split"

    def test_create_split_default_mode(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(last_name="田中", first_name="太郎")
        assert p.registration_mode == "split"

    def test_create_split_normalizes_spaces(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(last_name="  田中  ", first_name="\u3000太郎\u3000")
        assert p.last_name == "田中"
        assert p.first_name == "太郎"

    def test_create_split_normalizes_phone(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(last_name="田中", first_name="太郎", phone="090-1234-5678")
        assert p.phone == "09012345678"

    def test_create_split_empty_last_name_fails(self):
        from app.schemas.patient import PatientCreate
        with self.assertRaises(Exception):
            PatientCreate(registration_mode="split", last_name="", first_name="太郎")

    def test_create_split_empty_first_name_fails(self):
        from app.schemas.patient import PatientCreate
        with self.assertRaises(Exception):
            PatientCreate(registration_mode="split", last_name="田中", first_name="")

    def test_create_split_missing_first_name_fails(self):
        from app.schemas.patient import PatientCreate
        with self.assertRaises(Exception):
            PatientCreate(registration_mode="split", last_name="田中")

    def test_create_split_with_middle_name(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(last_name="田中", middle_name="ミドル", first_name="太郎")
        assert p.middle_name == "ミドル"

    def test_create_split_with_reading(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(last_name="田中", first_name="太郎", reading="タナカ タロウ")
        assert p.reading == "タナカ タロウ"

    def test_create_split_with_all_fields(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(
            registration_mode="split",
            last_name="田中", first_name="太郎",
            middle_name="ミドル",
            reading="タナカ ミドル タロウ",
            last_name_kana="タナカ", first_name_kana="タロウ",
            birth_date=date(1990, 1, 15),
            phone="09012345678",
            email="tanaka@example.com",
            notes="テスト",
        )
        assert p.reading == "タナカ ミドル タロウ"
        assert p.birth_date == date(1990, 1, 15)


class TestPatientCreateFullNameMode(unittest.TestCase):
    """PatientCreate full_name モード テスト"""

    def test_create_fullname_basic(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(registration_mode="full_name", full_name="John Smith")
        assert p.full_name == "John Smith"
        assert p.registration_mode == "full_name"

    def test_create_fullname_normalizes_spaces(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(registration_mode="full_name", full_name="  John\u3000Smith  ")
        assert p.full_name == "John Smith"

    def test_create_fullname_empty_fails(self):
        from app.schemas.patient import PatientCreate
        with self.assertRaises(Exception):
            PatientCreate(registration_mode="full_name", full_name="")

    def test_create_fullname_missing_fails(self):
        from app.schemas.patient import PatientCreate
        with self.assertRaises(Exception):
            PatientCreate(registration_mode="full_name")

    def test_create_fullname_with_reading(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(
            registration_mode="full_name",
            full_name="John Smith",
            reading="ジョン スミス",
        )
        assert p.reading == "ジョン スミス"

    def test_create_fullname_with_phone(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(
            registration_mode="full_name",
            full_name="John Smith",
            phone="090-1234-5678",
        )
        assert p.phone == "09012345678"


class TestBuildName(unittest.TestCase):
    """build_name ヘルパー関数テスト"""

    def test_build_name_split_basic(self):
        from app.schemas.patient import build_name
        assert build_name("split", "田中", None, "太郎", None) == "田中 太郎"

    def test_build_name_split_with_middle(self):
        from app.schemas.patient import build_name
        assert build_name("split", "田中", "ミドル", "太郎", None) == "田中 ミドル 太郎"

    def test_build_name_full_name_mode(self):
        from app.schemas.patient import build_name
        assert build_name("full_name", None, None, None, "John Smith") == "John Smith"

    def test_build_name_full_name_normalizes(self):
        from app.schemas.patient import build_name
        assert build_name("full_name", None, None, None, "John\u3000Smith") == "John Smith"


class TestCandidateQuery(unittest.TestCase):
    """候補検索クエリテスト"""

    def test_candidate_query_split_normalizes(self):
        from app.schemas.patient import CandidateQuery
        q = CandidateQuery(
            registration_mode="split",
            last_name="  田中  ",
            first_name="\u3000太郎\u3000",
            phone="090-1234-5678",
        )
        assert q.last_name == "田中"
        assert q.first_name == "太郎"
        assert q.phone == "09012345678"

    def test_candidate_query_fullname(self):
        from app.schemas.patient import CandidateQuery
        q = CandidateQuery(
            registration_mode="full_name",
            full_name="John Smith",
        )
        assert q.full_name == "John Smith"

    def test_candidate_query_with_reading(self):
        from app.schemas.patient import CandidateQuery
        q = CandidateQuery(
            registration_mode="split",
            last_name="田中",
            reading="タナカ タロウ",
        )
        assert q.reading == "タナカ タロウ"

    def test_candidate_query_with_birth_date(self):
        from app.schemas.patient import CandidateQuery
        q = CandidateQuery(
            registration_mode="split",
            last_name="田中",
            first_name="太郎",
            birth_date=date(1990, 1, 15),
        )
        assert q.birth_date == date(1990, 1, 15)


class TestPatientNumberFormat(unittest.TestCase):
    """患者番号フォーマットテスト"""

    def test_format_p000001(self):
        num = f"P{1:06d}"
        assert num == "P000001"

    def test_format_p000100(self):
        num = f"P{100:06d}"
        assert num == "P000100"


class TestPatientMatchFindOrCreate(unittest.TestCase):
    """patient_match の回帰テスト"""

    def _make_db(self):
        import asyncio
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base
        from app.models.menu import Menu
        from app.models.patient import Patient
        from app.models.practitioner import Practitioner
        from app.models.reservation import Reservation
        from app.models.reservation_color import ReservationColor
        from app.models.reservation_series import ReservationSeries

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def setup():
            async with engine.begin() as conn:
                # 作る表を明示する。Base.metadata には先に走ったテストが読み込んだモデル
                # （SQLite では作れない JSONB の表など）も載るので、全部作ろうとすると
                # 単体では通るのに全体で回すと落ちる（2026-10-06 修正）。
                await conn.run_sync(
                    Base.metadata.create_all,
                    tables=[
                        Patient.__table__,
                        Practitioner.__table__,
                        Menu.__table__,
                        ReservationColor.__table__,
                        ReservationSeries.__table__,
                        Reservation.__table__,
                    ],
                )
            return Session

        loop = asyncio.new_event_loop()
        SessionLocal = loop.run_until_complete(setup())
        return loop, SessionLocal, engine

    def test_find_or_create_reuses_existing_patient_for_same_name_and_phone(self):
        from app.services.patient_match import find_or_create_patient

        loop, Session, engine = self._make_db()

        async def _run():
            async with Session() as db:
                first = await find_or_create_patient(
                    db,
                    name="tokita makoto",
                    phone="0120117117",
                    auto_number=False,
                )
                await db.commit()

            async with Session() as db:
                second = await find_or_create_patient(
                    db,
                    name="tokita makoto",
                    phone="0120117117",
                    auto_number=False,
                )
                await db.commit()
                return first.id, second.id

        first_id, second_id = loop.run_until_complete(_run())
        assert first_id == second_id

        loop.run_until_complete(engine.dispose())

    def test_find_or_create_reuses_existing_patient_for_single_phone_candidate(self):
        from app.services.patient_match import find_or_create_patient

        loop, Session, engine = self._make_db()

        async def _run():
            async with Session() as db:
                first = await find_or_create_patient(
                    db,
                    name="tokita makoto",
                    phone="0120-117-117",
                    auto_number=False,
                )
                await db.commit()

            async with Session() as db:
                second = await find_or_create_patient(
                    db,
                    name="tokita  makoto",
                    phone="0120117117",
                    auto_number=False,
                )
                await db.commit()
                return first.id, second.id

        first_id, second_id = loop.run_until_complete(_run())
        assert first_id == second_id

        loop.run_until_complete(engine.dispose())

    def test_format_sequential(self):
        nums = [f"P{i:06d}" for i in range(1, 4)]
        assert nums == ["P000001", "P000002", "P000003"]


class TestNameLengthValidation(unittest.TestCase):
    """名前文字数制限テスト（外国人名対応: 100文字まで）"""

    def test_long_name_within_limit(self):
        from app.schemas.patient import PatientCreate
        long_name = "A" * 100
        p = PatientCreate(last_name=long_name, first_name="Test")
        assert len(p.last_name) == 100

    def test_long_name_exceeds_limit(self):
        from app.schemas.patient import PatientCreate
        import pydantic
        with self.assertRaises(pydantic.ValidationError):
            PatientCreate(last_name="A" * 101, first_name="Test")

    def test_foreign_name_with_spaces(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(last_name="O'Brien-Smith", first_name="John Michael")
        assert p.last_name == "O'Brien-Smith"
        assert p.first_name == "John Michael"

    def test_unicode_names(self):
        from app.schemas.patient import PatientCreate
        p = PatientCreate(last_name="Müller", first_name="François")
        assert p.last_name == "Müller"
        assert p.first_name == "François"

    def test_fullname_mode_length_limit(self):
        from app.schemas.patient import PatientCreate
        long_name = "A" * 100
        p = PatientCreate(registration_mode="full_name", full_name=long_name)
        assert len(p.full_name) == 100

    def test_fullname_mode_exceeds_limit(self):
        from app.schemas.patient import PatientCreate
        import pydantic
        with self.assertRaises(pydantic.ValidationError):
            PatientCreate(registration_mode="full_name", full_name="A" * 101)


if __name__ == "__main__":
    unittest.main()
