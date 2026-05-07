"""HotPepperメール解析テスト — SALON BOARD 実フォーマット4パターン対応"""
import pytest
from datetime import datetime
from app.agents.mail_parser import parse_hotpepper_mail, detect_event_type
from app.utils.datetime_jst import JST

# ===========================================================================
# サンプルメール定義（実際の SALON BOARD メール4パターン）
# 氏名は個人情報のためダミー名に置換済み
# ===========================================================================

# ── パターン0: 通常予約（[全員]クーポン、合計金額あり） ──
SAMPLE_MAIL_CREATED_BASE = """\
差出人: SALON BOARD <yoyaku_system@salonboard.com>
件名: 予約連絡

\ufeffcoco整骨院様

HOT PEPPER Beauty「SALON BOARD」にお客様から
ご予約が入りました。

◇ご予約内容
■予約番号
　BE64275292
■氏名
　テスト太郎
■来店日時
　2026年03月25日（水）10:00
■指名スタッフ
　指名なし
■メニュー
　ボディケア＋整体＋骨盤矯正＋接骨・整骨
　（所要時間目安：1時間）
■ご利用クーポン
　[全員]
　【腰痛でお悩みの方】全身整体　６０分６7００円
　　《辛い箇所を徹底的にケア☆》深層筋までしっかりアプローチ◎ガチガチに凝り固まった筋肉をしっかりほぐして改善します♪身体の不調に応じてボディケア、ストレッチも組み合わせた施術です。
■合計金額
　予約時合計金額　6,700円
　今回の利用ギフト券　利用なし
　今回の利用ポイント　利用なし
　お支払い予定金額　6,700円
※表示金額は、予約時に選択したメニュー金額(クーポン適用の場合は適用後の金額)の合計金額です。来店時のメニュー変更、サロンが別途設定するスタッフ指名料等の追加料金やキャンセル料等により、実際の支払額と異なる場合があります。
追加料金についてはこちらから↓
https://beauty.help.hotpepper.jp/s/article/000031948
◇ご予約付加情報
■ご要望・ご相談
　-

PC版SALON BOARD
https://salonboard.com/login/
スマートフォン版SALON BOARD
https://salonboard.com/login_sp/

予約受付日時：2026年03月23日（月）14:08

===================================================
SALON BOARD・HOT PEPPER Beauty
お問い合わせ：https://sbhd-kirei.salonboard.com/hc/ja/articles/360039395113
===================================================
"""

# ── パターン①: キャンセル連絡（合計金額なし、クーポンタグなし） ──
SAMPLE_MAIL_CANCEL = """\
差出人: SALON BOARD <yoyaku_system@salonboard.com>
件名: 【明日】キャンセル連絡


\ufeffcoco整骨院様

HOT PEPPER Beauty「SALON BOARD」にお客様から
ご予約のキャンセルがありました。

◇ご予約内容
■予約番号
　BE22417450
■氏名
　テスト一郎
■来店日時
　2026年01月17日（土）16:00
■指名スタッフ
　指名なし
■メニュー
　ボディケア＋ヘッド＋整体＋接骨・整骨
　（所要時間目安：1時間）
■ご利用クーポン
　【人気No.1首肩こり/眼精疲労】全身整体　６０分６６００円→６０００円
　　《辛い箇所を徹底的にケア☆》深層筋までしっかりアプローチ◎ガチガチに凝り固まった筋肉をしっかりほぐして改善します♪身体の不調に応じてヘッド/アイマッサージも組み合わせた施術です。

PC版SALON BOARD
https://salonboard.com/login/
スマートフォン版SALON BOARD
https://salonboard.com/login_sp/

予約受付日時：2026年01月10日（土）16:42

===================================================
SALON BOARD・HOT PEPPER Beauty
お問い合わせ：https://sbhd-kirei.salonboard.com/hc/ja/articles/360039395113
===================================================
"""

# ── パターン②: 直前予約（[新規]クーポンタグ、全フィールドあり） ──
SAMPLE_MAIL_URGENT = """\
差出人: SALON BOARD直前予約お知らせ <yoyaku_system@salonboard.com>
件名: 【当日16時00分】直前予約が入りました


\ufeffcoco整骨院様

HOT PEPPER Beauty「SALON BOARD」にお客様から
ご予約が入りました。

◇ご予約内容
■予約番号
　BE67865212
■氏名
　テスト二郎
■来店日時
　2026年03月29日（日）16:00
■指名スタッフ
　指名なし
■メニュー
　ボディケア＋整体＋骨盤矯正＋接骨・整骨
　（所要時間目安：1時間）
■ご利用クーポン
　[新規]
　【腰痛でお悩みの方】全身整体　６０分６7００円→６０００円
　　《辛い箇所を徹底的にケア☆》深層筋までしっかりアプローチ◎ガチガチに凝り固まった筋肉をしっかりほぐして改善します♪身体の不調に応じてボディケア、ストレッチも組み合わせた施術です。
■合計金額
　予約時合計金額　6,000円
　今回の利用ギフト券　利用なし
　今回の利用ポイント　利用なし
　お支払い予定金額　6,000円
※表示金額は、予約時に選択したメニュー金額(クーポン適用の場合は適用後の金額)の合計金額です。来店時のメニュー変更、サロンが別途設定するスタッフ指名料等の追加料金やキャンセル料等により、実際の支払額と異なる場合があります。
追加料金についてはこちらから↓
https://beauty.help.hotpepper.jp/s/article/000031948
◇ご予約付加情報
■ご要望・ご相談
　-

PC版SALON BOARD
https://salonboard.com/login/
スマートフォン版SALON BOARD
https://salonboard.com/login_sp/

予約受付日時：2026年03月29日（日）15:40

===================================================
SALON BOARD・HOT PEPPER Beauty
お問い合わせ：https://sbhd-kirei.salonboard.com/hc/ja/articles/360039395113
===================================================
"""

# ── パターン③: 翌日予約（[全員]クーポン、別金額） ──
SAMPLE_MAIL_NEXT_DAY = """\
差出人: SALON BOARD <yoyaku_system@salonboard.com>
件名: 【明日】予約連絡


\ufeffcoco整骨院様

HOT PEPPER Beauty「SALON BOARD」にお客様から
ご予約が入りました。

◇ご予約内容
■予約番号
　BD72321027
■氏名
　テスト三郎
■来店日時
　2025年10月12日（日）10:00
■指名スタッフ
　指名なし
■メニュー
　ボディケア＋整体＋骨盤矯正＋接骨・整骨
　（所要時間目安：1時間）
■ご利用クーポン
　[全員]
　【人気No.2　腰痛でお悩みの方】全身整体　６０分６６００円
　　《辛い箇所を徹底的にケア☆》深層筋までしっかりアプローチ◎ガチガチに凝り固まった筋肉をしっかりほぐして改善します♪身体の不調に応じてボディケア、ストレッチも組み合わせた施術です。
■合計金額
　予約時合計金額　6,600円
　今回の利用ギフト券　利用なし
　今回の利用ポイント　利用なし
　お支払い予定金額　6,600円
※表示金額は、予約時に選択したメニュー金額(クーポン適用の場合は適用後の金額)の合計金額です。来店時のメニュー変更、サロンが別途設定するスタッフ指名料等の追加料金やキャンセル料等により、実際の支払額と異なる場合があります。
追加料金についてはこちらから↓
https://beauty.help.hotpepper.jp/s/article/000031948
◇ご予約付加情報
■ご要望・ご相談
　-

PC版SALON BOARD
https://salonboard.com/login/
スマートフォン版SALON BOARD
https://salonboard.com/login_sp/

予約受付日時：2025年10月11日（土）15:05

===================================================
SALON BOARD・HOT PEPPER Beauty
お問い合わせ：https://sbhd-kirei.salonboard.com/hc/ja/articles/360039395113
===================================================
"""

# ── パターン④: 未対応予約リマインダー（処理対象外） ──
SAMPLE_MAIL_REMINDER = """\
差出人: SALON BOARD <yoyaku_system@salonboard.com>
件名: 【SALON BOARD】本日分の未対応予約のお知らせ


\ufeffcoco整骨院様

いつも「HOT PEPPER Beauty」をご利用いただきまして
誠にありがとうございます。

SALON BOARDに、本日来店を希望されている予約で
ご確認いただきたい予約が1件あります。
お早めに、予約詳細画面より予約内容のご確認をお願いいたします。

■内容
　予約番号：[BD79835217]　　[即時予約:未読]　　予約日：[2025年10月25日]


※予約内容の詳細の確認・予約処理は、
下記SALON BOARDからお願いいたします。

■予約内容の確認方法
予約一覧画面から確認したい予約の予約番号リンクをクリックすると、詳細情報が表示されます。

SALON BOARD
https://salonboard.com/login/
スマートフォン版SALON BOARD
https://salonboard.com/login_sp/

※本メールはSALON BOARD上で未読または仮予約確定待ちの
　予約があるサロン様へ自動で送信しております。
※本メールを受信されるまでに、すでに確認済みの予約が含まれている場合がございます。
　行き違いでご確認がお済みの場合はご容赦ください。
※お客様と直接お電話などで調整されている場合でも、
　SALON BOARD上で変更が完了していない予約は本メールの送信対象となります。
　SALON BOARDのご確認をお願いいたします。

===========================================================================
SALON BOARD・HOT PEPPER Beautyヘルプデスク
お問い合わせ：https://sbhd-kirei.salonboard.com/hc/ja/articles/360039395113
===========================================================================
"""


# ===========================================================================
# パターン0: 通常予約（既存テスト）
# ===========================================================================


class TestParseCreatedBase:
    """パターン0: 通常予約メール（[全員]クーポン・合計金額あり）"""

    def setup_method(self):
        self.result = parse_hotpepper_mail(SAMPLE_MAIL_CREATED_BASE)

    def test_event_type(self):
        assert self.result["event_type"] == "created"

    def test_reservation_number(self):
        assert self.result["reservation_number"] == "BE64275292"

    def test_patient_name(self):
        assert self.result["patient_name"] == "テスト太郎"

    def test_start_time(self):
        st = self.result["start_time"]
        assert (st.year, st.month, st.day, st.hour, st.minute) == (2026, 3, 25, 10, 0)
        assert st.tzinfo is not None

    def test_duration_minutes(self):
        assert self.result["duration_minutes"] == 60

    def test_end_time(self):
        assert self.result["end_time"].hour == 11
        assert self.result["end_time"].minute == 0

    def test_practitioner_none(self):
        assert self.result["practitioner_name"] is None

    def test_menu_name(self):
        assert self.result["menu_name"] == "ボディケア＋整体＋骨盤矯正＋接骨・整骨"

    def test_amount(self):
        assert self.result["amount"] == 6700

    def test_coupon(self):
        assert self.result["coupon_name"] is not None
        assert "全身整体" in self.result["coupon_name"]

    def test_note_dash(self):
        assert self.result["note"] is None

    def test_received_at(self):
        ra = self.result["received_at"]
        assert (ra.year, ra.month, ra.day, ra.hour, ra.minute) == (2026, 3, 23, 14, 8)


# ===========================================================================
# パターン①: キャンセル連絡
# ===========================================================================


class TestParseCancelMail:
    """パターン①: キャンセルメール"""

    def setup_method(self):
        self.result = parse_hotpepper_mail(SAMPLE_MAIL_CANCEL)

    def test_event_type(self):
        assert self.result["event_type"] == "cancelled"

    def test_reservation_number(self):
        assert self.result["reservation_number"] == "BE22417450"

    def test_patient_name(self):
        assert self.result["patient_name"] == "テスト一郎"

    def test_start_time(self):
        st = self.result["start_time"]
        assert (st.year, st.month, st.day, st.hour, st.minute) == (2026, 1, 17, 16, 0)

    def test_duration(self):
        assert self.result["duration_minutes"] == 60

    def test_end_time(self):
        assert self.result["end_time"].hour == 17

    def test_practitioner_none(self):
        assert self.result["practitioner_name"] is None

    def test_menu_name(self):
        assert self.result["menu_name"] == "ボディケア＋ヘッド＋整体＋接骨・整骨"

    def test_amount_none(self):
        """キャンセルメールには合計金額セクションがない"""
        assert self.result["amount"] is None

    def test_coupon_no_tag(self):
        """タグなしクーポン（[全員]等がない場合、1行目がクーポン名）"""
        assert self.result["coupon_name"] is not None
        assert "首肩こり" in self.result["coupon_name"]

    def test_received_at(self):
        ra = self.result["received_at"]
        assert (ra.year, ra.month, ra.day, ra.hour, ra.minute) == (2026, 1, 10, 16, 42)


# ===========================================================================
# パターン②: 直前予約
# ===========================================================================


class TestParseUrgentMail:
    """パターン②: 直前予約メール（[新規]クーポンタグ）"""

    def setup_method(self):
        self.result = parse_hotpepper_mail(SAMPLE_MAIL_URGENT)

    def test_event_type(self):
        assert self.result["event_type"] == "created"

    def test_reservation_number(self):
        assert self.result["reservation_number"] == "BE67865212"

    def test_patient_name(self):
        assert self.result["patient_name"] == "テスト二郎"

    def test_start_time(self):
        st = self.result["start_time"]
        assert (st.year, st.month, st.day, st.hour, st.minute) == (2026, 3, 29, 16, 0)

    def test_end_time(self):
        assert self.result["end_time"].hour == 17

    def test_menu_name(self):
        assert self.result["menu_name"] == "ボディケア＋整体＋骨盤矯正＋接骨・整骨"

    def test_amount(self):
        assert self.result["amount"] == 6000

    def test_coupon_new_tag(self):
        """[新規]タグ付きクーポン"""
        assert self.result["coupon_name"] is not None
        assert "腰痛" in self.result["coupon_name"]

    def test_received_at(self):
        ra = self.result["received_at"]
        assert (ra.year, ra.month, ra.day, ra.hour, ra.minute) == (2026, 3, 29, 15, 40)


# ===========================================================================
# パターン③: 翌日予約（別金額）
# ===========================================================================


class TestParseNextDayMail:
    """パターン③: 翌日予約メール（[全員]クーポン、6,600円）"""

    def setup_method(self):
        self.result = parse_hotpepper_mail(SAMPLE_MAIL_NEXT_DAY)

    def test_event_type(self):
        assert self.result["event_type"] == "created"

    def test_reservation_number(self):
        assert self.result["reservation_number"] == "BD72321027"

    def test_patient_name(self):
        assert self.result["patient_name"] == "テスト三郎"

    def test_start_time(self):
        st = self.result["start_time"]
        assert (st.year, st.month, st.day, st.hour, st.minute) == (2025, 10, 12, 10, 0)

    def test_amount(self):
        assert self.result["amount"] == 6600

    def test_coupon(self):
        assert "人気No.2" in self.result["coupon_name"]

    def test_received_at(self):
        ra = self.result["received_at"]
        assert (ra.year, ra.month, ra.day, ra.hour, ra.minute) == (2025, 10, 11, 15, 5)


# ===========================================================================
# パターン④: リマインダー（処理対象外）
# ===========================================================================


class TestParseReminderMail:
    """パターン④: 未対応予約のお知らせ → 処理対象外で ValueError"""

    def test_raises_value_error(self):
        with pytest.raises(ValueError, match="リマインダーメール"):
            parse_hotpepper_mail(SAMPLE_MAIL_REMINDER)

    def test_detect_event_type_reminder(self):
        assert detect_event_type(SAMPLE_MAIL_REMINDER) == "reminder"


# ===========================================================================
# detect_event_type 単体テスト（実メール + 合成パターン）
# ===========================================================================


class TestDetectEventType:
    """メール種別判定テスト"""

    def test_created_from_real_mail(self):
        assert detect_event_type(SAMPLE_MAIL_CREATED_BASE) == "created"

    def test_created_from_urgent(self):
        assert detect_event_type(SAMPLE_MAIL_URGENT) == "created"

    def test_created_from_next_day(self):
        assert detect_event_type(SAMPLE_MAIL_NEXT_DAY) == "created"

    def test_cancelled_from_real_mail(self):
        assert detect_event_type(SAMPLE_MAIL_CANCEL) == "cancelled"

    def test_reminder_from_real_mail(self):
        assert detect_event_type(SAMPLE_MAIL_REMINDER) == "reminder"

    def test_changed_synthetic(self):
        assert detect_event_type("予約変更のお知らせです") == "changed"

    def test_changed_synthetic2(self):
        assert detect_event_type("予約が変更されました") == "changed"

    def test_generic_created(self):
        assert detect_event_type("ご予約が入りました") == "created"


# ===========================================================================
# 異常系・補助系
# ===========================================================================


class TestParseErrors:
    """必須フィールド欠落で例外"""

    def test_missing_reservation_number(self):
        body = "■氏名\n　テスト太郎\n■来店日時\n　2026年03月25日（水）10:00"
        with pytest.raises(ValueError, match="予約番号"):
            parse_hotpepper_mail(body)

    def test_missing_patient_name(self):
        body = "■予約番号\n　BE12345\n■来店日時\n　2026年03月25日（水）10:00"
        with pytest.raises(ValueError, match="氏名"):
            parse_hotpepper_mail(body)

    def test_missing_start_time(self):
        body = "■予約番号\n　BE12345\n■氏名\n　テスト太郎"
        with pytest.raises(ValueError, match="来店日時"):
            parse_hotpepper_mail(body)

    def test_empty_body(self):
        with pytest.raises(ValueError):
            parse_hotpepper_mail("")


class TestParseDefaults:
    """補助系テスト"""

    def test_duration_default_60(self):
        """所要時間が本文に存在しない場合デフォルト60分"""
        body = (
            "■予約番号\n　BE99999\n"
            "■氏名\n　デフォルト太郎\n"
            "■来店日時\n　2026年04月01日（火）14:00\n"
            "■メニュー\n　テストメニュー\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 60
        assert result["end_time"].hour == 15

    def test_practitioner_named(self):
        """指名スタッフが名前の場合"""
        body = (
            "■予約番号\n　BE88888\n"
            "■氏名\n　指名テスト\n"
            "■来店日時\n　2026年04月01日（火）09:00\n"
            "■指名スタッフ\n　山田花子\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["practitioner_name"] == "山田花子"


# ===========================================================================
# 氏名カタカナ読み抽出テスト
# ===========================================================================


class TestPatientNameReading:
    """氏名の（カタカナ）読み抽出"""

    def test_reading_extracted(self):
        """全角カッコ内のカタカナをひらがな変換して reading に抽出"""
        body = (
            "■予約番号\n　BE77777\n"
            "■氏名\n　井戸 貴之（イド タカユキ）\n"
            "■来店日時\n　2026年04月01日（火）10:00\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["patient_name"] == "井戸 貴之"
        assert result["patient_reading"] == "いど たかゆき"

    def test_reading_half_width_parens(self):
        """半角カッコでもひらがな変換して読み抽出できる"""
        body = (
            "■予約番号\n　BE77778\n"
            "■氏名\n　山田 太郎(ヤマダ タロウ)\n"
            "■来店日時\n　2026年04月01日（火）11:00\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["patient_name"] == "山田 太郎"
        assert result["patient_reading"] == "やまだ たろう"

    def test_no_reading(self):
        """カッコなし→ reading は None"""
        body = (
            "■予約番号\n　BE77779\n"
            "■氏名\n　テスト太郎\n"
            "■来店日時\n　2026年04月01日（火）12:00\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["patient_name"] == "テスト太郎"
        assert result["patient_reading"] is None

    def test_existing_patterns_no_reading(self):
        """既存パターンは全て reading=None"""
        for mail in [SAMPLE_MAIL_CREATED_BASE, SAMPLE_MAIL_CANCEL, SAMPLE_MAIL_URGENT, SAMPLE_MAIL_NEXT_DAY]:
            result = parse_hotpepper_mail(mail)
            assert result["patient_reading"] is None, f"Failed for {result['patient_name']}"


# ===========================================================================
# 所要時間パース — 複合パターン（X時間Y分）
# ===========================================================================

class TestDurationCompound:
    """_parse_duration の複合パターンテスト"""

    def test_1hour_30min(self):
        """1時間30分 → 90分"""
        body = (
            "差出人: SALON BOARD <yoyaku_system@salonboard.com>\n"
            "件名: 予約連絡\n\n"
            "coco整骨院様\nご予約が入りました。\n"
            "◇ご予約内容\n"
            "■予約番号\n　BE99999999\n"
            "■氏名\n　テスト花子\n"
            "■来店日時\n　2026年05月01日（金）14:00\n"
            "■メニュー\n　ボディケア＋ヘッド＋整体＋接骨・整骨\n"
            "　（所要時間目安：1時間30分）\n"
            "■ご利用クーポン\n"
            "　【土日祝限定　人気No.2】深層筋整体　30％off 90分12000円→8500円\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 90
        assert result["duration_extracted"] is True

    def test_2hours(self):
        """2時間 → 120分"""
        body = (
            "差出人: SALON BOARD <yoyaku_system@salonboard.com>\n"
            "件名: 予約連絡\n\n"
            "coco整骨院様\nご予約が入りました。\n"
            "◇ご予約内容\n"
            "■予約番号\n　BE88888888\n"
            "■氏名\n　テスト次郎\n"
            "■来店日時\n　2026年05月02日（土）10:00\n"
            "■メニュー\n　フルコース\n"
            "　（所要時間目安：2時間）\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 120
        assert result["duration_extracted"] is True

    def test_45min_only(self):
        """45分 → 45分"""
        body = (
            "差出人: SALON BOARD <yoyaku_system@salonboard.com>\n"
            "件名: 予約連絡\n\n"
            "coco整骨院様\nご予約が入りました。\n"
            "◇ご予約内容\n"
            "■予約番号\n　BE77777777\n"
            "■氏名\n　テスト三郎\n"
            "■来店日時\n　2026年05月03日（日）16:00\n"
            "■メニュー\n　お試しコース\n"
            "　（所要時間目安：45分）\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 45
        assert result["duration_extracted"] is True

    def test_coupon_fallback_duration(self):
        """所要時間なし + クーポンに90分 → 90分"""
        body = (
            "差出人: SALON BOARD <yoyaku_system@salonboard.com>\n"
            "件名: 予約連絡\n\n"
            "coco整骨院様\nご予約が入りました。\n"
            "◇ご予約内容\n"
            "■予約番号\n　BE66666666\n"
            "■氏名\n　テスト四郎\n"
            "■来店日時\n　2026年05月04日（月）11:00\n"
            "■メニュー\n　ボディケア\n"
            "■ご利用クーポン\n"
            "　【全員】深層筋整体90分8500円\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 90
        assert result["duration_extracted"] is True

    def test_coupon_crosscheck_overrides_shorter_estimate(self):
        """所要時間目安1時間 + クーポン90分 → クーポン側の90分を採用"""
        body = (
            "差出人: SALON BOARD <yoyaku_system@salonboard.com>\n"
            "件名: 予約連絡\n\n"
            "coco整骨院様\nご予約が入りました。\n"
            "◇ご予約内容\n"
            "■予約番号\n　BE55555555\n"
            "■氏名\n　テスト五郎\n"
            "■来店日時\n　2026年05月05日（火）13:00\n"
            "■メニュー\n　ボディケア＋ヘッド＋整体＋接骨・整骨\n"
            "　（所要時間目安：1時間）\n"
            "■ご利用クーポン\n"
            "　【土日祝限定　人気No.2】深層筋整体　30％off 90分12000円→8500円\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 90

    def test_coupon_crosscheck_keeps_longer_estimate(self):
        """所要時間目安2時間 + クーポン60分 → 目安側の120分を維持"""
        body = (
            "差出人: SALON BOARD <yoyaku_system@salonboard.com>\n"
            "件名: 予約連絡\n\n"
            "coco整骨院様\nご予約が入りました。\n"
            "◇ご予約内容\n"
            "■予約番号\n　BE44444444\n"
            "■氏名\n　テスト六郎\n"
            "■来店日時\n　2026年05月06日（水）15:00\n"
            "■メニュー\n　フルコース\n"
            "　（所要時間目安：2時間）\n"
            "■ご利用クーポン\n"
            "　【全員】お試し60分5000円\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 120

    def test_40min_fullwidth_coupon(self):
        """所要時間目安40分 + クーポンに全角数字４０分 → 40分（2026-05-07実事例）"""
        body = (
            "差出人: SALON BOARD直前予約お知らせ <yoyaku_system@salonboard.com>\n"
            "件名: 【当日18時45分】直前予約が入りました\n\n"
            "\ufeffcoco整骨院様\n\n"
            "HOT PEPPER Beauty「SALON BOARD」にお客様から\nご予約が入りました。\n\n"
            "◇ご予約内容\n"
            "■予約番号\n　BE91112308\n"
            "■氏名\n　テスト七郎（テスト ナナロウ）\n"
            "■来店日時\n　2026年05月07日（木）18:45\n"
            "■指名スタッフ\n　指名なし\n"
            "■メニュー\n　ボディケア＋整体＋接骨・整骨\n"
            "　（所要時間目安：40分）\n"
            "■ご利用クーポン\n"
            "　[新規]\n"
            "　【人気No.3首肩こり/眼精疲労】全身整体\u3000４０分４7００円→４０００円\n"
            "■合計金額\n　予約時合計金額\u30004,000円\n"
        )
        result = parse_hotpepper_mail(body)
        assert result["duration_minutes"] == 40
        assert result["duration_extracted"] is True
        assert result["end_time"].hour == 19
        assert result["end_time"].minute == 25


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
